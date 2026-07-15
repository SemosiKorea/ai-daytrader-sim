from __future__ import annotations

import pytest

from daytrader.kis_readonly import KISReadOnlyClient, LiveOrderCapabilityDisabled


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
