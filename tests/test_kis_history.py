from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from daytrader.feed_history import FeedHistoryStore
from daytrader.kis_history import (
    KISUSMinuteHistorySync,
    completed_session_dates,
    parse_kis_us_minute_bar,
)
from daytrader.market_clock import MARKET_TZ, session_bounds
from daytrader.models import Market


def _row(at: datetime, price: float = 100.0) -> dict[str, str]:
    return {
        "xymd": at.strftime("%Y%m%d"),
        "xhms": at.strftime("%H%M%S"),
        "open": str(price),
        "high": str(price + 1),
        "low": str(price - 1),
        "last": str(price + 0.25),
        "evol": "100",
        "eamt": "10025",
    }


class FakeHistoryClient:
    async def get_market_data(
        self, path: str, *, tr_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        key = datetime.strptime(params["KEYB"], "%Y%m%d%H%M%S").replace(
            tzinfo=MARKET_TZ[Market.US]
        )
        return {
            "rt_cd": "0",
            "output2": [_row(key - timedelta(minutes=offset)) for offset in range(120)],
        }


def test_parse_kis_us_minute_bar_preserves_exact_notional() -> None:
    at = datetime(2026, 7, 15, 9, 30, tzinfo=MARKET_TZ[Market.US])
    bar = parse_kis_us_minute_bar("nvda", _row(at))

    assert bar.symbol == "NVDA"
    assert bar.start == at
    assert bar.volume == 100
    assert bar.notional == 10025


def test_completed_sessions_excludes_still_open_session() -> None:
    market = Market.US
    session_date = date(2026, 7, 15)
    session_open, _ = session_bounds(market, session_date)

    result = completed_session_dates(market, session_open + timedelta(hours=1), 1)

    assert result[0] < session_date


async def test_sync_kis_history_requires_and_saves_complete_regular_session(tmp_path) -> None:
    store = FeedHistoryStore(tmp_path / "history.db")
    sync = KISUSMinuteHistorySync(  # type: ignore[arg-type]
        FakeHistoryClient(), store, request_delay_seconds=0.05
    )
    session_date = date(2026, 7, 15)
    _, close = session_bounds(Market.US, session_date)

    result = await sync.sync_symbol("NVDA", "NASDAQ", sessions=1, now=close)

    assert result.ready is True
    assert result.complete_sessions == 1
    assert result.saved_bars == 390
    assert len(store.load(Market.US, "NVDA")) == 390
