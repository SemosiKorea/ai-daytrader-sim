from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .feed_history import FeedHistoryStore, IndicatorCalculator
from .kis_websocket import (
    FeedSymbol,
    KISApprovalClient,
    KISSubscription,
    KISWebSocketStream,
    RawQuote,
    RawTrade,
    build_subscriptions,
)
from .market_clock import MARKET_TZ, is_session, session_bounds
from .models import Market, MarketTick
from .repository import Repository

logger = logging.getLogger(__name__)


class EnrichedFeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: Path = Path("data/daytrader.db")
    feed_history_path: Path = Path("data/feed_history.db")
    market_data_bearer: str = "change-feed"
    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_env: Literal["prod", "paper"] = "paper"
    feed_target_url: str = "http://127.0.0.1:8787/v1/market-data/ticks"
    feed_discovery_seconds: float = 5.0
    feed_quote_max_age_seconds: float = 3.0
    feed_us_quote_scope: Literal["venue", "consolidated"] = "venue"
    feed_overseas_tr_key_prefix: str = "D"
    feed_reference_kr: str = "069500:KRX"
    feed_reference_us: str = "QQQ:NASDAQ"

    @model_validator(mode="after")
    def validate_feed(self) -> "EnrichedFeedSettings":
        if self.feed_overseas_tr_key_prefix.upper() not in {"D", "R"}:
            raise ValueError("FEED_OVERSEAS_TR_KEY_PREFIX must be D or R")
        if self.feed_discovery_seconds < 1 or self.feed_quote_max_age_seconds <= 0:
            raise ValueError("feed timing values must be positive")
        return self


