from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, model_validator

from .config import CostConfig
from .market_clock import MARKET_TZ, force_exit_at, is_session, session_bounds
from .models import (
    CandidatePlan,
    EntrySpec,
    ExitPolicy,
    Market,
    RuleGroup,
    StopLossSpec,
    TakeProfitSpec,
    TradePlan,
)
from .notifications import TelegramNotifier
from .repository import PlanConflictError, Repository
from .validator import ENTRY_WINDOWS, PlanValidationError, validate_plan


logger = logging.getLogger(__name__)
_FIELD_NAMES = ("종목명", "종목코드", "진입가격", "익절가격", "손절가격")
_FIELD_PATTERN = re.compile(r"^\s*(종목명|종목코드|진입가격|익절가격|손절가격)\s*:\s*(.+?)\s*$")
_BLOCK_SPLIT = re.compile(r"^\s*---\s*$", re.MULTILINE)


class TelegramTradeParseError(ValueError):
    pass


class SimpleTelegramTrade(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    symbol: str = Field(pattern=r"^\d{6}$")
    entry_price: int = Field(gt=0)
    take_profit_price: int = Field(gt=0)
    stop_price: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_prices(self) -> "SimpleTelegramTrade":
        if not self.stop_price < self.entry_price < self.take_profit_price:
            raise ValueError("가격은 손절가격 < 진입가격 < 익절가격 순서여야 합니다")
        return self


@dataclass(frozen=True)
class TelegramTradeResult:
    status: str
    detail: str
    plan_id: str | None = None
    content_hash: str | None = None


def _strip_code_fences(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if not line.strip().startswith("```")]
    return "\n".join(lines).strip()


def _price(value: str) -> int:
    normalized = re.sub(r"[\s,원₩]", "", value)
    if not normalized.isdigit():
        raise TelegramTradeParseError(f"가격 형식이 올바르지 않습니다: {value}")
    return int(normalized)


def parse_simple_trade_message(text: str) -> list[SimpleTelegramTrade]:
    normalized = _strip_code_fences(text)
    if normalized == "오늘 패스":
        return []
    blocks = [block.strip() for block in _BLOCK_SPLIT.split(normalized) if block.strip()]
    if not 1 <= len(blocks) <= 3:
        raise TelegramTradeParseError("종목은 1개부터 최대 3개까지 전달할 수 있습니다")
    trades: list[SimpleTelegramTrade] = []
    seen_symbols: set[str] = set()
    for block in blocks:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            match = _FIELD_PATTERN.fullmatch(line)
            if match is None:
                raise TelegramTradeParseError(f"허용되지 않은 줄이 있습니다: {line.strip()}")
            key, value = match.groups()
            if key in fields:
                raise TelegramTradeParseError(f"중복 필드가 있습니다: {key}")
            fields[key] = value.strip()
        missing = [name for name in _FIELD_NAMES if name not in fields]
        if missing:
            raise TelegramTradeParseError(f"필수 필드가 없습니다: {', '.join(missing)}")
        try:
            trade = SimpleTelegramTrade(
                name=fields["종목명"],
                symbol=fields["종목코드"],
                entry_price=_price(fields["진입가격"]),
                take_profit_price=_price(fields["익절가격"]),
                stop_price=_price(fields["손절가격"]),
            )
        except ValueError as exc:
            raise TelegramTradeParseError(str(exc)) from exc
        if trade.symbol in seen_symbols:
            raise TelegramTradeParseError(f"종목코드가 중복되었습니다: {trade.symbol}")
        seen_symbols.add(trade.symbol)
        trades.append(trade)
    return trades


class SimpleTelegramTradeService:
    def __init__(
        self,
        repository: Repository,
        universe: dict[str, dict[str, dict[str, str]]],
        costs: dict[str, CostConfig],
        notifier: TelegramNotifier,
        *,
        allowed_chat_id: str,
        max_message_age_seconds: float = 120,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.universe = universe
        self.costs = costs
        self.notifier = notifier
        self.allowed_chat_id = str(allowed_chat_id)
        self.max_message_age_seconds = max_message_age_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(_strip_code_fences(text).encode("utf-8")).hexdigest()

    def _authorized_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id", ""))
        return (
            chat.get("type") == "private"
            and chat_id == self.allowed_chat_id
            and str(sender.get("id", "")) == self.allowed_chat_id
        )

    @staticmethod
    def _normalized_name(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    def _build_plan(
        self, trades: list[SimpleTelegramTrade], update_id: int, now: datetime
    ) -> TradePlan:
        market = Market.KR
        local_now = now.astimezone(MARKET_TZ[market])
        trade_date = local_now.date()
        if not is_session(market, trade_date):
            raise PlanValidationError("오늘은 한국거래소 정규 거래일이 아닙니다")
        earliest, latest = ENTRY_WINDOWS[market]
        current_time = local_now.time().replace(tzinfo=None, microsecond=0)
        if current_time >= latest:
            raise PlanValidationError("오늘 신규 진입 가능 시간이 종료되었습니다")
        entry_start = max(earliest, current_time)
        _, session_close = session_bounds(market, trade_date)
        force_exit = force_exit_at(market, trade_date)
        allowed = self.universe.get(market.value, {})
        candidates: list[CandidatePlan] = []
        for trade in trades:
            metadata = allowed.get(trade.symbol)
            if metadata is None:
                raise PlanValidationError(f"허용 종목 목록에 없는 종목입니다: {trade.symbol}")
            expected_name = str(metadata["name"])
            if self._normalized_name(trade.name) != self._normalized_name(expected_name):
                raise PlanValidationError(
                    f"종목명과 종목코드가 일치하지 않습니다: {trade.symbol}"
                )
            if trade.entry_price > self.costs[market.value].initial_cash:
                raise PlanValidationError(
                    f"총자금으로 1주를 매수할 수 없습니다: {trade.symbol}"
                )
            trigger_price = max(
                trade.stop_price + 1,
                int(round(trade.stop_price * 1.001)),
            )
            candidates.append(
                CandidatePlan(
                    symbol=trade.symbol,
                    exchange=metadata["exchange"],
                    reason="Telegram 전달 가격 기반 자동 가상매매",
                    strategy_type="rules",
                    entry=EntrySpec(
                        trigger_price=trigger_price,
                        limit_price=trade.entry_price,
                        start_time=entry_start,
                        end_time=latest,
                        price_only=True,
                        rules=RuleGroup(),
                    ),
                    stop_loss=StopLossSpec(price=trade.stop_price),
                    take_profit=[
                        TakeProfitSpec(price=trade.take_profit_price, quantity_pct=100)
                    ],
                    exit_policy=ExitPolicy(
                        max_holding_minutes=360,
                        no_progress_exit_minutes=None,
                        exit_if_below_vwap_sec=None,
                    ),
                    force_exit_time=force_exit.time().replace(tzinfo=None),
                )
            )
        version = self.repository.next_plan_version(market, trade_date)
        plan = TradePlan(
            plan_id=f"TG_KR_{trade_date:%Y%m%d}_{update_id}",
            plan_version=version,
            created_at=now,
            market=market,
            trade_date=trade_date,
            expires_at=session_close.astimezone(MARKET_TZ[market]),
            approval_nonce="000000",
            approved_symbols=candidates,
        )
        validate_plan(plan, self.universe, self.costs, now=now)
        return plan

    async def process_update(self, update: dict[str, Any]) -> TelegramTradeResult:
        update_id = update.get("update_id")
        message = update.get("message")
        if not isinstance(update_id, int) or not isinstance(message, dict):
            return TelegramTradeResult("IGNORED", "메시지 업데이트가 아닙니다")
        if not self._authorized_message(message):
            return TelegramTradeResult("IGNORED", "허용되지 않은 발신자입니다")
        text = message.get("text")
        message_id = message.get("message_id")
        message_date = message.get("date")
        if not isinstance(text, str) or not isinstance(message_id, int):
            return TelegramTradeResult("IGNORED", "텍스트 메시지가 아닙니다")
        now = self.clock().astimezone(UTC)
        if not isinstance(message_date, int):
            return TelegramTradeResult("IGNORED", "메시지 시각이 없습니다")
        age = (now - datetime.fromtimestamp(message_date, UTC)).total_seconds()
        if age < -30 or age > self.max_message_age_seconds:
            return TelegramTradeResult("IGNORED", "오래되었거나 미래 시각인 메시지입니다")
        local_trade_date = now.astimezone(MARKET_TZ[Market.KR]).date().isoformat()
        content_hash = self._content_hash(f"{local_trade_date}\n{text}")
        claimed = self.repository.claim_telegram_trade_message(
            update_id=update_id,
            message_id=message_id,
            chat_id=self.allowed_chat_id,
            content_hash=content_hash,
        )
        if not claimed:
            return TelegramTradeResult("DUPLICATE", "이미 처리한 메시지입니다")
        try:
            trades = parse_simple_trade_message(text)
            if not trades:
                self.repository.set_telegram_trade_message_status(
                    update_id, "NO_TRADE", "오늘 패스"
                )
                await self.notifier.send_best_effort("오늘 패스: 주문 없음")
                return TelegramTradeResult("NO_TRADE", "오늘 패스")
            plan = self._build_plan(trades, update_id, now)
            plan_hash = self.repository.arm_telegram_plan(plan, update_id=update_id)
        except (TelegramTradeParseError, PlanValidationError, PlanConflictError, ValueError) as exc:
            detail = str(exc)
            self.repository.set_telegram_trade_message_status(update_id, "REJECTED", detail)
            await self.notifier.send_best_effort(f"등록 거절: {detail}")
            return TelegramTradeResult("REJECTED", detail)
        symbols = ", ".join(candidate.symbol for candidate in plan.approved_symbols)
        detail = f"등록완료: {symbols} / 자동 가상매매"
        self.repository.set_telegram_trade_message_status(update_id, "ARMED", plan.plan_id)
        await self.notifier.send_best_effort(detail)
        return TelegramTradeResult("ARMED", detail, plan.plan_id, plan_hash)


class TelegramTradeReceiver:
    def __init__(
        self,
        token: str,
        service: SimpleTelegramTradeService,
        *,
        poll_timeout_seconds: int = 25,
    ):
        self.token = token
        self.service = service
        self.poll_timeout_seconds = poll_timeout_seconds
        self.offset: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/getUpdates"

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        params: dict[str, Any] = {
            "timeout": self.poll_timeout_seconds,
            "allowed_updates": json.dumps(["message"]),
        }
        if self.offset is not None:
            params["offset"] = self.offset
        response = await client.get(
            self.url,
            params=params,
            timeout=self.poll_timeout_seconds + 10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            raise httpx.HTTPError("Telegram getUpdates returned an error")
        for update in payload.get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = max(self.offset or 0, update_id + 1)
            await self.service.process_update(update)

    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            while not self._stop.is_set():
                try:
                    await self.poll_once(client)
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPError:
                    logger.warning("Telegram trade polling failed")
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except TimeoutError:
                        pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="telegram-trade-receiver")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
