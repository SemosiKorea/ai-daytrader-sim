from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daytrader.broker import PaperBroker
from daytrader.config import CostConfig
from daytrader.models import CandidatePlan, Market, MarketTick, Predicate, RuleGroup
from daytrader.repository import Repository
from daytrader.rules import evaluate_group


def _tick(
    price: float,
    *,
    timestamp: datetime | None = None,
    ask_size: int = 10,
    bid: float | None = None,
    ask: float | None = None,
) -> MarketTick:
    timestamp = timestamp or datetime.now(UTC)
    return MarketTick(
        market=Market.US,
        symbol="NVDA",
        timestamp=timestamp,
        source_timestamp=timestamp,
        received_timestamp=timestamp,
        sequence_id=int(timestamp.timestamp() * 1000),
        connection_id="test",
        data_source="test",
        quote_scope="consolidated",
        session="regular",
        market_status="open",
        symbol_status="trading",
        luld_status="normal",
        last=price,
        bid=bid if bid is not None else price - 0.01,
        ask=ask if ask is not None else price,
        ask_size=ask_size,
        trade_size=100,
    )


def test_cross_above_uses_previous_context() -> None:
    group = RuleGroup(
        predicates=[Predicate(indicator="last", operator="cross_above", value="vwap_regular")]
    )
    assert evaluate_group(
        group,
        {"last": 101, "vwap_regular": 100},
        {"last": 99, "vwap_regular": 100},
    )
    assert not evaluate_group(group, {"last": 101, "vwap_regular": 100}, None)


def test_paper_fill_targets_and_restart_persistence(tmp_path) -> None:
    repository = Repository(tmp_path / "broker.db")
    costs = {"US": CostConfig(1_500, 0, 0, 0, 0)}
    broker = PaperBroker(repository, costs)
    candidate = CandidatePlan.model_validate(
        {
            "symbol": "NVDA",
            "exchange": "NASDAQ",
            "reason": "Conservative partial fill test candidate.",
            "entry": {
                "trigger_price": 100,
                "limit_price": 100,
                "start_time": "09:40:00",
                "end_time": "11:00:00",
                "price_only": True,
                "rules": {"mode": "all", "predicates": [], "groups": []},
            },
            "stop_loss": {"price": 99},
            "take_profit": [
                {"price": 102, "quantity_pct": 50},
                {"price": 104, "quantity_pct": 50},
            ],
            "force_exit_time": "15:50:00",
        }
    )
    started = datetime.now(UTC)
    order, reason = broker.submit_entry(
        "US_test_plan",
        candidate,
        _tick(100, timestamp=started, ask_size=10),
    )
    assert reason is None
    assert order is not None
    assert order.desired_quantity == 5
    assert not broker.portfolios[Market.US].positions

    broker.process_pending(_tick(100, timestamp=started + timedelta(milliseconds=100)))
    assert not broker.portfolios[Market.US].positions
    broker.process_pending(
        _tick(
            100,
            timestamp=started + timedelta(milliseconds=350),
            bid=100,
            ask=100,
        )
    )
    assert not broker.portfolios[Market.US].positions
    broker.process_pending(
        _tick(100, timestamp=started + timedelta(milliseconds=400), ask_size=2)
    )
    assert broker.portfolios[Market.US].positions["NVDA"].quantity == 2
    assert broker.portfolios[Market.US].pending_orders["NVDA"].filled_quantity == 2
    broker.process_pending(
        _tick(100, timestamp=started + timedelta(milliseconds=500), ask_size=10)
    )
    assert broker.portfolios[Market.US].positions["NVDA"].quantity == 5
    assert not broker.portfolios[Market.US].pending_orders

    broker.on_tick(
        _tick(102, timestamp=started + timedelta(seconds=1), bid=102, ask=102.01)
    )
    assert broker.portfolios[Market.US].positions["NVDA"].remaining == 5
    broker.on_tick(
        _tick(102, timestamp=started + timedelta(seconds=1.4), bid=102, ask=102.01)
    )
    assert broker.portfolios[Market.US].positions["NVDA"].remaining == 2
    broker.on_tick(
        _tick(104, timestamp=started + timedelta(seconds=2), bid=104, ask=104.01)
    )
    assert broker.portfolios[Market.US].positions["NVDA"].remaining == 2
    broker.on_tick(
        _tick(104, timestamp=started + timedelta(seconds=2.4), bid=104, ask=104.01)
    )
    assert not broker.portfolios[Market.US].positions
    assert broker.performance(Market.US)["closed_trades"] == 1

    restored = PaperBroker(repository, costs)
    assert restored.portfolios[Market.US].cash == broker.portfolios[Market.US].cash
    assert restored.portfolios[Market.US].realized_pnl == 14.0
