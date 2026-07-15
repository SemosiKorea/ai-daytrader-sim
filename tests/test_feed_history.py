from __future__ import annotations

from datetime import date, timedelta

import pytest

from daytrader.feed_history import FeedHistoryStore, IndicatorCalculator, MinuteBar
from daytrader.market_clock import is_session, session_bounds
from daytrader.models import Market


def _prior_sessions(market: Market, current: date, count: int) -> list[date]:
    sessions = []
    cursor = current - timedelta(days=1)
    while len(sessions) < count:
        if is_session(market, cursor):
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(sessions))


def test_indicator_calculator_warms_from_twenty_sessions(tmp_path) -> None:
    store = FeedHistoryStore(tmp_path / "feed.db")
    market = Market.US
    symbol = "NVDA"
    trade_date = date(2026, 7, 15)
    for day_index, session_date in enumerate(_prior_sessions(market, trade_date, 20)):
        session_open, _ = session_bounds(market, session_date)
        for minute in range(60):
            start = session_open + timedelta(minutes=minute)
            price = 100 + day_index * 0.1 + minute * 0.01
            store.save(
                MinuteBar(
                    market=market,
                    symbol=symbol,
                    start=start,
                    open=price,
                    high=price + 0.05,
                    low=price - 0.05,
                    close=price + 0.01,
                    volume=100,
                    notional=price * 100,
                )
            )

    calculator = IndicatorCalculator(market, symbol, store)
    session_open, _ = session_bounds(market, trade_date)
    for minute in range(6):
        calculator.on_trade(105 + minute * 0.1, 100, session_open + timedelta(minutes=minute))

    indicators, ready, timestamps = calculator.snapshot(
        session_open + timedelta(minutes=5, seconds=1)
    )

    assert ready["vwap_regular"] is True
    assert indicators["vwap_regular"] == pytest.approx(105.25)
    assert ready["ema_50_1m_regular"] is True
    assert ready["rsi_14_1m_regular"] is True
    assert ready["atr_14_1m_regular"] is True
    assert ready["opening_range_5_high"] is True
    assert indicators["opening_range_5_high"] == pytest.approx(105.4)
    assert ready["recent_high_5_1m_regular"] is True
    assert ready["previous_close"] is True
    assert ready["relative_volume_cumulative_20d_same_time_regular"] is True
    assert indicators["relative_volume_cumulative_20d_same_time_regular"] == pytest.approx(1.0)
    assert timestamps["bar_volume_1m_regular"].tzinfo is not None


def test_relative_volume_stays_unready_without_twenty_sessions(tmp_path) -> None:
    store = FeedHistoryStore(tmp_path / "feed.db")
    calculator = IndicatorCalculator(Market.KR, "005930", store)
    session_open, _ = session_bounds(Market.KR, date(2026, 7, 15))
    calculator.on_trade(70_000, 10, session_open)

    indicators, ready, _ = calculator.snapshot(session_open)

    assert ready["relative_volume_cumulative_20d_same_time_regular"] is False
    assert indicators["relative_volume_cumulative_20d_same_time_regular"] == 0.0


def test_late_start_does_not_claim_opening_range_or_gap_ready(tmp_path) -> None:
    store = FeedHistoryStore(tmp_path / "feed.db")
    market = Market.US
    symbol = "NVDA"
    trade_date = date(2026, 7, 15)
    previous_date = _prior_sessions(market, trade_date, 1)[0]
    previous_open, _ = session_bounds(market, previous_date)
    store.save(
        MinuteBar(
            market=market,
            symbol=symbol,
            start=previous_open,
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=100,
            notional=10_050,
        )
    )
    calculator = IndicatorCalculator(market, symbol, store)
    session_open, _ = session_bounds(market, trade_date)
    late = session_open + timedelta(minutes=4)
    calculator.on_trade(105, 100, late)

    indicators, ready, _ = calculator.snapshot(session_open + timedelta(minutes=5))

    assert ready["opening_range_5_high"] is False
    assert ready["gap_pct"] is False
    assert indicators["gap_pct"] == 0.0
