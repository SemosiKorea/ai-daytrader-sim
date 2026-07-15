from __future__ import annotations

import math
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Market(StrEnum):
    KR = "KR"
    US = "US"


class PlanStatus(StrEnum):
    VALIDATED = "VALIDATED"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderState(StrEnum):
    SIGNAL_TRIGGERED = "SIGNAL_TRIGGERED"
    ENTRY_PENDING = "ENTRY_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DATA_ERROR = "DATA_ERROR"
    MARKET_HALTED = "MARKET_HALTED"
    RISK_BLOCKED = "RISK_BLOCKED"
    FORCE_CLOSED = "FORCE_CLOSED"


PRICE_INDICATORS = {"last", "bid", "ask", "spread_pct"}
ALLOWED_INDICATORS = PRICE_INDICATORS | {
    "previous_open",
    "previous_high",
    "previous_low",
    "previous_close",
    "opening_range_5_high",
    "opening_range_5_low",
    "opening_range_10_high",
    "opening_range_10_low",
    "opening_range_15_high",
    "opening_range_15_low",
    "vwap_regular",
    "atr_14_1m_regular",
    "ema_9_1m_regular",
    "ema_20_1m_regular",
    "ema_50_1m_regular",
    "rsi_14_1m_regular",
    "relative_volume_cumulative_20d_same_time_regular",
    "gap_pct",
    "market_above_vwap_regular",
    "recent_high_5_1m_regular",
    "recent_low_5_1m_regular",
    "bar_volume_1m_regular",
}


class Predicate(BaseModel):
    indicator: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "cross_above", "cross_below"]
    value: float | bool | str
    confirm_ticks: int = Field(default=1, ge=1, le=20)
    hold_above_ms: int = Field(default=0, ge=0, le=60_000)
    minimum_cross_pct: float = Field(default=0.0, ge=0, le=5)
    cooldown_sec: int = Field(default=0, ge=0, le=3600)

    @model_validator(mode="after")
    def validate_indicator(self) -> "Predicate":
        if self.indicator not in ALLOWED_INDICATORS:
            raise ValueError(f"unsupported indicator: {self.indicator}")
        if isinstance(self.value, str) and self.value not in ALLOWED_INDICATORS:
            raise ValueError(f"unsupported referenced indicator: {self.value}")
        if self.operator not in {"cross_above", "cross_below"} and (
            self.confirm_ticks != 1
            or self.hold_above_ms
            or self.minimum_cross_pct
            or self.cooldown_sec
        ):
            raise ValueError("debounce fields are only valid for cross operators")
        return self


class RuleGroup(BaseModel):
    mode: Literal["all", "any"] = "all"
    predicates: list[Predicate] = Field(default_factory=list, max_length=12)
    groups: list["RuleGroup"] = Field(default_factory=list, max_length=4)

    @property
    def empty(self) -> bool:
        return not self.predicates and not self.groups

    def validate_depth(self, depth: int = 1) -> None:
        if depth > 4:
            raise ValueError("rule nesting exceeds four levels")
        for group in self.groups:
            if group.empty:
                raise ValueError("nested rule groups cannot be empty")
            group.validate_depth(depth + 1)


class EntrySpec(BaseModel):
    trigger_price: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    start_time: time
    end_time: time
    price_only: bool = False
    rules: RuleGroup = Field(default_factory=RuleGroup)

    @model_validator(mode="after")
    def validate_window(self) -> "EntrySpec":
        if self.end_time <= self.start_time:
            raise ValueError("entry end_time must be after start_time")
        if self.limit_price < self.trigger_price:
            raise ValueError("limit_price must be at or above trigger_price")
        if self.price_only and not self.rules.empty:
            raise ValueError("price_only entry cannot also contain indicator rules")
        self.rules.validate_depth()
        return self


class StopLossSpec(BaseModel):
    price: float = Field(gt=0)
    execution_type: Literal["marketable_limit"] = "marketable_limit"
    limit_offset_pct: float = Field(default=0.2, ge=0.05, le=2.0)
    emergency_exit_after_sec: float = Field(default=2.0, ge=0.1, le=30)


