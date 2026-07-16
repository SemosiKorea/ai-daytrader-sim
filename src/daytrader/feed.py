from __future__ import annotations

import argparse
import asyncio
import json
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
from .kis_history import KISUSMinuteHistorySync
from .kis_readonly import KISReadOnlyClient
from .market_clock import MARKET_TZ, force_exit_at, is_session, session_bounds
from .models import Market, MarketTick
from .repository import Repository

logger = logging.getLogger(__name__)


class EnrichedFeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: Path = Path("data/daytrader.db")
    telegram_trade_database_path: Path = Path("data/simple_telegram.db")
    telegram_trade_poll_enabled: bool = False
    feed_history_path: Path = Path("data/feed_history.db")
    market_data_bearer: str = "change-feed"
    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_env: Literal["prod", "paper"] = "paper"
    feed_target_url: str = "http://127.0.0.1:8787/v1/market-data/ticks"
    feed_discovery_seconds: float = 5.0
    feed_quote_max_age_seconds: float = 3.0
    feed_emit_interval_ms: int = 200
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
        if not 100 <= self.feed_emit_interval_ms <= 250:
            raise ValueError("FEED_EMIT_INTERVAL_MS must be between 100 and 250")
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

    def update_batch(
        self,
        *,
        high: float,
        low: float,
        last: float,
        volume: int,
        notional: float,
        at: datetime,
    ) -> None:
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self.last = last
        self.volume += max(0, volume)
        self.notional += max(0.0, notional)
        self.updated_at = at


