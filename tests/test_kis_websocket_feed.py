from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from daytrader.feed import EnrichedFeedBridge, EnrichedFeedSettings
from daytrader.kis_websocket import (
    DOMESTIC_QUOTE_COLUMNS,
    DOMESTIC_QUOTE_TR,
    DOMESTIC_TRADE_COLUMNS,
    DOMESTIC_TRADE_TR,
    OVERSEAS_QUOTE_COLUMNS,
    OVERSEAS_QUOTE_TR,
    OVERSEAS_TRADE_COLUMNS,
    OVERSEAS_TRADE_TR,
    FeedSymbol,
    KISFrameDecoder,
    RawQuote,
    RawTrade,
    build_subscriptions,
    subscription_message,
)
from daytrader.models import Market


def _frame(tr_id: str, columns: list[str], values: dict[str, str]) -> str:
    payload = "^".join(values.get(column, "0") for column in columns)
    return f"0|{tr_id}|1|{payload}"


def test_builds_official_kis_subscription_keys() -> None:
    subscriptions = build_subscriptions(
        {
            FeedSymbol(Market.KR, "005930", "KRX"),
            FeedSymbol(Market.US, "NVDA", "NASDAQ"),
        }
    )

    assert {(item.tr_id, item.tr_key) for item in subscriptions} == {
        (DOMESTIC_QUOTE_TR, "005930"),
        (DOMESTIC_TRADE_TR, "005930"),
        (OVERSEAS_QUOTE_TR, "DNASNVDA"),
        (OVERSEAS_TRADE_TR, "DNASNVDA"),
    }
    message = json.loads(subscription_message("approval", subscriptions[0]))
    assert message["header"]["approval_key"] == "approval"
    assert message["body"]["input"]["tr_id"] == subscriptions[0].tr_id


def test_decodes_domestic_quote_and_trade() -> None:
    subscriptions = build_subscriptions({FeedSymbol(Market.KR, "005930", "KRX")})
    decoder = KISFrameDecoder(subscriptions)
    received = datetime(2026, 7, 15, 1, 5, tzinfo=UTC)

    quote = decoder.decode(
        _frame(
            DOMESTIC_QUOTE_TR,
            DOMESTIC_QUOTE_COLUMNS,
            {
                "MKSC_SHRN_ISCD": "005930",
                "BSOP_HOUR": "100500",
                "BIDP1": "69900",
                "ASKP1": "70000",
                "BIDP_RSQN1": "120",
                "ASKP_RSQN1": "80",
                "ANTC_CNPR": "70100",
                "ANTC_VOL": "15000",
                "ANTC_CNTG_PRDY_CTRT": "1.59",
            },
        ),
        received,
    )[0]
    trade = decoder.decode(
        _frame(
            DOMESTIC_TRADE_TR,
            DOMESTIC_TRADE_COLUMNS,
            {
                "MKSC_SHRN_ISCD": "005930",
                "BSOP_DATE": "20260715",
                "STCK_CNTG_HOUR": "100501",
                "STCK_PRPR": "70000",
                "CNTG_VOL": "10",
                "ACML_VOL": "12345",
                "TRHT_YN": "N",
            },
        ),
        received,
    )[0]

    assert isinstance(quote, RawQuote)
    assert quote.bid == 69_900
    assert quote.ask_size == 80
    assert quote.indicative_price == 70_100
    assert quote.indicative_volume == 15_000
    assert isinstance(trade, RawTrade)
    assert trade.last == 70_000
    assert trade.size == 10
    assert trade.at.tzinfo is not None


def test_decodes_overseas_quote_and_trade() -> None:
    subscriptions = build_subscriptions({FeedSymbol(Market.US, "NVDA", "NASDAQ")})
    decoder = KISFrameDecoder(subscriptions)
    received = datetime(2026, 7, 15, 14, 5, tzinfo=UTC)

    quote = decoder.decode(
        _frame(
            OVERSEAS_QUOTE_TR,
            OVERSEAS_QUOTE_COLUMNS,
            {
                "SYMB": "DNASNVDA",
                "XYMD": "20260715",
                "XHMS": "100500",
                "PBID1": "180.01",
                "PASK1": "180.03",
                "VBID1": "500",
                "VASK1": "300",
            },
        ),
        received,
    )[0]
    trade = decoder.decode(
        _frame(
            OVERSEAS_TRADE_TR,
            OVERSEAS_TRADE_COLUMNS,
            {
                "SYMB": "DNASNVDA",
                "XYMD": "20260715",
                "XHMS": "100501",
                "LAST": "180.02",
                "EVOL": "25",
                "TVOL": "50000",
            },
        ),
        received,
    )[0]

    assert isinstance(quote, RawQuote)
    assert quote.bid == pytest.approx(180.01)
    assert quote.ask_size == 300
    assert isinstance(trade, RawTrade)
    assert trade.last == pytest.approx(180.02)
    assert trade.cumulative_volume == 50_000


@pytest.mark.asyncio
async def test_bridge_posts_a_schema_valid_enriched_tick(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        market_data_bearer="m" * 32,
        kis_app_key="key",
        kis_app_secret="secret",
        feed_target_url="http://feed.test/v1/market-data/ticks",
    )
    bridge = EnrichedFeedBridge(settings)
    requests: list[dict] = []

    class FakeHTTP:
        async def post(self, url, *, headers, json):
            requests.append({"url": url, "headers": headers, "json": json})

            class Response:
                status_code = 202
                text = ""

            return Response()

        async def aclose(self):
            return None

    await bridge.http.aclose()
    bridge.http = FakeHTTP()
    at = datetime(2026, 7, 15, 1, 5, tzinfo=UTC)
    quote = RawQuote(Market.KR, "005930", at, 69_900, 70_000, 120, 80)
    trade = RawTrade(Market.KR, "005930", at, 69_950, 10, 1000)

    await bridge.on_record(quote, at, "connection-1")
    await bridge.on_record(trade, at, "connection-1")

    assert len(requests) == 1
    payload = requests[0]["json"]
    assert payload["data_source"] == "KIS_WEBSOCKET_ENRICHED"
    assert payload["quote_scope"] == "consolidated"
    assert payload["bid_size"] == 120
    assert "vwap_regular" in payload["indicators"]