class TakeProfitSpec(BaseModel):
    price: float = Field(gt=0)
    quantity_pct: int = Field(gt=0, le=100)


class ExitPolicy(BaseModel):
    max_holding_minutes: int = Field(default=90, ge=1, le=360)
    no_progress_exit_minutes: int | None = Field(default=30, ge=1, le=180)
    no_progress_min_pct: float = Field(default=0.3, ge=0, le=5)
    exit_if_below_vwap_sec: int | None = Field(default=60, ge=1, le=600)
    move_stop_to_entry_after_tp1: bool = False


class PullbackRebreakSpec(BaseModel):
    breakout_confirm_ticks: int = Field(default=3, ge=1, le=20)
    breakout_hold_ms: int = Field(default=1000, ge=0, le=60_000)
    breakout_relative_volume_min: float = Field(default=1.5, ge=0.1, le=10)
    pullback_depth_atr_min: float = Field(default=0.2, ge=0.05, le=3)
    pullback_depth_atr_max: float = Field(default=0.8, ge=0.1, le=5)
    pullback_min_minutes: float = Field(default=2, ge=0.5, le=30)
    pullback_max_minutes: float = Field(default=10, ge=1, le=60)
    pullback_rsi_min: float = Field(default=45, ge=0, le=100)
    pullback_rsi_max: float = Field(default=60, ge=0, le=100)
    pullback_volume_ratio_max: float = Field(default=0.7, gt=0, le=2)
    support_breach_pct_max: float = Field(default=0.15, ge=0, le=2)
    rebreak_rsi_min: float = Field(default=50, ge=0, le=100)
    rebreak_rsi_max: float = Field(default=68, ge=0, le=100)
    rebreak_relative_volume_min: float = Field(default=1.3, ge=0.1, le=10)
    spread_pct_max: float = Field(default=0.15, gt=0, le=2)
    require_market_above_vwap: bool = True

    @model_validator(mode="after")
    def validate_ranges(self) -> "PullbackRebreakSpec":
        if self.pullback_depth_atr_min >= self.pullback_depth_atr_max:
            raise ValueError("pullback ATR minimum must be below maximum")
        if self.pullback_min_minutes >= self.pullback_max_minutes:
            raise ValueError("pullback minimum duration must be below maximum")
        if self.pullback_rsi_min > self.pullback_rsi_max:
            raise ValueError("pullback RSI range is invalid")
        if self.rebreak_rsi_min > self.rebreak_rsi_max:
            raise ValueError("rebreak RSI range is invalid")
        return self