@dataclass(slots=True)
class PendingSymbolRecords:
    connection_id: str
    quote: RawQuote | None = None
    quote_received_at: datetime | None = None
    quote_version: int = 0
    trade: RawTrade | None = None
    trade_received_at: datetime | None = None
    trade_version: int = 0
    trade_batch_open: float | None = None
    trade_batch_high: float | None = None
    trade_batch_low: float | None = None
    trade_batch_volume: int = 0
    trade_batch_notional: float = 0.0
    applied_quote_version: int = 0
    applied_trade_version: int = 0
    emitted_quote_version: int = 0
    emitted_trade_version: int = 0
    indicative_quote_version: int = 0


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
        database_path = (
            settings.telegram_trade_database_path
            if settings.telegram_trade_poll_enabled
            else settings.database_path
        )
        self.repository = Repository(database_path)
        self.history = FeedHistoryStore(settings.feed_history_path)
        self.calculators: dict[tuple[Market, str], IndicatorCalculator] = {}
        self.quotes: dict[tuple[Market, str], tuple[RawQuote, datetime, str]] = {}
        self.trades: dict[tuple[Market, str], tuple[RawTrade, datetime, str]] = {}
        self.cumulative_volumes: dict[tuple[Market, str], tuple[date, int]] = {}
        self.premarket: dict[tuple[Market, str], PremarketAccumulator] = {}
        self.sequences: dict[tuple[Market, str], int] = {}
        self.pending_records: dict[tuple[Market, str], PendingSymbolRecords] = {}
        self.pending_event = asyncio.Event()
        self.dropped_records = 0
        self._last_drop_log_at: datetime | None = None
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
            experiments = self.repository.active_portfolio_experiments(market, local_date)
            for experiment in experiments:
                for candidate in experiment.candidates:
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
            if (plans or experiments or scanning) and self.references[market] is not None:
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

    def _record_drop(self, count: int, now: datetime) -> None:
        self.dropped_records += count
        if self._last_drop_log_at is None:
            self._last_drop_log_at = now
            return
        if (now - self._last_drop_log_at).total_seconds() < 60:
            return
        logger.warning(
            "discarded %d superseded or stale KIS records in the last interval",
            self.dropped_records,
        )
        self.dropped_records = 0
        self._last_drop_log_at = now

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
        """Store only the newest record and return without doing HTTP or indicator work."""
        key = (record.market, record.symbol.upper())
        state = self.pending_records.get(key)
        if state is None or state.connection_id != connection_id:
            state = PendingSymbolRecords(connection_id=connection_id)
            self.pending_records[key] = state
        if isinstance(record, RawQuote):
            if state.quote is not None and record.at < state.quote.at:
                self._record_drop(1, received_at)
                return
            if state.quote_version > state.applied_quote_version:
                self._record_drop(1, received_at)
            state.quote = record
            state.quote_received_at = received_at
            state.quote_version += 1
        else:
            if state.trade is not None and record.at < state.trade.at:
                self._record_drop(1, received_at)
                return
            if state.trade_version > state.applied_trade_version:
                self._record_drop(1, received_at)
            local_date = record.at.astimezone(MARKET_TZ[record.market]).date()
            prior_cumulative: int | None = None
            if state.trade is not None and (
                state.trade.at.astimezone(MARKET_TZ[state.trade.market]).date() == local_date
            ):
                prior_cumulative = state.trade.cumulative_volume
            else:
                prior = self.cumulative_volumes.get(key)
                if prior and prior[0] == local_date:
                    prior_cumulative = prior[1]
            quantity = max(0, record.size)
            if prior_cumulative is not None and record.cumulative_volume >= prior_cumulative:
                quantity = record.cumulative_volume - prior_cumulative
            if state.trade_batch_open is None:
                state.trade_batch_open = record.last
            state.trade_batch_high = max(state.trade_batch_high or record.last, record.last)
            state.trade_batch_low = min(state.trade_batch_low or record.last, record.last)
            state.trade_batch_volume += quantity
            state.trade_batch_notional += record.last * quantity
            state.trade = record
            state.trade_received_at = received_at
            state.trade_version += 1
        self.pending_event.set()

    def _apply_trade(
        self,
        key: tuple[Market, str],
        trade: RawTrade,
        received_at: datetime,
        connection_id: str,
        batch_open: float,
        batch_high: float,
        batch_low: float,
        batch_volume: int,
        batch_notional: float,
    ) -> None:
        self.trades[key] = (trade, received_at, connection_id)
        local_date = trade.at.astimezone(MARKET_TZ[trade.market]).date()
        self.cumulative_volumes[key] = (local_date, trade.cumulative_volume)
        if self._session(trade.market, trade.at) == "premarket":
            accumulator = self.premarket.get(key)
            if accumulator is None or accumulator.trade_date != local_date:
                self.premarket[key] = PremarketAccumulator(
                    trade_date=local_date,
                    open=batch_open,
                    high=batch_high,
                    low=batch_low,
                    last=trade.last,
                    volume=batch_volume,
                    notional=batch_notional,
                    updated_at=trade.at,
                    previous_close=trade.previous_close,
                )
            else:
                accumulator.update_batch(
                    high=batch_high,
                    low=batch_low,
                    last=trade.last,
                    volume=batch_volume,
                    notional=batch_notional,
                    at=trade.at,
                )
                accumulator.previous_close = trade.previous_close or accumulator.previous_close
        self._calculator(trade.market, trade.symbol).on_trade(
            trade.last,
            batch_volume,
            trade.at,
            opening_price=batch_open,
            high=batch_high,
            low=batch_low,
            notional=batch_notional,
        )

    async def flush_pending_once(self, now: datetime | None = None) -> None:
        """Apply one coalesced snapshot per symbol without blocking WebSocket reception."""
        now = now or datetime.now().astimezone()
        snapshots = []
        for key, state in self.pending_records.items():
            snapshots.append(
                (
                    key,
                    state,
                    state.quote,
                    state.quote_received_at,
                    state.quote_version,
                    state.trade,
                    state.trade_received_at,
                    state.trade_version,
                    state.trade_batch_open,
                    state.trade_batch_high,
                    state.trade_batch_low,
                    state.trade_batch_volume,
                    state.trade_batch_notional,
                )
            )
            state.trade_batch_open = None
            state.trade_batch_high = None
            state.trade_batch_low = None
            state.trade_batch_volume = 0
            state.trade_batch_notional = 0.0
        for (
            key,
            state,
            quote,
            quote_received_at,
            quote_version,
            trade,
            trade_received_at,
            trade_version,
            trade_batch_open,
            trade_batch_high,
            trade_batch_low,
            trade_batch_volume,
            trade_batch_notional,
        ) in snapshots:
            if self.pending_records.get(key) is not state:
                continue
            stale_quote = bool(
                quote
                and (now - quote.at.astimezone(now.tzinfo)).total_seconds()
                > self.settings.feed_quote_max_age_seconds
            )
            stale_trade = bool(
                trade
                and (now - trade.at.astimezone(now.tzinfo)).total_seconds()
                > self.settings.feed_quote_max_age_seconds
            )
            if stale_quote and quote_version > state.applied_quote_version:
                state.applied_quote_version = quote_version
                state.emitted_quote_version = quote_version
                state.indicative_quote_version = quote_version
                self._record_drop(1, now)
            if stale_trade and trade_version > state.applied_trade_version:
                state.applied_trade_version = trade_version
                state.emitted_trade_version = trade_version
                self._record_drop(1, now)
            if (
                quote is not None
                and quote_received_at is not None
                and not stale_quote
                and quote_version > state.applied_quote_version
            ):
                self.quotes[key] = (quote, quote_received_at, state.connection_id)
                state.applied_quote_version = quote_version
            if (
                trade is not None
                and trade_received_at is not None
                and trade_batch_open is not None
                and trade_batch_high is not None
                and trade_batch_low is not None
                and not stale_trade
                and trade_version > state.applied_trade_version
            ):
                self._apply_trade(
                    key,
                    trade,
                    trade_received_at,
                    state.connection_id,
                    trade_batch_open,
                    trade_batch_high,
                    trade_batch_low,
                    trade_batch_volume,
                    trade_batch_notional,
                )
                state.applied_trade_version = trade_version
            if (
                quote is not None
                and quote_received_at is not None
                and not stale_quote
                and quote_version > state.indicative_quote_version
            ):
                await self._emit_indicative_quote(quote, quote_received_at, state.connection_id)
                state.indicative_quote_version = quote_version
            if (
                quote is not None
                and trade is not None
                and quote_received_at is not None
                and trade_received_at is not None
                and not stale_quote
                and not stale_trade
                and quote_version > state.emitted_quote_version
                and trade_version > state.emitted_trade_version
            ):
                received_at = max(quote_received_at, trade_received_at)
                await self._emit(key, received_at, state.connection_id)
                state.emitted_quote_version = quote_version
                state.emitted_trade_version = trade_version

    async def _process_pending_records(self) -> None:
        interval = self.settings.feed_emit_interval_ms / 1000
        while True:
            await self.pending_event.wait()
            await asyncio.sleep(interval)
            self.pending_event.clear()
            await self.flush_pending_once()
            if any(
                state.quote_version > state.applied_quote_version
                or state.trade_version > state.applied_trade_version
                for state in self.pending_records.values()
            ):
                self.pending_event.set()

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
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(stream.run(), name="kis-websocket-stream")
                tasks.create_task(
                    self._process_pending_records(), name="kis-latest-record-processor"
                )
        finally:
            await self.http.aclose()