def _reference(value: str, market: Market) -> FeedSymbol | None:
    if not value.strip():
        return None
    parts = value.upper().split(":", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError("market reference must use SYMBOL:EXCHANGE")
    return FeedSymbol(market, parts[0], parts[1], reference=True)


class EnrichedFeedBridge:
    """Join KIS trade/quote streams, calculate indicators, and push safe MarketTick data."""

    def __init__(self, settings: EnrichedFeedSettings):
        self.settings = settings
        self.repository = Repository(settings.database_path)
        self.history = FeedHistoryStore(settings.feed_history_path)
        self.calculators: dict[tuple[Market, str], IndicatorCalculator] = {}
        self.quotes: dict[tuple[Market, str], tuple[RawQuote, datetime, str]] = {}
        self.trades: dict[tuple[Market, str], tuple[RawTrade, datetime, str]] = {}
        self.cumulative_volumes: dict[tuple[Market, str], tuple[date, int]] = {}
        self.sequences: dict[tuple[Market, str], int] = {}
        self.references = {
            Market.KR: _reference(settings.feed_reference_kr, Market.KR),
            Market.US: _reference(settings.feed_reference_us, Market.US),
        }
        self.http = httpx.AsyncClient(timeout=5)

    def _calculator(self, market: Market, symbol: str) -> IndicatorCalculator:
        key = (market, symbol.upper())
        if key not in self.calculators:
            self.calculators[key] = IndicatorCalculator(market, symbol, self.history)
        return self.calculators[key]

    def feed_symbols(self) -> set[FeedSymbol]:
        symbols: set[FeedSymbol] = set()
        for market in Market:
            local_date = datetime.now(MARKET_TZ[market]).date()
            plans = self.repository.active_plans(market, local_date)
            for plan in plans:
                for candidate in plan.approved_symbols:
                    symbols.add(
                        FeedSymbol(
                            market=market,
                            symbol=candidate.symbol.upper(),
                            exchange=candidate.exchange.upper(),
                        )
                    )
            if plans and self.references[market] is not None:
                symbols.add(self.references[market])
        return symbols

    def subscriptions(self) -> tuple[KISSubscription, ...]:
        return build_subscriptions(
            self.feed_symbols(),
            overseas_prefix=self.settings.feed_overseas_tr_key_prefix,
        )

    @staticmethod
    def _session(market: Market, timestamp: datetime) -> str:
        local = timestamp.astimezone(MARKET_TZ[market])
        if not is_session(market, local.date()):
            return "closed"
        session_open, session_close = session_bounds(market, local.date())
        if local < session_open:
            return "premarket"
        if local >= session_close:
            return "afterhours"
        return "regular"

    def _market_above_vwap(
        self, market: Market, timestamp: datetime, connection_id: str
    ) -> tuple[bool, bool, datetime]:
        reference = self.references[market]
        if reference is None:
            return False, False, timestamp
        key = (market, reference.symbol)
        trade_state = self.trades.get(key)
        if trade_state is None:
            return False, False, timestamp
        trade, _, trade_connection_id = trade_state
        if trade_connection_id != connection_id:
            return False, False, trade.at
        if abs((timestamp - trade.at).total_seconds()) > self.settings.feed_quote_max_age_seconds:
            return False, False, trade.at
        indicators, ready, generated = self._calculator(market, reference.symbol).snapshot(trade.at)
        vwap_ready = ready.get("vwap_regular", False)
        return (
            bool(vwap_ready and trade.last > float(indicators["vwap_regular"])),
            vwap_ready,
            generated.get("vwap_regular", timestamp),
        )

    async def _emit(
        self, key: tuple[Market, str], received_at: datetime, connection_id: str
    ) -> None:
        quote_state = self.quotes.get(key)
        trade_state = self.trades.get(key)
        if quote_state is None or trade_state is None:
            return
        quote, quote_received, quote_connection_id = quote_state
        trade, trade_received, trade_connection_id = trade_state
        if quote_connection_id != connection_id or trade_connection_id != connection_id:
            return
        if quote.reference or trade.reference:
            return
        if (
            abs((quote_received - trade_received).total_seconds())
            > self.settings.feed_quote_max_age_seconds
        ):
            return
        if min(quote.bid, quote.ask, trade.last) <= 0 or quote.bid >= quote.ask:
            return
        indicators, ready, timestamps = self._calculator(*key).snapshot(trade.at)
        market_above, market_ready, market_timestamp = self._market_above_vwap(
            trade.market, trade.at, connection_id
        )
        indicators["market_above_vwap_regular"] = market_above
        ready["market_above_vwap_regular"] = market_ready
        timestamps["market_above_vwap_regular"] = market_timestamp
        self.sequences[key] = self.sequences.get(key, 0) + 1
        session = self._session(trade.market, trade.at)
        if session != "regular":
            return
        tick = MarketTick(
            market=trade.market,
            symbol=trade.symbol,
            timestamp=trade.at,
            source_timestamp=max(trade.at, quote.at),
            received_timestamp=received_at,
            sequence_id=self.sequences[key],
            connection_id=connection_id,
            data_source="KIS_WEBSOCKET_ENRICHED",
            quote_scope=(
                "consolidated" if trade.market == Market.KR else self.settings.feed_us_quote_scope
            ),
            session=session,
            market_status="open",
            symbol_status="halted" if trade.halted else "trading",
            luld_status="normal",
            halt_reason="KIS_TRADING_HALT" if trade.halted else None,
            last=trade.last,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            trade_size=trade.size,
            indicators=indicators,
            indicator_ready=ready,
            indicator_timestamps=timestamps,
        )
        try:
            response = await self.http.post(
                self.settings.feed_target_url,
                headers={"Authorization": f"Bearer {self.settings.market_data_bearer}"},
                json=tick.model_dump(mode="json"),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "feed target unavailable for %s:%s: %s",
                trade.market.value,
                trade.symbol,
                exc,
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "feed target rejected %s:%s status=%d body=%s",
                trade.market.value,
                trade.symbol,
                response.status_code,
                response.text[:500],
            )

    async def on_record(
        self, record: RawQuote | RawTrade, received_at: datetime, connection_id: str
    ) -> None:
        key = (record.market, record.symbol.upper())
        if isinstance(record, RawQuote):
            self.quotes[key] = (record, received_at, connection_id)
        else:
            self.trades[key] = (record, received_at, connection_id)
            local_date = record.at.astimezone(MARKET_TZ[record.market]).date()
            prior = self.cumulative_volumes.get(key)
            quantity = record.size
            if prior and prior[0] == local_date and record.cumulative_volume >= prior[1]:
                quantity = record.cumulative_volume - prior[1]
            self.cumulative_volumes[key] = (local_date, record.cumulative_volume)
            self._calculator(record.market, record.symbol).on_trade(
                record.last, quantity, record.at
            )
        await self._emit(key, received_at, connection_id)

    async def run(self) -> None:
        if not self.settings.kis_app_key or not self.settings.kis_app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET are required for the live feed")
        if len(
            self.settings.market_data_bearer
        ) < 24 or self.settings.market_data_bearer.startswith(("change-", "replace-")):
            raise ValueError("MARKET_DATA_BEARER must be a random value of at least 24 characters")
        approval = KISApprovalClient(
            self.settings.kis_app_key or "",
            self.settings.kis_app_secret or "",
            self.settings.kis_env,
        )
        stream = KISWebSocketStream(
            approval,
            self.settings.kis_env,
            self.subscriptions,
            self.on_record,
            self.settings.feed_discovery_seconds,
        )
        try:
            await stream.run()
        finally:
            await self.http.aclose()


def run() -> None:
    parser = argparse.ArgumentParser(description="KIS enriched read-only market-data bridge")
    parser.add_argument(
        "--import-history",
        type=Path,
        help="import verified one-minute bars from CSV and exit",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = EnrichedFeedSettings()
    if args.import_history:
        imported = FeedHistoryStore(settings.feed_history_path).import_csv(args.import_history)
        logger.info("imported %d historical minute bars", imported)
        return
    asyncio.run(EnrichedFeedBridge(settings).run())
