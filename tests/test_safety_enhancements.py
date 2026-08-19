from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from daytrader.broker import PaperBroker
from daytrader.config import CostConfig
from daytrader.engine import FeedState, TradingEngine
from daytrader.market_clock import force_exit_at
from daytrader.models import (
    CandidatePlan,
    Market,
    MarketTick,
    OrderState,
    Predicate,
    RuleGroup,
    TradePlan,
)
from daytrader.repository import Repository
from daytrader.rules import CrossDebounceState, evaluate_group


def candidate(symbol: str = "NVDA") -> CandidatePlan:
    return CandidatePlan.model_validate(
        {
            "symbol": symbol,
            "exchange": "NASDAQ",
            "reason": "A conservative state-machine test candidate.",
            "entry": {
                "trigger_price": 100,
                "limit_price": 100,
                "start_time": "09:40:00",
                "end_time": "11:00:00",
                "price_only": True,
                "rules": {"mode": "all", "predicates": [], "groups": []},
            },
            "stop_loss": {
                "price": 99,
                "limit_offset_pct": 0.2,
                "emergency_exit_after_sec": 2,
            },
            "take_profit": [
                {"price": 102, "quantity_pct": 50},
                {"price": 104, "quantity_pct": 50},
            ],
            "force_exit_time": "15:50:00",
        }
    )


def tick(
    at: datetime,
    *,
    price: float = 100,
    bid: float | None = None,
    ask: float | None = None,
    ask_size: int = 10,
    sequence: int | None = None,
    symbol: str = "NVDA",
    **updates,
) -> MarketTick:
    return MarketTick(
        market=Market.US,
        symbol=symbol,
        timestamp=at,
        source_timestamp=at,
        received_timestamp=at,
        sequence_id=sequence if sequence is not None else int(at.timestamp() * 1000),
        connection_id="test-feed",
        data_source="test",
        quote_scope="consolidated",
        session=updates.pop("session", "regular"),
        market_status=updates.pop("market_status", "open"),
        symbol_status=updates.pop("symbol_status", "trading"),
        luld_status=updates.pop("luld_status", "normal"),
        last=price,
        bid=bid if bid is not None else price - 0.01,
        ask=ask if ask is not None else price,
        ask_size=ask_size,
        trade_size=100,
        **updates,
    )