@pytest.mark.asyncio
async def test_bridge_does_not_join_records_across_reconnections(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        market_data_bearer="m" * 32,
        kis_app_key="key",
        kis_app_secret="secret",
        feed_target_url="http://feed.test/v1/market-data/ticks",
    )
    bridge = EnrichedFeedBridge(settings)
    requests: list[dict] = []

    class FakeHTTP:
        async def post(self, url, *, headers, json):
            requests.append(json)

            class Response:
                status_code = 202
                text = ""

            return Response()

        async def aclose(self):
            return None

    await bridge.http.aclose()
    bridge.http = FakeHTTP()
    at = datetime(2026, 7, 15, 1, 5, tzinfo=UTC)
    quote = RawQuote(Market.KR, "005930", at, 69_900, 70_000, 120, 80)
    trade = RawTrade(Market.KR, "005930", at, 69_950, 10, 1000)

    await bridge.on_record(quote, at, "connection-old")
    await bridge.on_record(trade, at, "connection-new")

    assert requests == []


@pytest.mark.asyncio
async def test_bridge_keeps_premarket_separate_from_regular_indicators(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        market_data_bearer="m" * 32,
        kis_app_key="key",
        kis_app_secret="secret",
        feed_target_url="http://feed.test/v1/market-data/ticks",
    )
    bridge = EnrichedFeedBridge(settings)
    requests: list[dict] = []

    class FakeHTTP:
        async def post(self, url, *, headers, json):
            requests.append(json)

            class Response:
                status_code = 202
                text = ""

            return Response()

        async def aclose(self):
            return None

    await bridge.http.aclose()
    bridge.http = FakeHTTP()
    at = datetime(2026, 7, 15, 23, 45, tzinfo=UTC)  # 08:45 KST
    await bridge.on_record(
        RawQuote(Market.KR, "005930", at, 69_900, 70_000, 120, 80),
        at,
        "connection-1",
    )
    await bridge.on_record(
        RawTrade(Market.KR, "005930", at, 69_950, 10, 1000),
        at,
        "connection-1",
    )

    assert requests[0]["session"] == "premarket"
    assert requests[0]["indicator_ready"]["premarket_vwap"] is True
    assert requests[0]["indicator_ready"]["vwap_regular"] is False


@pytest.mark.asyncio
async def test_bridge_emits_kr_indicative_snapshot_without_trade(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        market_data_bearer="m" * 32,
        feed_target_url="http://feed.test/v1/market-data/ticks",
    )
    bridge = EnrichedFeedBridge(settings)
    requests: list[dict] = []

    class FakeHTTP:
        async def post(self, url, *, headers, json):
            requests.append(json)

            class Response:
                status_code = 202
                text = ""

            return Response()

        async def aclose(self):
            return None

    await bridge.http.aclose()
    bridge.http = FakeHTTP()
    at = datetime(2026, 7, 15, 23, 45, tzinfo=UTC)
    quote = RawQuote(
        Market.KR,
        "005930",
        at,
        69_900,
        70_000,
        120,
        80,
        indicative_price=69_950,
        indicative_volume=15_000,
        indicative_change_rate=1.38,
    )

    await bridge.on_record(quote, at, "connection-1")

    assert requests[0]["data_source"] == "KIS_WEBSOCKET_INDICATIVE"
    assert requests[0]["last"] == 69_950
    assert requests[0]["indicators"]["premarket_volume"] == 15_000
    assert requests[0]["indicator_ready"]["premarket_gap_pct"] is True


def test_bridge_subscribes_active_market_universe_before_approval(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        market_data_bearer="m" * 32,
    )
    bridge = EnrichedFeedBridge(settings)
    now = datetime(2026, 7, 15, 23, 45, tzinfo=UTC)  # KR premarket scan window
    symbols = bridge.feed_symbols(now)

    assert FeedSymbol(Market.KR, "005930", "KRX") in symbols
    assert any(item.reference and item.market == Market.KR for item in symbols)
    assert len(build_subscriptions(symbols)) <= 40


def test_configured_multitheme_universes_leave_subscription_capacity(tmp_path) -> None:
    settings = EnrichedFeedSettings(
        _env_file=None,
        database_path=tmp_path / "daytrader.db",
        feed_history_path=tmp_path / "history.db",
        universe_path="config/universe.yaml",
        market_data_bearer="m" * 32,
    )
    bridge = EnrichedFeedBridge(settings)

    kr_symbols = bridge.feed_symbols(datetime(2026, 7, 15, 23, 45, tzinfo=UTC))
    us_symbols = bridge.feed_symbols(datetime(2026, 7, 16, 13, 0, tzinfo=UTC))

    assert len([item for item in kr_symbols if item.market == Market.KR]) == 19
    assert len([item for item in us_symbols if item.market == Market.US]) == 19
    assert len(build_subscriptions(kr_symbols)) == 38
    assert len(build_subscriptions(us_symbols)) == 38
    assert bridge.universe["KR"]["012450"]["theme"] == "방산·우주항공"
    assert bridge.universe["US"]["CRWD"]["theme"] == "사이버보안"
