from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from daytrader.broker import PaperBroker
from daytrader.config import CostConfig, Settings
from daytrader.kis_orders import (
    KISOrderAction,
    KISOrderIntent,
    KISOrderIntentRecorder,
    KISOrderRequestBuilder,
    KISOrderSafetyError,
    KISOrderSide,
    KISOrderTransport,
)
from daytrader.models import CandidatePlan, Market, MarketTick
from daytrader.repository import Repository


def _intent(
    market: Market = Market.US,
    exchange: str = "NASDAQ",
    side: KISOrderSide = KISOrderSide.BUY,
) -> KISOrderIntent:
    return KISOrderIntent(
        idempotency_key="US_plan_NVDA_ENTRY",
        source_order_id="paper-order-1",
        plan_id="US_plan_001",
        market=market,
        symbol="NVDA" if market == Market.US else "005930",
        exchange=exchange,
        action=KISOrderAction.NEW,
        side=side,
        quantity=3,
        limit_price=180.25 if market == Market.US else 70_000,
        reason="ENTRY_SIGNAL",
    )


def _candidate() -> CandidatePlan:
    return CandidatePlan.model_validate(
        {
            "symbol": "NVDA",
            "exchange": "NASDAQ",
            "reason": "Record-only KIS order intent integration test.",
            "entry": {
                "trigger_price": 100,
                "limit_price": 100,
                "start_time": "09:40:00",
                "end_time": "11:00:00",
                "price_only": True,
                "rules": {"mode": "all", "predicates": [], "groups": []},
            },
            "stop_loss": {"price": 99},
            "take_profit": [{"price": 102, "quantity_pct": 100}],
            "force_exit_time": "15:50:00",
        }
    )


def _tick(at: datetime, price: float = 100) -> MarketTick:
    return MarketTick(
        market=Market.US,
        symbol="NVDA",
        timestamp=at,
        source_timestamp=at,
        received_timestamp=at,
        sequence_id=int(at.timestamp() * 1000),
        connection_id="test",
        data_source="test",
        quote_scope="consolidated",
        session="regular",
        market_status="open",
        symbol_status="trading",
        luld_status="normal",
        last=price,
        bid=price - 0.01,
        ask=price,
        ask_size=10,
        trade_size=100,
    )


def test_builds_official_domestic_and_us_order_contracts() -> None:
    domestic = KISOrderRequestBuilder.prepare_new(
        _intent(Market.KR, "KRX"),
        account_number="12345678",
        product_code="01",
        environment="paper",
    )
    assert domestic.path == "/uapi/domestic-stock/v1/trading/order-cash"
    assert domestic.tr_id == "VTTC0012U"
    assert domestic.body["PDNO"] == "005930"
    assert domestic.body["ORD_DVSN"] == "00"
    assert domestic.body["ORD_UNPR"] == "70000"

    us_sell = KISOrderRequestBuilder.prepare_new(
        _intent(side=KISOrderSide.SELL),
        account_number="12345678",
        product_code="01",
        environment="prod",
    )
    assert us_sell.path == "/uapi/overseas-stock/v1/trading/order"
    assert us_sell.tr_id == "TTTT1006U"
    assert us_sell.body["OVRS_EXCG_CD"] == "NASD"
    assert us_sell.body["OVRS_ORD_UNPR"] == "180.25"
    assert us_sell.body["SLL_TYPE"] == "00"


def test_builds_official_cancel_contracts() -> None:
    cancel = _intent()
    cancel.action = KISOrderAction.CANCEL
    cancel.side = None
    cancel.limit_price = 0
    request = KISOrderRequestBuilder.prepare_cancel(
        cancel,
        account_number="12345678",
        product_code="01",
        environment="paper",
        original_order_number="30135009",
    )

    assert request.tr_id == "VTTT1004U"
    assert request.body["RVSE_CNCL_DVSN_CD"] == "02"
    assert request.body["ORGN_ODNO"] == "30135009"
    assert request.body["OVRS_ORD_UNPR"] == "0"


