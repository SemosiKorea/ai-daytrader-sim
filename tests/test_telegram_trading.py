from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from daytrader.config import CostConfig
from daytrader.market_clock import MARKET_TZ, is_session
from daytrader.models import Market, TradePlan
from daytrader.repository import Repository
from daytrader.telegram_trading import (
    SimpleTelegramTradeService,
    TelegramTradeParseError,
    parse_simple_trade_message,
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_best_effort(self, message: str) -> bool:
        self.messages.append(message)
        return True


def _next_session_at(hour: int = 8, minute: int = 45) -> datetime:
    timezone = MARKET_TZ[Market.KR]
    trade_date = datetime.now(timezone).date() + timedelta(days=1)
    while not is_session(Market.KR, trade_date):
        trade_date += timedelta(days=1)
    return datetime(
        trade_date.year,
        trade_date.month,
        trade_date.day,
        hour,
        minute,
        tzinfo=timezone,
    ).astimezone(UTC)


def _service(tmp_path, now: datetime):
    repository = Repository(tmp_path / "telegram.db")
    notifier = RecordingNotifier()
    costs = {
        "KR": CostConfig(
            initial_cash=3_000_000,
            commission_bps_each_side=0,
            slippage_bps_each_side=0,
            sell_tax_bps=0,
            fx_bps_each_side=0,
        )
    }
    universe = {
        "KR": {
            "005930": {"name": "삼성전자", "exchange": "KRX", "theme": "반도체"}
        }
    }
    service = SimpleTelegramTradeService(
        repository,
        universe,
        costs,
        notifier,  # type: ignore[arg-type]
        allowed_chat_id="123456789",
        clock=lambda: now,
    )
    return service, repository, notifier, costs


def _update(now: datetime, *, update_id: int = 100, text: str | None = None) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "date": int(now.timestamp()),
            "chat": {"id": 123456789, "type": "private"},
            "from": {"id": 123456789},
            "text": text
            or (
                "종목명: 삼성전자\n"
                "종목코드: 005930\n"
                "진입가격: 100,000원\n"
                "익절가격: 104000\n"
                "손절가격: 98000"
            ),
        },
    }


def test_parse_exact_five_field_blocks() -> None:
    trades = parse_simple_trade_message(
        "종목명: 삼성전자\n종목코드: 005930\n진입가격: 100,000원\n"
        "익절가격: 104000\n손절가격: 98000\n---\n"
        "종목명: SK하이닉스\n종목코드: 000660\n진입가격: 200000\n"
        "익절가격: 210000\n손절가격: 196000"
    )
    assert [trade.symbol for trade in trades] == ["005930", "000660"]
    assert trades[0].entry_price == 100_000


def test_parser_rejects_extra_or_inverted_fields() -> None:
    with pytest.raises(TelegramTradeParseError, match="허용되지 않은 줄"):
        parse_simple_trade_message(
            "종목명: 삼성전자\n종목코드: 005930\n진입가격: 100000\n"
            "익절가격: 104000\n손절가격: 98000\n추천이유: 강세"
        )
    with pytest.raises(TelegramTradeParseError, match="손절가격 < 진입가격 < 익절가격"):
        parse_simple_trade_message(
            "종목명: 삼성전자\n종목코드: 005930\n진입가격: 100000\n"
            "익절가격: 99000\n손절가격: 98000"
        )


@pytest.mark.asyncio
async def test_forwarded_message_arms_one_simple_paper_plan(tmp_path) -> None:
    now = _next_session_at()
    service, repository, notifier, costs = _service(tmp_path, now)

    result = await service.process_update(_update(now))

    assert result.status == "ARMED"
    assert costs["KR"].initial_cash == 3_000_000
    stored = repository.get_plan(result.plan_id or "")
    assert stored is not None
    assert stored["status"] == "ARMED"
    plan = TradePlan.model_validate_json(stored["payload"])
    assert len(plan.approved_symbols) == 1
    candidate = plan.approved_symbols[0]
    assert candidate.symbol == "005930"
    assert candidate.entry.limit_price == 100_000
    assert candidate.take_profit[0].price == 104_000
    assert candidate.stop_loss.price == 98_000
    assert notifier.messages == ["등록완료: 005930 / 자동 가상매매"]


@pytest.mark.asyncio
async def test_duplicate_content_and_wrong_sender_are_ignored(tmp_path) -> None:
    now = _next_session_at()
    service, repository, _, _ = _service(tmp_path, now)
    first = await service.process_update(_update(now, update_id=100))
    duplicate = await service.process_update(_update(now, update_id=101))
    wrong = _update(now, update_id=102)
    wrong["message"]["from"]["id"] = 999
    unauthorized = await service.process_update(wrong)

    assert first.status == "ARMED"
    assert duplicate.status == "DUPLICATE"
    assert unauthorized.status == "IGNORED"
    assert repository.telegram_trade_message(102) is None


@pytest.mark.asyncio
async def test_today_pass_records_no_trade(tmp_path) -> None:
    now = _next_session_at()
    service, repository, notifier, _ = _service(tmp_path, now)

    result = await service.process_update(_update(now, text="오늘 패스"))

    assert result.status == "NO_TRADE"
    assert repository.telegram_trade_message(100)["status"] == "NO_TRADE"
    assert notifier.messages == ["오늘 패스: 주문 없음"]
