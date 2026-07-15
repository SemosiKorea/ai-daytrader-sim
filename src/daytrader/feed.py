from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .feed_history import FeedHistoryStore, IndicatorCalculator
from .config import load_universe
from .kis_websocket import (
    FeedSymbol,
    KISApprovalClient,
    KISSubscription,
    KISWebSocketStream,
    RawQuote,
    RawTrade,
    build_subscriptions,
)
from .market_clock import MARKET_TZ, force_exit_at, is_session, session_bounds
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
    universe_path: Path = Path("config/universe.yaml")
    feed_scan_premarket_minutes_kr: int = 30
    feed_scan_premarket_minutes_us: int = 60
    feed_scan_regular_minutes: int = 120

    @model_validator(mode="after")
    def validate_feed(self) -> "EnrichedFeedSettings":
        if self.feed_overseas_tr_key_prefix.upper() not in {"D", "R"}:
            raise ValueError("FEED_OVERSEAS_TR_KEY_PREFIX must be D or R")
        if self.feed_discovery_seconds < 1 or self.feed_quote_max_age_seconds <= 0:
            raise ValueError("feed timing values must be positive")
        if min(
            self.feed_scan_premarket_minutes_kr,
            self.feed_scan_premarket_minutes_us,
            self.feed_scan_regular_minutes,
        ) <= 0:
            raise ValueError("feed scan windows must be positive")
        return self