@pytest.mark.asyncio
async def test_production_transport_is_blocked_before_any_network_request() -> None:
    called = False

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    request = KISOrderRequestBuilder.prepare_new(
        _intent(),
        account_number="12345678",
        product_code="01",
        environment="prod",
    )
    transport = KISOrderTransport("key", "secret", transport=httpx.MockTransport(handler))

    with pytest.raises(KISOrderSafetyError, match="source-code disabled"):
        await transport.dispatch(request)
    assert called is False


@pytest.mark.asyncio
async def test_paper_transport_uses_token_hash_and_order_endpoints() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.host == "openapivts.koreainvestment.com"
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "paper-token"})
        if request.url.path == "/uapi/hashkey":
            return httpx.Response(200, json={"HASH": "payload-hash"})
        assert request.headers["tr_id"] == "VTTT1002U"
        assert request.headers["hashkey"] == "payload-hash"
        return httpx.Response(
            200,
            json={"rt_cd": "0", "msg_cd": "", "msg1": "", "output": {"ODNO": "1"}},
        )

    request = KISOrderRequestBuilder.prepare_new(
        _intent(),
        account_number="12345678",
        product_code="01",
        environment="paper",
    )
    transport = KISOrderTransport("key", "secret", transport=httpx.MockTransport(handler))

    response = await transport.dispatch(request)

    assert response["output"]["ODNO"] == "1"
    assert paths == [
        "/oauth2/tokenP",
        "/uapi/hashkey",
        "/uapi/overseas-stock/v1/trading/order",
    ]


def test_order_intent_idempotency_prevents_duplicate_records(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    recorder = KISOrderIntentRecorder(
        repository,
        account_configured=False,
        product_code="01",
    )
    values = {
        "order_id": "paper-order-1",
        "idempotency_key": "US_plan_001:NVDA:ENTRY",
        "plan_id": "US_plan_001",
        "market": Market.US,
        "symbol": "NVDA",
        "exchange": "NASDAQ",
        "side": "BUY",
        "quantity": 3,
        "limit_price": 180.25,
        "reason": "ENTRY_SIGNAL",
    }

    assert recorder.record_new_order(**values) is True
    assert recorder.record_new_order(**values) is False
    assert len(repository.recent_kis_order_intents()) == 1


def test_paper_broker_records_entry_cancel_and_exit_intents(tmp_path) -> None:
    repository = Repository(tmp_path / "broker.db")
    recorder = KISOrderIntentRecorder(
        repository,
        account_configured=True,
        product_code="01",
    )
    broker = PaperBroker(
        repository,
        {"US": CostConfig(1_500, 0, 0, 0, 0)},
        recorder,
    )
    started = datetime.now(UTC)

    first, reason = broker.submit_entry("US_plan_cancel", _candidate(), _tick(started))
    assert reason is None
    assert first is not None
    broker.cancel_pending(Market.US, "NVDA", "TEST_CANCEL")

    second, reason = broker.submit_entry(
        "US_plan_exit",
        _candidate(),
        _tick(started + timedelta(days=1)),
    )
    assert reason is None
    assert second is not None
    broker.process_pending(_tick(started + timedelta(days=1, milliseconds=400)))
    broker.on_tick(_tick(started + timedelta(days=1, seconds=1), 102.01))

    records = repository.recent_kis_order_intents()
    assert [(record["action"], record["side"]) for record in records] == [
        ("NEW", "SELL"),
        ("NEW", "BUY"),
        ("CANCEL", None),
        ("NEW", "BUY"),
    ]
    serialized = str(records)
    assert "12345678" not in serialized
    assert all(record["status"] == "RECORDED_ONLY" for record in records)
    assert all(record["payload"]["live_transport_enabled"] is False for record in records)


def test_settings_reject_any_runtime_live_order_mode() -> None:
    with pytest.raises(ValidationError, match="kis_order_mode"):
        Settings(
            gpt_action_bearer="g" * 24,
            admin_bearer="a" * 24,
            market_data_bearer="m" * 24,
            kis_order_mode="live",
        )