def run() -> None:
    parser = argparse.ArgumentParser(description="KIS enriched read-only market-data bridge")
    parser.add_argument(
        "--import-history",
        type=Path,
        help="import verified one-minute bars from CSV and exit",
    )
    parser.add_argument(
        "--sync-kis-us-history",
        action="store_true",
        help="read and store official KIS US regular-session one-minute history",
    )
    parser.add_argument(
        "--history-sessions",
        type=int,
        default=20,
        help="number of completed US sessions to synchronize (default: 20)",
    )
    parser.add_argument(
        "--history-symbol",
        action="append",
        default=[],
        help="limit KIS history sync to a symbol; may be repeated",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = EnrichedFeedSettings()
    if args.import_history and args.sync_kis_us_history:
        parser.error("--import-history and --sync-kis-us-history are mutually exclusive")
    if args.import_history:
        imported = FeedHistoryStore(settings.feed_history_path).import_csv(args.import_history)
        logger.info("imported %d historical minute bars", imported)
        return
    if args.sync_kis_us_history:
        if args.history_sessions < 1 or args.history_sessions > 22:
            parser.error("--history-sessions must be between 1 and 22")
        if not settings.kis_app_key or not settings.kis_app_secret:
            parser.error("KIS_APP_KEY and KIS_APP_SECRET are required")
        requested = {symbol.upper() for symbol in args.history_symbol}
        symbols = dict(load_universe(settings.universe_path).get("US", {}))
        reference = _reference(settings.feed_reference_us, Market.US)
        if reference is not None:
            symbols.setdefault(reference.symbol, {"exchange": reference.exchange})
        if requested:
            unknown = requested - set(symbols)
            if unknown:
                parser.error(f"unknown US history symbols: {sorted(unknown)}")
            symbols = {symbol: symbols[symbol] for symbol in sorted(requested)}

        async def synchronize() -> list[dict[str, object]]:
            client = KISReadOnlyClient(
                settings.kis_app_key or "",
                settings.kis_app_secret or "",
                settings.kis_env,
            )
            try:
                await client.authenticate()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    raise
                logger.warning(
                    "KIS rejected rapid access-token reissuance; retrying once in 65 seconds"
                )
                await asyncio.sleep(65)
                await client.authenticate()
            sync = KISUSMinuteHistorySync(
                client,
                FeedHistoryStore(settings.feed_history_path),
            )
            results = []
            for symbol, metadata in symbols.items():
                result = await sync.sync_symbol(
                    symbol,
                    metadata["exchange"],
                    sessions=args.history_sessions,
                )
                results.append(
                    {
                        "symbol": result.symbol,
                        "requested_sessions": result.requested_sessions,
                        "complete_sessions": result.complete_sessions,
                        "saved_bars": result.saved_bars,
                        "incomplete_sessions": result.incomplete_sessions,
                        "ready": result.ready,
                    }
                )
            return results

        results = asyncio.run(synchronize())
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(bool(result["ready"]) for result in results):
            raise SystemExit(2)
        return
    asyncio.run(EnrichedFeedBridge(settings).run())