def test_pending_order_reserves_slot_expires_and_is_idempotent(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    now = datetime.now(UTC)
    order, reason = broker.submit_entry("US_plan_001", candidate(), tick(now))
    assert order and reason is None

    second, reason = broker.submit_entry(
        "US_plan_002", candidate("AMD"), tick(now, symbol="AMD")
    )
    assert second is None
    assert reason == "POSITION_SLOT_OCCUPIED"

    broker.expire_pending(now + timedelta(seconds=6))
    assert not broker.portfolios[Market.US].pending_orders
    retry, reason = broker.submit_entry("US_plan_001", candidate(), tick(now + timedelta(seconds=7)))
    assert retry is None
    assert reason == "ORDER_ALREADY_SUBMITTED"
    states = [
        json.loads(event["payload"])["state"]
        for event in repository.recent_events(20)
        if event["event_type"] == "ORDER_STATE_CHANGED"
    ]
    assert OrderState.EXPIRED.value in states


def test_stop_uses_bid_and_gap_waits_for_emergency_exit(tmp_path) -> None:
    repository = Repository(tmp_path / "stop.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    now = datetime.now(UTC)
    order, _ = broker.submit_entry("US_plan_stop", candidate(), tick(now))
    assert order
    broker.process_pending(tick(now + timedelta(milliseconds=400)))
    position = broker.portfolios[Market.US].positions["NVDA"]

    gap = tick(
        now + timedelta(seconds=1), price=98.6, bid=98.5, ask=98.6, ask_size=100
    )
    broker.on_tick(gap)
    assert position.exit_pending_at is not None
    assert "NVDA" in broker.portfolios[Market.US].positions

    emergency = tick(
        now + timedelta(seconds=3.1), price=98.1, bid=98.0, ask=98.1, ask_size=100
    )
    broker.on_tick(emergency)
    assert not broker.portfolios[Market.US].positions
    sell = next(
        event
        for event in repository.recent_events(20)
        if event["event_type"] == "PAPER_SELL_FILLED"
    )
    assert json.loads(sell["payload"])["price"] == 98.0


def test_force_close_rejects_stale_cached_quote(tmp_path) -> None:
    repository = Repository(tmp_path / "force-close.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    now = datetime.now(UTC)
    order, _ = broker.submit_entry("US_plan_force", candidate(), tick(now))
    assert order
    broker.process_pending(tick(now + timedelta(milliseconds=400)))
    stale = tick(now - timedelta(seconds=10))
    engine.latest_ticks[(Market.US, "NVDA")] = stale

    result = engine.force_close_market(Market.US, now=now)

    assert result["unpriced_symbols"] == ["NVDA"]
    assert "NVDA" in broker.portfolios[Market.US].positions


def test_performance_drawdown_includes_open_position_equity(tmp_path) -> None:
    repository = Repository(tmp_path / "equity.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    now = datetime.now(UTC)
    order, _ = broker.submit_entry("US_plan_equity", candidate(), tick(now))
    assert order
    broker.process_pending(tick(now + timedelta(milliseconds=400)))

    broker.on_tick(
        tick(now + timedelta(seconds=1), price=90.1, bid=90.0, ask=90.1)
    )

    performance = broker.performance(Market.US)
    assert performance["closed_trades"] == 0
    assert performance["net_pnl"] < 0
    assert performance["max_drawdown_pct"] > 0


def test_take_profit_exit_uses_bid_size_and_partial_fill_state(tmp_path) -> None:
    repository = Repository(tmp_path / "partial-exit.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    now = datetime.now(UTC)
    order, _ = broker.submit_entry("US_plan_partial_exit", candidate(), tick(now))
    assert order
    broker.process_pending(tick(now + timedelta(milliseconds=400), ask_size=100))
    assert broker.portfolios[Market.US].positions["NVDA"].remaining == 5

    broker.on_tick(
        tick(now + timedelta(seconds=1), price=102.1, bid=102, ask=102.1)
    )
    broker.on_tick(
        tick(
            now + timedelta(seconds=1.4),
            price=102.1,
            bid=102,
            ask=102.1,
            bid_size=1,
        )
    )

    position = broker.portfolios[Market.US].positions["NVDA"]
    assert position.remaining == 4
    assert position.exit_remaining_quantity == 2
    states = [
        json.loads(event["payload"])["state"]
        for event in repository.recent_events(20)
        if event["event_type"] == "ORDER_STATE_CHANGED"
    ]
    assert OrderState.PARTIALLY_FILLED.value in states


def test_sequence_and_crossed_quotes_are_rejected(tmp_path) -> None:
    repository = Repository(tmp_path / "feed.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    now = datetime.now(UTC)
    assert engine.process_tick(tick(now, sequence=10))["accepted"]
    duplicate = engine.process_tick(tick(now + timedelta(milliseconds=1), sequence=10))
    assert not duplicate["accepted"]
    assert "SEQUENCE_REVERSED" in duplicate["reasons"]

    crossed = engine.process_tick(
        tick(
            now + timedelta(milliseconds=2),
            sequence=11,
            bid=100.2,
            ask=100.1,
        )
    )
    assert not crossed["accepted"]
    assert "CROSSED_MARKET" in crossed["reasons"]


def test_identical_stale_data_rejections_are_aggregated(tmp_path) -> None:
    repository = Repository(tmp_path / "stale-aggregate.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    stale_at = datetime.now(UTC) - timedelta(seconds=10)

    for sequence in range(100):
        result = engine.process_tick(
            tick(stale_at + timedelta(milliseconds=sequence), sequence=sequence + 1)
        )
        assert result == {"accepted": False, "reasons": ["STALE_DATA"]}

    rejection_events = [
        event
        for event in repository.recent_events(200)
        if event["event_type"].startswith("DATA_REJECTED")
    ]
    assert len(rejection_events) == 1
    payload = json.loads(rejection_events[0]["payload"])
    assert payload["details"]["aggregation"] == "initial"
    aggregate = engine.rejection_aggregates[(Market.US, "NVDA", ("STALE_DATA",))]
    assert aggregate.count == 100

    recovered = engine.process_tick(tick(datetime.now(UTC), sequence=101))
    assert recovered["accepted"]
    rejection_events = [
        event
        for event in repository.recent_events(200)
        if event["event_type"].startswith("DATA_REJECTED")
    ]
    assert len(rejection_events) == 2
    summary = json.loads(rejection_events[0]["payload"])
    assert summary["details"]["aggregate_count"] == 100
    assert summary["details"]["aggregation"] == "flushed_on_recovery"
    assert not engine.rejection_aggregates


def test_halt_cancels_pending_order_and_resets_entry_state(tmp_path) -> None:
    repository = Repository(tmp_path / "halt.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    now = datetime.now(UTC)
    order, _ = broker.submit_entry("US_plan_halt", candidate(), tick(now))
    assert order
    halted = tick(
        now + timedelta(milliseconds=100),
        sequence=2,
        market_status="halted",
        symbol_status="halted",
        luld_status="paused",
    )
    result = engine.process_tick(halted)
    assert not result["accepted"]
    assert not broker.portfolios[Market.US].pending_orders
    state_events = [
        json.loads(event["payload"])
        for event in repository.recent_events(20)
        if event["event_type"] == "ORDER_STATE_CHANGED"
    ]
    assert any(event["state"] == OrderState.MARKET_HALTED.value for event in state_events)


def test_cross_debounce_requires_ticks_hold_and_margin() -> None:
    predicate = Predicate(
        indicator="last",
        operator="cross_above",
        value="vwap_regular",
        confirm_ticks=3,
        hold_above_ms=2000,
        minimum_cross_pct=0.05,
        cooldown_sec=60,
    )
    group = RuleGroup(predicates=[predicate])
    states: dict[str, CrossDebounceState] = {}
    now = datetime.now(UTC)
    previous = {"last": 99.9, "vwap_regular": 100.0}
    current = {"last": 100.06, "vwap_regular": 100.0}
    assert not evaluate_group(group, current, previous, states=states, evaluated_at=now)
    assert not evaluate_group(
        group,
        current,
        current,
        states=states,
        evaluated_at=now + timedelta(seconds=1),
    )
    assert evaluate_group(
        group,
        current,
        current,
        states=states,
        evaluated_at=now + timedelta(seconds=2),
    )


def test_us_early_close_uses_official_calendar() -> None:
    exit_at = force_exit_at(Market.US, date(2026, 11, 27))
    assert exit_at.hour == 12
    assert exit_at.minute == 50


def _plan_with(candidate_plan: CandidatePlan, at: datetime) -> TradePlan:
    return TradePlan(
        plan_id="US_guard_plan_001",
        market=Market.US,
        trade_date=at.date(),
        expires_at=at + timedelta(hours=8),
        approval_nonce="000000",
        approved_symbols=[candidate_plan],
    )


def test_opening_deviation_persistently_blocks_candidate(tmp_path) -> None:
    repository = Repository(tmp_path / "guard.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    at = datetime.now(UTC)
    guarded = CandidatePlan.model_validate(
        {
            **candidate().model_dump(),
            "premarket_guard": {
                "reference_price": 100,
                "max_open_deviation_pct": 1,
            },
        }
    )
    plan = _plan_with(guarded, at)
    current = tick(
        at,
        price=102,
        indicators={"regular_open_price": 102.0},
        indicator_ready={"regular_open_price": True},
        indicator_timestamps={"regular_open_price": at},
    )

    reasons = engine._premarket_guard_reasons(plan, guarded, current)

    assert reasons == ["OPEN_DEVIATION_EXCEEDED"]
    state = repository.candidate_guard_state(plan.plan_id, guarded.symbol)
    assert state and state["reason"] == "OPEN_DEVIATION_EXCEEDED"
    assert engine._premarket_guard_reasons(plan, guarded, current)[0].startswith(
        "CANDIDATE_RISK_BLOCKED"
    )


def test_engine_does_not_submit_when_ask_exceeds_maximum_limit(tmp_path) -> None:
    repository = Repository(tmp_path / "limit.db")
    broker = PaperBroker(repository, {"US": CostConfig(1_500, 0, 0, 0, 0)})
    engine = TradingEngine(repository, broker)
    at = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    plan = _plan_with(candidate(), at)
    current = tick(at, price=100, bid=100, ask=100.1)
    feed = FeedState(at, at, 1, "test-feed", "regular")

    engine._try_entry(plan, candidate(), current, feed)

    assert broker.portfolios[Market.US].pending_orders == {}
    rejection = repository.recent_events(1)[0]
    assert "MAX_BUY_LIMIT_EXCEEDED" in json.loads(rejection["payload"])["reasons"]
