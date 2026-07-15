from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from daytrader.kis_readonly import (
    KISQuotePoller,
    KISReadOnlyClient,
    LiveOrderCapabilityDisabled,
)
from daytrader.models import Market, MarketTick


@pytest.mark.asyncio
async def test_live_orders_are_impossible() -> None:
    client = KISReadOnlyClient("key", "secret")
    with pytest.raises(LiveOrderCapabilityDisabled, match="record_only"):
        await client.place_order(symbol="005930", quantity=1)


@pytest.mark.asyncio
async def test_non_market_data_paths_are_blocked_before_network() -> None:
    client = KISReadOnlyClient("key", "secret")
    with pytest.raises(LiveOrderCapabilityDisabled, match="not an allowed"):
        await client.get_market_data(
            "/uapi/domestic-stock/v1/trading/order-cash", tr_id="x", params={}
        )


@pytest.mark.asyncio
async def test_rest_poller_fans_experiment_ticks_to_both_engines() -> None:
    candidate = SimpleNamespace(symbol="005930", exchange="KRX")

    class FakeRepository:
        def active_plans(self, *_):
            return []

        def active_portfolio_experiments(self, market, *_):
            return [SimpleNamespace(candidates=[candidate])] if market == Market.KR else []

    class FakeClient:
        async def quote(self, market, symbol, exchange):
            now = datetime.now(UTC)
            return MarketTick(
                market=market,
                symbol=symbol,
                timestamp=now,
                source_timestamp=now,
                received_timestamp=now,
                sequence_id=1,
                connection_id="rest-test",
                data_source="TEST",
                quote_scope="consolidated",
                session="regular",
                market_status="open",
                symbol_status="trading",
                luld_status="normal",
                last=100,
                bid=99.9,
                ask=100,
            )

    class TickReceiver:
        def __init__(self):
            self.symbols = []

        def process_tick(self, tick):
            self.symbols.append(tick.symbol)

    engine = TickReceiver()
    experiments = TickReceiver()
    poller = KISQuotePoller(
        FakeClient(),
        FakeRepository(),
        engine,
        {},
        experiment_manager=experiments,
    )

    await poller.poll_once()

    assert engine.symbols == ["005930"]
    assert experiments.symbols == ["005930"]
