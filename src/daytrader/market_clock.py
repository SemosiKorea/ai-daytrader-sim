from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from .models import Market


MARKET_TZ = {Market.KR: ZoneInfo("Asia/Seoul"), Market.US: ZoneInfo("America/New_York")}
CALENDAR_NAME = {Market.KR: "XKRX", Market.US: "XNYS"}
FORCE_EXIT_OFFSET = {Market.KR: timedelta(minutes=15), Market.US: timedelta(minutes=10)}


def calendar_for(market: Market):
    return xcals.get_calendar(CALENDAR_NAME[market])


def is_session(market: Market, trade_date: date) -> bool:
    return bool(calendar_for(market).is_session(trade_date.isoformat()))


def session_bounds(market: Market, trade_date: date) -> tuple[datetime, datetime]:
    calendar = calendar_for(market)
    label = trade_date.isoformat()
    if not calendar.is_session(label):
        raise ValueError(f"{trade_date} is not an exchange session for {market.value}")
    return calendar.session_open(label).to_pydatetime(), calendar.session_close(label).to_pydatetime()


def force_exit_at(market: Market, trade_date: date) -> datetime:
    _, close = session_bounds(market, trade_date)
    return (close - FORCE_EXIT_OFFSET[market]).astimezone(MARKET_TZ[market])


def regular_session_open(market: Market, trade_date: date) -> datetime:
    open_at, _ = session_bounds(market, trade_date)
    return open_at.astimezone(MARKET_TZ[market])
