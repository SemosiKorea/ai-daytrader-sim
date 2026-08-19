from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .feed_history import FeedHistoryStore, MinuteBar
from .kis_readonly import KISReadOnlyClient
from .market_clock import MARKET_TZ, is_session, session_bounds
from .models import Market

logger = logging.getLogger(__name__)

HISTORY_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
HISTORY_TR_ID = "HHDFS76950200"
EXCHANGE_CODES = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


@dataclass(frozen=True, slots=True)
class HistorySyncResult:
    symbol: str
    requested_sessions: int
    complete_sessions: int
    saved_bars: int
    incomplete_sessions: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.complete_sessions >= self.requested_sessions


def completed_session_dates(
    market: Market, now: datetime, count: int
) -> list[date]:
    """Return the latest completed official sessions, oldest first."""
    if count < 1:
        raise ValueError("session count must be positive")
    local_now = now.astimezone(MARKET_TZ[market])
    cursor = local_now.date()
    values = []
    while len(values) < count:
        if is_session(market, cursor):
            _, close = session_bounds(market, cursor)
            if close <= local_now:
                values.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(values))


def _number(row: dict[str, Any], key: str) -> float:
    try:
        return float(str(row.get(key, "")).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid KIS history field {key}") from exc


def parse_kis_us_minute_bar(symbol: str, row: dict[str, Any]) -> MinuteBar:
    day = "".join(value for value in str(row.get("xymd", "")) if value.isdigit())
    clock = "".join(value for value in str(row.get("xhms", "")) if value.isdigit())
    if len(day) != 8 or len(clock) != 6:
        raise ValueError("KIS history row has an invalid local timestamp")
    start = datetime.strptime(day + clock, "%Y%m%d%H%M%S").replace(
        tzinfo=MARKET_TZ[Market.US]
    )
    open_price = _number(row, "open")
    high = _number(row, "high")
    low = _number(row, "low")
    close = _number(row, "last")
    volume_value = _number(row, "evol")
    notional = _number(row, "eamt")
    if not all(
        math.isfinite(value)
        for value in (open_price, high, low, close, volume_value, notional)
    ):
        raise ValueError("KIS history row contains a non-finite number")
    if min(open_price, high, low, close) <= 0:
        raise ValueError("KIS history row contains a non-positive price")
    if not low <= min(open_price, close) <= max(open_price, close) <= high:
        raise ValueError("KIS history OHLC values are inconsistent")
    if volume_value < 0 or not volume_value.is_integer() or notional < 0:
        raise ValueError("KIS history volume or notional is invalid")
    return MinuteBar(
        market=Market.US,
        symbol=symbol.upper(),
        start=start,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=int(volume_value),
        notional=notional,
    )


class KISUSMinuteHistorySync:
    """Read-only importer for KIS overseas regular-session one-minute bars."""

    def __init__(
        self,
        client: KISReadOnlyClient,
        store: FeedHistoryStore,
        *,
        request_delay_seconds: float = 0.11,
    ):
        self.client = client
        self.store = store
        self.request_delay_seconds = max(request_delay_seconds, 0.05)

    async def _page(
        self, symbol: str, exchange_code: str, keyb: str
    ) -> list[dict[str, Any]]:
        payload = await self.client.get_market_data(
            HISTORY_PATH,
            tr_id=HISTORY_TR_ID,
            params={
                "AUTH": "",
                "EXCD": exchange_code,
                "SYMB": symbol.upper(),
                "NMIN": "1",
                "PINC": "1",
                "NEXT": "1",
                "NREC": "120",
                "FILL": "",
                "KEYB": keyb,
            },
        )
        if str(payload.get("rt_cd", "0")) != "0":
            raise RuntimeError(
                f"KIS history rejected {symbol}: {payload.get('msg_cd')} "
                f"{payload.get('msg1')}"
            )
        rows = payload.get("output2") or []
        if not isinstance(rows, list):
            raise ValueError("KIS history output2 is not a list")
        await asyncio.sleep(self.request_delay_seconds)
        return [row for row in rows if isinstance(row, dict)]

    async def _session_bars(
        self, symbol: str, exchange_code: str, session_date: date
    ) -> list[MinuteBar]:
        session_open, session_close = session_bounds(Market.US, session_date)
        expected = int((session_close - session_open).total_seconds() // 60)
        key_time = (session_close - timedelta(minutes=1)).astimezone(MARKET_TZ[Market.US])
        pages = math.ceil(expected / 120)
        bars: dict[datetime, MinuteBar] = {}
        for _ in range(pages):
            rows = await self._page(
                symbol,
                exchange_code,
                key_time.strftime("%Y%m%d%H%M%S"),
            )
            if not rows:
                break
            parsed_times = []
            for row in rows:
                try:
                    bar = parse_kis_us_minute_bar(symbol, row)
                except ValueError as exc:
                    logger.warning("discarding invalid KIS bar for %s: %s", symbol, exc)
                    continue
                parsed_times.append(bar.start)
                if session_open <= bar.start < session_close:
                    bars[bar.start] = bar
            if not parsed_times:
                break
            key_time = min(parsed_times) - timedelta(minutes=1)
            if key_time < session_open:
                break
        return [bars[start] for start in sorted(bars)]

    async def sync_symbol(
        self,
        symbol: str,
        exchange: str,
        *,
        sessions: int = 20,
        now: datetime | None = None,
    ) -> HistorySyncResult:
        exchange_code = EXCHANGE_CODES.get(exchange.upper())
        if exchange_code is None:
            raise ValueError(f"unsupported KIS US exchange: {exchange}")
        now = now or datetime.now().astimezone()
        dates = completed_session_dates(Market.US, now, sessions)
        complete = 0
        saved = 0
        incomplete = []
        for index, session_date in enumerate(dates, start=1):
            bars = await self._session_bars(symbol.upper(), exchange_code, session_date)
            session_open, session_close = session_bounds(Market.US, session_date)
            expected_starts = {
                session_open + timedelta(minutes=offset)
                for offset in range(int((session_close - session_open).total_seconds() // 60))
            }
            observed_starts = {bar.start for bar in bars}
            if expected_starts == observed_starts:
                complete += 1
            else:
                incomplete.append(
                    f"{session_date.isoformat()}:{len(observed_starts)}/{len(expected_starts)}"
                )
            saved += self.store.save_many(bars)
            logger.info(
                "KIS history %s session %d/%d %s bars=%d complete=%s",
                symbol.upper(),
                index,
                len(dates),
                session_date.isoformat(),
                len(bars),
                expected_starts == observed_starts,
            )
        return HistorySyncResult(
            symbol=symbol.upper(),
            requested_sessions=sessions,
            complete_sessions=complete,
            saved_bars=saved,
            incomplete_sessions=tuple(incomplete),
        )