class CandidatePlan(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    exchange: str = Field(min_length=2, max_length=16)
    reason: str = Field(min_length=5, max_length=1000)
    strategy_type: Literal["rules", "pullback_rebreak"] = "rules"
    entry: EntrySpec
    stop_loss: StopLossSpec
    take_profit: list[TakeProfitSpec] = Field(min_length=1, max_length=3)
    exit_policy: ExitPolicy = Field(default_factory=ExitPolicy)
    pullback_rebreak: PullbackRebreakSpec | None = None
    force_exit_time: time

    @model_validator(mode="after")
    def validate_prices(self) -> "CandidatePlan":
        self.symbol = self.symbol.upper()
        self.exchange = self.exchange.upper()
        if self.strategy_type == "rules":
            if not self.entry.price_only and self.entry.rules.empty:
                raise ValueError("empty rules require price_only=true")
            if self.pullback_rebreak is not None:
                raise ValueError("pullback_rebreak config requires pullback strategy_type")
        else:
            if self.entry.price_only or not self.entry.rules.empty:
                raise ValueError(
                    "pullback_rebreak requires price_only=false and an empty generic rule group"
                )
            if self.pullback_rebreak is None:
                self.pullback_rebreak = PullbackRebreakSpec()
        if self.stop_loss.price >= self.entry.trigger_price:
            raise ValueError("stop loss must be below entry trigger")
        prices = [target.price for target in self.take_profit]
        if len(set(prices)) != len(prices):
            raise ValueError("take-profit prices must be unique")
        if prices != sorted(prices) or prices[0] <= self.entry.limit_price:
            raise ValueError("take profits must increase above the maximum entry limit")
        if sum(target.quantity_pct for target in self.take_profit) != 100:
            raise ValueError("take-profit quantities must total 100%")
        risk = self.entry.limit_price - self.stop_loss.price
        reward = prices[0] - self.entry.limit_price
        if reward / risk < 1.5:
            raise ValueError("first target reward/risk must be at least 1.5 at limit_price")
        stop_pct = risk / self.entry.limit_price * 100
        if not 0.25 <= stop_pct <= 3.0:
            raise ValueError("stop distance from limit_price must be between 0.25% and 3.0%")
        return self


class TradePlan(BaseModel):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    plan_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    market: Market
    trade_date: date
    expires_at: datetime
    approval_nonce: str = Field(pattern=r"^\d{6}$")
    approved_symbols: list[CandidatePlan] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_plan_identity(self) -> "TradePlan":
        symbols = [candidate.symbol.upper() for candidate in self.approved_symbols]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate symbols are not allowed")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return self


class MarketTick(BaseModel):
    market: Market
    symbol: str
    timestamp: datetime
    source_timestamp: datetime
    received_timestamp: datetime
    sequence_id: int = Field(ge=0)
    connection_id: str = Field(min_length=1, max_length=80)
    data_source: str = Field(min_length=1, max_length=80)
    quote_scope: Literal["consolidated", "venue", "unknown"]
    session: Literal["premarket", "regular", "afterhours", "closed"]
    market_status: Literal["open", "closed", "halted"]
    symbol_status: Literal["trading", "halted", "paused"]
    luld_status: Literal["normal", "limit_up", "limit_down", "paused"]
    halt_reason: str | None = Field(default=None, max_length=200)
    corporate_action: bool = False
    last: float = Field(gt=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: int = Field(ge=0, default=0)
    ask_size: int = Field(ge=0, default=0)
    trade_size: int = Field(ge=0, default=0)
    indicators: dict[str, float | bool] = Field(default_factory=dict)
    indicator_timestamps: dict[str, datetime] = Field(default_factory=dict)
    indicator_ready: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_quote(self) -> "MarketTick":
        timestamps = [self.timestamp, self.source_timestamp, self.received_timestamp]
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("all tick timestamps must include a timezone")
        values: list[float | bool] = [self.last, self.bid, self.ask, *self.indicators.values()]
        if any(isinstance(value, float) and not math.isfinite(value) for value in values):
            raise ValueError("tick values cannot contain NaN or Infinity")
        unknown = set(self.indicators) - ALLOWED_INDICATORS
        if unknown:
            raise ValueError(f"unsupported indicators in tick: {sorted(unknown)}")
        reserved = set(self.indicators) & PRICE_INDICATORS
        if reserved:
            raise ValueError(f"price fields cannot be overridden in indicators: {sorted(reserved)}")
        if set(self.indicator_timestamps) - set(self.indicators):
            raise ValueError("indicator_timestamps contains an indicator without a value")
        if set(self.indicator_ready) - set(self.indicators):
            raise ValueError("indicator_ready contains an indicator without a value")
        if any(value.tzinfo is None for value in self.indicator_timestamps.values()):
            raise ValueError("indicator timestamps must include a timezone")
        return self


class PlanReceipt(BaseModel):
    plan_id: str
    status: PlanStatus
    content_hash: str
    message: str


class NonceRequest(BaseModel):
    market: Market
    trade_date: date


class NonceResponse(BaseModel):
    market: Market
    trade_date: date
    nonce: str
    expires_at: datetime


class PortfolioView(BaseModel):
    market: Market
    cash: float
    equity: float
    realized_pnl: float
    positions: list[dict[str, Any]]