@dataclass(slots=True)
class PremarketAccumulator:
    trade_date: date
    open: float
    high: float
    low: float
    last: float
    volume: int
    notional: float
    updated_at: datetime
    previous_close: float = 0.0

    def update(self, price: float, quantity: int, at: datetime) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.last = price
        size = max(0, quantity)
        self.volume += size
        self.notional += price * size
        self.updated_at = at


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
        self.premarket: dict[tuple[Market, str], PremarketAccumulator] = {}
        self.sequences: dict[tuple[Market, str], int] = {}
        self.universe = load_universe(settings.universe_path)
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

    def _scan_market(self, market: Market, now: datetime) -> bool:
        local = now.astimezone(MARKET_TZ[market])
        if not is_session(market, local.date()):
            return False
        session_open, _ = session_bounds(market, local.date())
        lead = (
            self.settings.feed_scan_premarket_minutes_kr
            if market == Market.KR
            else self.settings.feed_scan_premarket_minutes_us
        )
        return session_open - timedelta(minutes=lead) <= local <= min(
            session_open + timedelta(minutes=self.settings.feed_scan_regular_minutes),
            force_exit_at(market, local.date()),
        )

    def feed_symbols(self, now: datetime | None = None) -> set[FeedSymbol]:
        now = now or datetime.now().astimezone()
        symbols: set[FeedSymbol] = set()
        for market in Market:
            local_date = now.astimezone(MARKET_TZ[market]).date()
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
            scanning = self._scan_market(market, now)
            if scanning:
                for symbol, metadata in self.universe.get(market.value, {}).items():
                    symbols.add(
                        FeedSymbol(market, symbol.upper(), metadata["exchange"].upper())
                    )
            if (plans or scanning) and self.references[market] is not None:
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

    def _premarket_indicators(
        self,
        key: tuple[Market, str],
        previous_close: float | None,
    ) -> tuple[dict[str, float | bool], dict[str, bool], dict[str, datetime]]:
        state = self.premarket.get(key)
        if state is None:
            return {}, {}, {}
        vwap = state.notional / state.volume if state.volume else state.last
        previous_close = previous_close or state.previous_close or None
        values: dict[str, float | bool] = {
            "premarket_open": state.open,
            "premarket_high": state.high,
            "premarket_low": state.low,
            "premarket_last": state.last,
            "premarket_vwap": vwap,
            "premarket_volume": float(state.volume),
            "premarket_gap_pct": (
                (state.last - previous_close) / previous_close * 100
                if previous_close and previous_close > 0
                else 0.0
            ),
        }
        ready = {name: True for name in values}
        ready["premarket_gap_pct"] = bool(previous_close and previous_close > 0)
        timestamps = {name: state.updated_at for name in values}
        return values, ready, timestamps

    async def _post_tick(self, tick: MarketTick) -> None:
        try:
            response = await self.http.post(
                self.settings.feed_target_url,
                headers={"Authorization": f"Bearer {self.settings.market_data_bearer}"},
                json=tick.model_dump(mode="json"),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "feed target unavailable for %s:%s: %s",
                tick.market.value,
                tick.symbol,
                exc,
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "feed target rejected %s:%s status=%d body=%s",
                tick.market.value,
                tick.symbol,
                response.status_code,
                response.text[:500],
            )

    async def _emit_indicative_quote(
        self, quote: RawQuote, received_at: datetime, connection_id: str
    ) -> None:
        if (
            quote.market != Market.KR
            or quote.reference
            or self._session(quote.market, quote.at) != "premarket"
            or quote.indicative_price <= 0
            or min(quote.bid, quote.ask) <= 0
            or quote.bid >= quote.ask
        ):
            return
        key = (quote.market, quote.symbol.upper())
        trade_state = self.trades.get(key)
        if trade_state and trade_state[2] == connection_id and abs(
            (quote.at - trade_state[0].at).total_seconds()
        ) <= self.settings.feed_quote_max_age_seconds:
            return
        local_date = quote.at.astimezone(MARKET_TZ[quote.market]).date()
        prior_close = 0.0
        if quote.indicative_change_rate > -100:
            prior_close = quote.indicative_price / (1 + quote.indicative_change_rate / 100)
        state = self.premarket.get(key)
        if state is None or state.trade_date != local_date:
            state = PremarketAccumulator(
                trade_date=local_date,
                open=quote.indicative_price,
                high=quote.indicative_price,
                low=quote.indicative_price,
                last=quote.indicative_price,
                volume=max(0, quote.indicative_volume),
                notional=quote.indicative_price * max(0, quote.indicative_volume),
                updated_at=quote.at,
                previous_close=prior_close,
            )
            self.premarket[key] = state
        else:
            state.high = max(state.high, quote.indicative_price)
            state.low = min(state.low, quote.indicative_price)
            state.last = quote.indicative_price
            state.volume = max(0, quote.indicative_volume)
            state.notional = quote.indicative_price * state.volume
            state.updated_at = quote.at
            state.previous_close = prior_close or state.previous_close
        indicators, ready, timestamps = self._calculator(*key).snapshot(quote.at)
        pre_values, pre_ready, pre_timestamps = self._premarket_indicators(
            key,
            float(indicators["previous_close"])
            if ready.get("previous_close", False)
            else None,
        )
        indicators.update(pre_values)
        ready.update(pre_ready)
        timestamps.update(pre_timestamps)
        indicators["market_above_vwap_regular"] = False
        ready["market_above_vwap_regular"] = False
        timestamps["market_above_vwap_regular"] = quote.at
        self.sequences[key] = self.sequences.get(key, 0) + 1
        await self._post_tick(
            MarketTick(
                market=quote.market,
                symbol=quote.symbol,
                timestamp=quote.at,
                source_timestamp=quote.at,
                received_timestamp=received_at,
                sequence_id=self.sequences[key],
                connection_id=connection_id,
                data_source="KIS_WEBSOCKET_INDICATIVE",
                quote_scope="consolidated",
                session="premarket",
                market_status="open",
                symbol_status="trading",
                luld_status="normal",
                last=quote.indicative_price,
                bid=quote.bid,
                ask=quote.ask,
                bid_size=quote.bid_size,
                ask_size=quote.ask_size,
                trade_size=0,
                indicators=indicators,
                indicator_ready=ready,
                indicator_timestamps=timestamps,
            )
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
        pre_values, pre_ready, pre_timestamps = self._premarket_indicators(
            key,
            float(indicators["previous_close"])
            if ready.get("previous_close", False)
            else None,
        )
        indicators.update(pre_values)
        ready.update(pre_ready)
        timestamps.update(pre_timestamps)
        market_above, market_ready, market_timestamp = self._market_above_vwap(
            trade.market, trade.at, connection_id
        )
        indicators["market_above_vwap_regular"] = market_above
        ready["market_above_vwap_regular"] = market_ready
        timestamps["market_above_vwap_regular"] = market_timestamp
        self.sequences[key] = self.sequences.get(key, 0) + 1
        session = self._session(trade.market, trade.at)
        if session not in {"premarket", "regular"}:
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
        await self._post_tick(tick)

    async def on_record(
        self, record: RawQuote | RawTrade, received_at: datetime, connection_id: str
    ) -> None:
        key = (record.market, record.symbol.upper())
        if isinstance(record, RawQuote):
            self.quotes[key] = (record, received_at, connection_id)
            await self._emit_indicative_quote(record, received_at, connection_id)
        else:
            self.trades[key] = (record, received_at, connection_id)
            local_date = record.at.astimezone(MARKET_TZ[record.market]).date()
            prior = self.cumulative_volumes.get(key)
            quantity = record.size
            if prior and prior[0] == local_date and record.cumulative_volume >= prior[1]:
                quantity = record.cumulative_volume - prior[1]
            self.cumulative_volumes[key] = (local_date, record.cumulative_volume)
            if self._session(record.market, record.at) == "premarket":
                state = self.premarket.get(key)
                if state is None or state.trade_date != local_date:
                    self.premarket[key] = PremarketAccumulator(
                        trade_date=local_date,
                        open=record.last,
                        high=record.last,
                        low=record.last,
                        last=record.last,
                        volume=max(0, quantity),
                        notional=record.last * max(0, quantity),
                        updated_at=record.at,
                        previous_close=record.previous_close,
                    )
                else:
                    state.update(record.last, quantity, record.at)
                    state.previous_close = record.previous_close or state.previous_close
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
