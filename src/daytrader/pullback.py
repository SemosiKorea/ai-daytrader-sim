from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean

from .market_clock import MARKET_TZ
from .models import CandidatePlan, MarketTick
from .repository import Repository


@dataclass
class PullbackDecision:
    signal: bool
    phase: str
    reasons: list[str]
    metrics: dict


@dataclass
class PullbackRuntimeState:
    phase: str = "WAIT_BREAKOUT"
    session_date: str | None = None
    connection_id: str | None = None
    breakout_candidate_started: datetime | None = None
    breakout_confirm_count: int = 0
    breakout_price: float | None = None
    breakout_at: datetime | None = None
    impulse_high: float | None = None
    atr_at_breakout: float | None = None
    pullback_started_at: datetime | None = None
    pullback_low: float | None = None
    pullback_high: float | None = None
    pullback_depth_atr: float | None = None
    prior_breakout_confirmed: bool = False
    impulse_volumes: list[float] = field(default_factory=list)
    pullback_volumes: list[float] = field(default_factory=list)
    last_volume_timestamp: str | None = None

    def payload(self) -> dict:
        return {
            **self.__dict__,
            "breakout_candidate_started": (
                self.breakout_candidate_started.isoformat()
                if self.breakout_candidate_started
                else None
            ),
            "breakout_at": self.breakout_at.isoformat() if self.breakout_at else None,
            "pullback_started_at": (
                self.pullback_started_at.isoformat() if self.pullback_started_at else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "PullbackRuntimeState":
        values = dict(payload)
        for key in ("breakout_candidate_started", "breakout_at", "pullback_started_at"):
            if values.get(key):
                values[key] = datetime.fromisoformat(values[key])
        return cls(**values)


class PullbackRebreakEngine:
    REQUIRED_INDICATORS = {
        "opening_range_5_high",
        "vwap_regular",
        "atr_14_1m_regular",
        "ema_9_1m_regular",
        "ema_20_1m_regular",
        "ema_50_1m_regular",
        "rsi_14_1m_regular",
        "relative_volume_cumulative_20d_same_time_regular",
        "market_above_vwap_regular",
        "recent_high_5_1m_regular",
        "recent_low_5_1m_regular",
        "bar_volume_1m_regular",
    }

    def __init__(self, repository: Repository):
        self.repository = repository
        self.states: dict[tuple[str, str], PullbackRuntimeState] = {}

    def _state(self, plan_id: str, symbol: str) -> PullbackRuntimeState:
        key = (plan_id, symbol.upper())
        if key not in self.states:
            stored = self.repository.load_strategy_state(*key)
            self.states[key] = (
                PullbackRuntimeState.from_payload(stored) if stored else PullbackRuntimeState()
            )
        return self.states[key]

    def phase(self, plan_id: str, symbol: str) -> str:
        return self._state(plan_id, symbol).phase

    def _save(
        self, plan_id: str, candidate: CandidatePlan, tick: MarketTick, state: PullbackRuntimeState
    ) -> None:
        self.repository.save_strategy_state(
            plan_id, candidate.symbol, tick.market, state.payload()
        )

    def _transition(
        self,
        plan_id: str,
        candidate: CandidatePlan,
        tick: MarketTick,
        state: PullbackRuntimeState,
        phase: str,
        reason: str,
    ) -> None:
        previous = state.phase
        state.phase = phase
        self._save(plan_id, candidate, tick, state)
        if previous == phase:
            return
        self.repository.add_event(
            "STRATEGY_STATE_CHANGED",
            tick.market,
            candidate.symbol,
            plan_id,
            {
                "strategy": "pullback_rebreak",
                "from": previous,
                "to": phase,
                "reason": reason,
                "metrics": self.metrics(state),
            },
        )

    @staticmethod
    def _reset_values(state: PullbackRuntimeState) -> None:
        state.breakout_candidate_started = None
        state.breakout_confirm_count = 0
        state.breakout_price = None
        state.breakout_at = None
        state.impulse_high = None
        state.atr_at_breakout = None
        state.pullback_started_at = None
        state.pullback_low = None
        state.pullback_high = None
        state.pullback_depth_atr = None
        state.prior_breakout_confirmed = False
        state.impulse_volumes.clear()
        state.pullback_volumes.clear()
        state.last_volume_timestamp = None

    def reset(
        self, plan_id: str, candidate: CandidatePlan, tick: MarketTick, reason: str
    ) -> PullbackRuntimeState:
        state = self._state(plan_id, candidate.symbol)
        self._reset_values(state)
        state.session_date = (
            (tick.source_timestamp or tick.timestamp)
            .astimezone(MARKET_TZ[tick.market])
            .date()
            .isoformat()
        )
        state.connection_id = tick.connection_id
        self._transition(plan_id, candidate, tick, state, "WAIT_BREAKOUT", reason)
        return state

    @staticmethod
    def metrics(state: PullbackRuntimeState) -> dict:
        volume_ratio = None
        if state.impulse_volumes and state.pullback_volumes:
            impulse = fmean(state.impulse_volumes)
            volume_ratio = fmean(state.pullback_volumes) / impulse if impulse > 0 else None
        return {
            "prior_breakout_confirmed": state.prior_breakout_confirmed,
            "breakout_price": state.breakout_price,
            "impulse_high": state.impulse_high,
            "atr_at_breakout": state.atr_at_breakout,
            "pullback_low": state.pullback_low,
            "pullback_high": state.pullback_high,
            "pullback_depth_atr": state.pullback_depth_atr,
            "pullback_volume_ratio": volume_ratio,
        }

    @staticmethod
    def _record_volume(
        state: PullbackRuntimeState, tick: MarketTick, *, pullback: bool
    ) -> None:
        name = "bar_volume_1m_regular"
        generated = tick.indicator_timestamps.get(name)
        if generated is None:
            return
        key = generated.isoformat()
        if key == state.last_volume_timestamp:
            return
        state.last_volume_timestamp = key
        volume = float(tick.indicators[name])
        target = state.pullback_volumes if pullback else state.impulse_volumes
        target.append(volume)
        if len(target) > 20:
            target.pop(0)

    @staticmethod
    def _support_intact(values: dict, max_breach_pct: float) -> bool:
        floor_multiplier = 1 - max_breach_pct / 100
        return values["last"] >= max(
            values["vwap_regular"] * floor_multiplier,
            values["ema_20_1m_regular"] * floor_multiplier,
        )

    def evaluate(
        self,
        plan_id: str,
        candidate: CandidatePlan,
        tick: MarketTick,
        previous: dict | None,
        *,
        entry_allowed: bool = True,
    ) -> PullbackDecision:
        spec = candidate.pullback_rebreak
        if spec is None:
            return PullbackDecision(False, "DATA_ERROR", ["PULLBACK_CONFIG_MISSING"], {})
        state = self._state(plan_id, candidate.symbol)
        timestamp = tick.source_timestamp or tick.timestamp
        session_date = timestamp.astimezone(MARKET_TZ[tick.market]).date().isoformat()
        if state.session_date and (
            state.session_date != session_date or state.connection_id != tick.connection_id
        ):
            state = self.reset(plan_id, candidate, tick, "DATA_CONTEXT_RESET")
        else:
            state.session_date = session_date
            state.connection_id = tick.connection_id

        values = {"last": tick.last, "spread_pct": (tick.ask - tick.bid) / tick.last * 100}
        values.update(tick.indicators)
        atr = float(values["atr_14_1m_regular"])
        if atr <= 0:
            return PullbackDecision(False, state.phase, ["ATR_NOT_POSITIVE"], self.metrics(state))

        if state.phase == "WAIT_BREAKOUT":
            opening_high = float(values["opening_range_5_high"])
            aligned = (
                values["ema_9_1m_regular"]
                > values["ema_20_1m_regular"]
                > values["ema_50_1m_regular"]
            )
            strong = (
                values["last"] > opening_high
                and values["last"] > values["vwap_regular"]
                and aligned
                and values["relative_volume_cumulative_20d_same_time_regular"]
                >= spec.breakout_relative_volume_min
            )
            crossed = bool(
                previous
                and previous.get("last", values["last"]) <= opening_high
                and values["last"] > opening_high
            )
            if crossed and strong:
                state.impulse_volumes.clear()
                state.pullback_volumes.clear()
                state.last_volume_timestamp = None
                state.breakout_candidate_started = timestamp
                state.breakout_confirm_count = 1
                state.breakout_price = float(values["last"])
                state.impulse_high = float(values["last"])
                state.atr_at_breakout = atr
            elif state.breakout_confirm_count and strong:
                state.breakout_confirm_count += 1
                state.impulse_high = max(state.impulse_high or 0, float(values["last"]))
            else:
                state.breakout_candidate_started = None
                state.breakout_confirm_count = 0
                state.breakout_price = None
                state.impulse_high = None
            if state.breakout_confirm_count:
                self._record_volume(state, tick, pullback=False)
            held_ms = (
                (timestamp - state.breakout_candidate_started).total_seconds() * 1000
                if state.breakout_candidate_started
                else 0
            )
            if (
                state.breakout_confirm_count >= spec.breakout_confirm_ticks
                and held_ms >= spec.breakout_hold_ms
            ):
                state.prior_breakout_confirmed = True
                state.breakout_at = timestamp
                self._transition(
                    plan_id, candidate, tick, state, "WAIT_PULLBACK", "BREAKOUT_CONFIRMED"
                )
            else:
                self._save(plan_id, candidate, tick, state)
            return PullbackDecision(False, state.phase, ["WAITING_FOR_BREAKOUT"], self.metrics(state))

        if state.phase == "SIGNAL_TRIGGERED":
            return PullbackDecision(
                True, state.phase, ["RECOVERING_SIGNAL"], self.metrics(state)
            )
        if state.phase in {
            "ENTRY_PENDING",
            "POSITION_OPEN",
            "RISK_BLOCKED",
            "CLOSED",
            "CANCELLED",
            "EXPIRED",
        }:
            return PullbackDecision(False, state.phase, [state.phase], self.metrics(state))

        if state.phase == "WAIT_PULLBACK" and values["last"] > (
            state.impulse_high or values["last"]
        ):
            state.impulse_high = float(values["last"])
            state.pullback_low = float(values["last"])
            state.pullback_started_at = None
            state.pullback_volumes.clear()
            self._record_volume(state, tick, pullback=False)
        else:
            state.impulse_high = max(
                state.impulse_high or values["last"], float(values["last"])
            )
            state.pullback_low = min(
                state.pullback_low or values["last"], float(values["last"])
            )
        depth_atr = (state.impulse_high - state.pullback_low) / (
            state.atr_at_breakout or atr
        )
        state.pullback_depth_atr = depth_atr
        support_intact = self._support_intact(values, spec.support_breach_pct_max)
        if depth_atr > spec.pullback_depth_atr_max or not support_intact:
            state = self.reset(plan_id, candidate, tick, "PULLBACK_INVALIDATED")
            return PullbackDecision(
                False, state.phase, ["PULLBACK_INVALIDATED"], self.metrics(state)
            )

        if state.phase == "WAIT_PULLBACK":
            if depth_atr < spec.pullback_depth_atr_min:
                self._record_volume(state, tick, pullback=False)
                self._save(plan_id, candidate, tick, state)
                return PullbackDecision(
                    False, state.phase, ["PULLBACK_TOO_SHALLOW"], self.metrics(state)
                )
            state.pullback_started_at = state.pullback_started_at or timestamp
            self._record_volume(state, tick, pullback=True)
            elapsed_minutes = (timestamp - state.pullback_started_at).total_seconds() / 60
            if elapsed_minutes > spec.pullback_max_minutes:
                state = self.reset(plan_id, candidate, tick, "PULLBACK_TIMEOUT")
                return PullbackDecision(False, state.phase, ["PULLBACK_TIMEOUT"], self.metrics(state))
            metrics = self.metrics(state)
            ratio = metrics["pullback_volume_ratio"]
            rsi_ready = spec.pullback_rsi_min <= values["rsi_14_1m_regular"] <= spec.pullback_rsi_max
            if (
                elapsed_minutes >= spec.pullback_min_minutes
                and rsi_ready
                and ratio is not None
                and ratio <= spec.pullback_volume_ratio_max
            ):
                state.pullback_high = max(
                    float(values["last"]),
                    min(float(values["recent_high_5_1m_regular"]), state.impulse_high),
                )
                self._transition(
                    plan_id, candidate, tick, state, "WAIT_REBREAK", "PULLBACK_CONFIRMED"
                )
                return PullbackDecision(
                    False, state.phase, ["WAITING_FOR_REBREAK"], self.metrics(state)
                )
            reasons = []
            if elapsed_minutes < spec.pullback_min_minutes:
                reasons.append("PULLBACK_DURATION_SHORT")
            if not rsi_ready:
                reasons.append("PULLBACK_RSI_OUT_OF_RANGE")
            if ratio is None:
                reasons.append("PULLBACK_VOLUME_NOT_READY")
            elif ratio > spec.pullback_volume_ratio_max:
                reasons.append("PULLBACK_VOLUME_TOO_HIGH")
            self._save(plan_id, candidate, tick, state)
            return PullbackDecision(False, state.phase, reasons, self.metrics(state))

        if state.phase == "WAIT_REBREAK":
            if state.pullback_started_at and (
                timestamp - state.pullback_started_at
            ).total_seconds() / 60 > spec.pullback_max_minutes:
                state = self.reset(plan_id, candidate, tick, "REBREAK_TIMEOUT")
                return PullbackDecision(False, state.phase, ["REBREAK_TIMEOUT"], self.metrics(state))
            level = state.pullback_high
            crossed = bool(
                level is not None
                and previous
                and previous.get("last", values["last"]) <= level
                and values["last"] > level
            )
            conditions = {
                "REBREAK_NOT_CROSSED": crossed,
                "BELOW_EMA9": values["last"] > values["ema_9_1m_regular"],
                "BELOW_VWAP": values["last"] > values["vwap_regular"],
                "RSI_OUT_OF_RANGE": spec.rebreak_rsi_min
                <= values["rsi_14_1m_regular"]
                <= spec.rebreak_rsi_max,
                "RELATIVE_VOLUME_LOW": values[
                    "relative_volume_cumulative_20d_same_time_regular"
                ]
                >= spec.rebreak_relative_volume_min,
                "SPREAD_TOO_WIDE": values["spread_pct"] <= spec.spread_pct_max,
                "MARKET_BELOW_VWAP": (
                    not spec.require_market_above_vwap
                    or values["market_above_vwap_regular"] is True
                ),
                "ENTRY_TRIGGER_NOT_REACHED": values["last"] >= candidate.entry.trigger_price,
                "OUTSIDE_ENTRY_WINDOW": entry_allowed,
            }
            failures = [reason for reason, passed in conditions.items() if not passed]
            if not failures:
                self._transition(
                    plan_id, candidate, tick, state, "SIGNAL_TRIGGERED", "PULLBACK_REBREAK"
                )
                return PullbackDecision(True, state.phase, [], self.metrics(state))
            self._save(plan_id, candidate, tick, state)
            return PullbackDecision(False, state.phase, failures, self.metrics(state))

        return PullbackDecision(False, state.phase, ["UNKNOWN_STRATEGY_STATE"], self.metrics(state))

    def mark_submission(
        self,
        plan_id: str,
        candidate: CandidatePlan,
        tick: MarketTick,
        accepted: bool,
        reason: str | None,
    ) -> None:
        state = self._state(plan_id, candidate.symbol)
        phase = "ENTRY_PENDING" if accepted else "RISK_BLOCKED"
        self._transition(
            plan_id,
            candidate,
            tick,
            state,
            phase,
            "ORDER_SUBMITTED" if accepted else (reason or "RISK_BLOCKED"),
        )

    def mark_terminal(
        self,
        plan_id: str,
        candidate: CandidatePlan,
        tick: MarketTick,
        phase: str,
        reason: str,
    ) -> None:
        state = self._state(plan_id, candidate.symbol)
        self._transition(plan_id, candidate, tick, state, phase, reason)
