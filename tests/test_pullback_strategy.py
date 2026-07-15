from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daytrader.models import CandidatePlan, Market, MarketTick, Predicate
from daytrader.pullback import PullbackRebreakEngine
from daytrader.repository import Repository
from daytrader.rules import evaluate_predicate


def pullback_candidate() -> CandidatePlan:
    return CandidatePlan.model_validate(
        {
            "symbol": "NVDA",
            "exchange": "NASDAQ",
            "reason": "State-based pullback and rebreak strategy test.",
            "strategy_type": "pullback_rebreak",
            "entry": {
                "trigger_price": 100.1,
                "limit_price": 100.2,
                "start_time": "09:40:00",
                "end_time": "11:30:00",
                "price_only": False,
                "rules": {"mode": "all", "predicates": [], "groups": []},
            },
            "stop_loss": {"price": 99.0},
            "take_profit": [{"price": 102.1, "quantity_pct": 100}],
            "force_exit_time": "15:50:00",
            "pullback_rebreak": {
                "breakout_confirm_ticks": 3,
                "breakout_hold_ms": 1000,
                "pullback_depth_atr_min": 0.2,
                "pullback_depth_atr_max": 0.8,
                "pullback_min_minutes": 2,
                "pullback_max_minutes": 10,
            },
        }
    )


def strategy_tick(
    at: datetime,
    price: float,
    *,
    rsi: float = 58,
    relative_volume: float = 1.6,
    bar_volume: float = 1000,
    recent_high: float | None = None,
    bar_timestamp: datetime | None = None,
) -> MarketTick:
    indicators = {
        "opening_range_5_high": 100.0,
        "vwap_regular": 99.9,
        "atr_14_1m_regular": 1.0,
        "ema_9_1m_regular": 100.1,
        "ema_20_1m_regular": 99.8,
        "ema_50_1m_regular": 99.5,
        "rsi_14_1m_regular": rsi,
        "relative_volume_cumulative_20d_same_time_regular": relative_volume,
        "market_above_vwap_regular": True,
        "recent_high_5_1m_regular": recent_high or price,
        "recent_low_5_1m_regular": min(price, 99.9),
        "bar_volume_1m_regular": bar_volume,
    }
    timestamps = {name: bar_timestamp or at for name in indicators}
    return MarketTick(
        market=Market.US,
        symbol="NVDA",
        timestamp=at,
        source_timestamp=at,
        received_timestamp=at,
        sequence_id=int(at.timestamp() * 1000),
        connection_id="pullback-test-feed",
        data_source="test",
        quote_scope="consolidated",
        session="regular",
        market_status="open",
        symbol_status="trading",
        luld_status="normal",
        last=price,
        bid=price - 0.01,
        ask=price + 0.01,
        ask_size=100,
        trade_size=100,
        indicators=indicators,
        indicator_ready={name: True for name in indicators},
        indicator_timestamps=timestamps,
    )


def context(tick: MarketTick) -> dict:
    return {"last": tick.last, **tick.indicators}


def test_pullback_state_machine_persists_and_signals_rebreak(tmp_path) -> None:
    repository = Repository(tmp_path / "pullback.db")
    candidate = pullback_candidate()
    plan_id = "US_pullback_001"
    start = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    engine = PullbackRebreakEngine(repository)

    first = strategy_tick(start, 100.2, bar_timestamp=start)
    decision = engine.evaluate(plan_id, candidate, first, {"last": 99.9})
    assert decision.phase == "WAIT_BREAKOUT"
    second = strategy_tick(start + timedelta(seconds=1), 100.22, bar_timestamp=start)
    engine.evaluate(plan_id, candidate, second, context(first))
    third = strategy_tick(start + timedelta(seconds=2), 100.25, bar_timestamp=start)
    decision = engine.evaluate(plan_id, candidate, third, context(second))
    assert decision.phase == "WAIT_PULLBACK"
    assert decision.metrics["prior_breakout_confirmed"] is True

    restored = PullbackRebreakEngine(repository)
    impulse = strategy_tick(start + timedelta(seconds=3), 100.3, bar_timestamp=start)
    decision = restored.evaluate(plan_id, candidate, impulse, context(third))
    assert decision.phase == "WAIT_PULLBACK"
    assert decision.metrics["impulse_high"] == 100.3

    pullback_start = strategy_tick(
        start + timedelta(minutes=1),
        100.0,
        rsi=55,
        bar_volume=500,
        recent_high=100.15,
    )
    decision = restored.evaluate(plan_id, candidate, pullback_start, context(impulse))
    assert decision.phase == "WAIT_PULLBACK"
    assert 0.2 <= decision.metrics["pullback_depth_atr"] <= 0.8

    confirmed = strategy_tick(
        start + timedelta(minutes=3),
        100.0,
        rsi=55,
        bar_volume=500,
        recent_high=100.15,
    )
    decision = restored.evaluate(plan_id, candidate, confirmed, context(pullback_start))
    assert decision.phase == "WAIT_REBREAK"
    assert decision.metrics["pullback_high"] == 100.15
    assert decision.metrics["pullback_volume_ratio"] == 0.5

    rebreak = strategy_tick(
        start + timedelta(minutes=3, seconds=1),
        100.2,
        rsi=60,
        relative_volume=1.4,
        bar_volume=800,
        recent_high=100.15,
        bar_timestamp=confirmed.indicator_timestamps["bar_volume_1m_regular"],
    )
    decision = restored.evaluate(plan_id, candidate, rebreak, context(confirmed))
    assert decision.signal is True
    assert decision.phase == "SIGNAL_TRIGGERED"


def test_boolean_eq_and_ne_operators() -> None:
    assert evaluate_predicate(
        Predicate(indicator="market_above_vwap_regular", operator="eq", value=True),
        {"market_above_vwap_regular": True},
    )
    assert evaluate_predicate(
        Predicate(indicator="market_above_vwap_regular", operator="ne", value=False),
        {"market_above_vwap_regular": True},
    )
