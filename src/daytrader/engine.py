from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .broker import PaperBroker
from .market_clock import MARKET_TZ, force_exit_at, is_session
from .models import (
    PRICE_INDICATORS,
    CandidatePlan,
    Market,
    MarketTick,
    OrderState,
    PlanStatus,
    RuleGroup,
    TradePlan,
)
from .pullback import PullbackRebreakEngine
from .repository import Repository
from .rules import CrossDebounceState, evaluate_group


@dataclass
class FeedState:
    source_timestamp: datetime
    received_timestamp: datetime
    sequence_id: int | None
    connection_id: str
    session: str
    halted: bool = False
    warmup_until: datetime | None = None
    context_reset: bool = False


class TradingEngine:
    def __init__(self, repository: Repository, broker: PaperBroker):
        self.repository = repository
        self.broker = broker
        self.latest_ticks: dict[tuple[Market, str], MarketTick] = {}
        self.previous_context: dict[tuple[Market, str], dict] = {}
        self.feed_states: dict[tuple[Market, str], FeedState] = {}
        self.cross_states: dict[tuple[str, str], dict[str, CrossDebounceState]] = {}
        self.pullback = PullbackRebreakEngine(repository)

    @staticmethod
    def context(tick: MarketTick) -> dict:
        spread_pct = (tick.ask - tick.bid) / tick.last * 100
        return {
            "last": tick.last,
            "bid": tick.bid,
            "ask": tick.ask,
            "spread_pct": spread_pct,
            **tick.indicators,
        }

    @staticmethod
    def _rule_indicators(group: RuleGroup) -> set[str]:
        indicators = {predicate.indicator for predicate in group.predicates}
        indicators.update(
            predicate.value
            for predicate in group.predicates
            if isinstance(predicate.value, str)
        )
        for child in group.groups:
            indicators.update(TradingEngine._rule_indicators(child))
        return indicators

    def _log_rejection(
        self,
        tick: MarketTick,
        plan_id: str | None,
        reasons: list[str],
        event_type: str = "ENTRY_REJECTED",
        details: dict | None = None,
    ) -> None:
        self.repository.add_event(
            event_type,
            tick.market,
            tick.symbol,
            plan_id,
            {
                "reasons": reasons,
                "snapshot": {
                    "source_timestamp": tick.source_timestamp,
                    "received_timestamp": tick.received_timestamp,
                    "sequence_id": tick.sequence_id,
                    "session": tick.session,
                    "last": tick.last,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread_pct": (tick.ask - tick.bid) / tick.last * 100,
                    "market_status": tick.market_status,
                    "symbol_status": tick.symbol_status,
                },
                "details": details or {},
            },
        )

    def _validate_feed(self, tick: MarketTick) -> list[str]:
        reasons = []
        now = datetime.now(UTC)
        source = (tick.source_timestamp or tick.timestamp).astimezone(UTC)
        received = (tick.received_timestamp or now).astimezone(UTC)
        policy = self.broker.costs[tick.market.value]
        if (now - source).total_seconds() > policy.max_tick_age_seconds:
            reasons.append("STALE_DATA")
        if source - now > timedelta(seconds=1) or received - now > timedelta(seconds=1):
            reasons.append("FUTURE_TIMESTAMP")
        if source - received > timedelta(seconds=1):
            reasons.append("SOURCE_AFTER_RECEIVED")
        if tick.bid > tick.ask:
            reasons.append("CROSSED_MARKET")
        key = (tick.market, tick.symbol.upper())
        state = self.feed_states.get(key)
        if state and state.connection_id == tick.connection_id:
            if source < state.source_timestamp.astimezone(UTC):
                reasons.append("SOURCE_TIME_REVERSED")
            if received < state.received_timestamp.astimezone(UTC):
                reasons.append("RECEIVED_TIME_REVERSED")
            if (
                tick.sequence_id is not None
                and state.sequence_id is not None
                and tick.sequence_id <= state.sequence_id
            ):
                reasons.append("SEQUENCE_REVERSED")
        return reasons

    def _update_feed_state(self, tick: MarketTick) -> FeedState:
        key = (tick.market, tick.symbol.upper())
        source = tick.source_timestamp or tick.timestamp
        received = tick.received_timestamp or datetime.now(UTC)
        prior = self.feed_states.get(key)
        reset = prior and (
            prior.connection_id != tick.connection_id or prior.session != tick.session
        )
        halted_now = (
            tick.market_status != "open"
            or tick.symbol_status != "trading"
            or tick.luld_status != "normal"
        )
        resumed = prior and prior.halted and not halted_now
        cooldown = self.broker.costs[tick.market.value].halt_resume_cooldown_seconds
        state = FeedState(
            source_timestamp=source,
            received_timestamp=received,
            sequence_id=tick.sequence_id,
            connection_id=tick.connection_id,
            session=tick.session,
            halted=halted_now,
            warmup_until=(received + timedelta(seconds=cooldown) if reset or resumed else None),
            context_reset=bool(reset or resumed),
        )
        if prior and not (reset or resumed) and prior.warmup_until:
            state.warmup_until = prior.warmup_until
        self.feed_states[key] = state
        if reset or resumed or halted_now:
            self.previous_context.pop(key, None)
            for cross_key in [value for value in self.cross_states if value[1] == key[1]]:
                self.cross_states.pop(cross_key, None)
        return state

    def _indicators_ready(self, candidate: CandidatePlan, tick: MarketTick) -> list[str]:
        if candidate.strategy_type == "pullback_rebreak":
            required = self.pullback.REQUIRED_INDICATORS
        elif candidate.entry.price_only:
            return []
        else:
            required = self._rule_indicators(candidate.entry.rules) - PRICE_INDICATORS
        reasons = []
        source = tick.source_timestamp or tick.timestamp
        max_age = self.broker.costs[tick.market.value].indicator_max_age_seconds
        for indicator in required:
            if indicator not in tick.indicators:
                reasons.append(f"INDICATOR_MISSING:{indicator}")
                continue
            if not tick.indicator_ready.get(indicator, False):
                reasons.append(f"INDICATOR_NOT_READY:{indicator}")
                continue
            generated_at = tick.indicator_timestamps.get(indicator)
            if generated_at is None:
                reasons.append(f"INDICATOR_TIMESTAMP_MISSING:{indicator}")
                continue
            if generated_at.tzinfo is None:
                reasons.append(f"INDICATOR_TIMESTAMP_NAIVE:{indicator}")
                continue
            allowed_age = max_age
            if indicator.startswith("previous_"):
                allowed_age = 7 * 24 * 3600
            elif indicator.startswith("opening_range_") or indicator == "gap_pct":
                allowed_age = 24 * 3600
            elif "_1m_" in indicator:
                allowed_age = max(90.0, max_age)
            age = (source - generated_at).total_seconds()
            if age < -0.1 or age > allowed_age:
                reasons.append(f"INDICATOR_STALE:{indicator}")
        return reasons

    def _eligible_time(self, candidate: CandidatePlan, tick: MarketTick) -> bool:
        source = tick.source_timestamp or tick.timestamp
        local_time = source.astimezone(MARKET_TZ[tick.market]).time().replace(tzinfo=None)
        return candidate.entry.start_time <= local_time <= candidate.entry.end_time

    def _premarket_guard_reasons(
        self, plan: TradePlan, candidate: CandidatePlan, tick: MarketTick
    ) -> list[str]:
        guard = candidate.premarket_guard
        if guard is None:
            return []
        existing = self.repository.candidate_guard_state(plan.plan_id, candidate.symbol)
        if existing:
            return [f"CANDIDATE_RISK_BLOCKED:{existing['reason']}"]
        if not tick.indicator_ready.get("regular_open_price", False):
            return ["REGULAR_OPEN_NOT_READY"]
        opening_price = float(tick.indicators["regular_open_price"])
        deviation = abs(opening_price - guard.reference_price) / guard.reference_price * 100
        if deviation > guard.max_open_deviation_pct:
            payload = {
                "reference_price": guard.reference_price,
                "regular_open_price": opening_price,
                "open_deviation_pct": deviation,
                "max_open_deviation_pct": guard.max_open_deviation_pct,
            }
            self.repository.block_candidate(
                plan.plan_id,
                candidate.symbol,
                tick.market,
                "OPEN_DEVIATION_EXCEEDED",
                payload,
            )
            self.repository.add_event(
                "CANDIDATE_RISK_BLOCKED",
                tick.market,
                candidate.symbol,
                plan.plan_id,
                {"reason": "OPEN_DEVIATION_EXCEEDED", **payload},
            )
            blocked = {
                state["symbol"]
                for state in self.repository.candidate_guard_states(plan.plan_id)
            }
            if blocked.issuperset({item.symbol for item in plan.approved_symbols}):
                self.repository.set_plan_status(plan.plan_id, PlanStatus.RISK_BLOCKED)
            return ["OPEN_DEVIATION_EXCEEDED"]
        reasons: list[str] = []
        spread = (tick.ask - tick.bid) / tick.last * 100
        if spread > guard.max_spread_pct:
            reasons.append("REGULAR_SPREAD_CONFIRMATION_FAILED")
        relative_name = "relative_volume_cumulative_20d_same_time_regular"
        if not tick.indicator_ready.get(relative_name, False):
            reasons.append("REGULAR_RELATIVE_VOLUME_NOT_READY")
        elif float(tick.indicators[relative_name]) < guard.relative_volume_min:
            reasons.append("REGULAR_RELATIVE_VOLUME_TOO_LOW")
        if guard.require_above_vwap:
            if not tick.indicator_ready.get("vwap_regular", False):
                reasons.append("REGULAR_VWAP_NOT_READY")
            elif tick.last <= float(tick.indicators["vwap_regular"]):
                reasons.append("PRICE_NOT_ABOVE_REGULAR_VWAP")
        if guard.require_market_above_vwap:
            market_name = "market_above_vwap_regular"
            if not tick.indicator_ready.get(market_name, False):
                reasons.append("MARKET_VWAP_NOT_READY")
            elif not bool(tick.indicators[market_name]):
                reasons.append("MARKET_NOT_ABOVE_VWAP")
        return reasons

    def _try_entry(
        self, plan: TradePlan, candidate: CandidatePlan, tick: MarketTick, feed: FeedState
    ) -> None:
        reasons = []
        source = tick.source_timestamp or tick.timestamp
        received = tick.received_timestamp or source
        entry_allowed = self._eligible_time(candidate, tick)
        local_time = source.astimezone(MARKET_TZ[tick.market]).time().replace(tzinfo=None)
        if candidate.strategy_type == "rules" and not entry_allowed:
            reasons.append("OUTSIDE_ENTRY_WINDOW")
        if (
            candidate.strategy_type == "pullback_rebreak"
            and local_time > candidate.entry.end_time
        ):
            reasons.append("OUTSIDE_ENTRY_WINDOW")
        if tick.session != "regular":
            reasons.append("NOT_REGULAR_SESSION")
        if tick.market == Market.US and tick.quote_scope != "consolidated":
            reasons.append("NON_CONSOLIDATED_QUOTE")
        if tick.market_status != "open" or tick.symbol_status != "trading":
            reasons.append("MARKET_HALTED")
        if tick.luld_status != "normal":
            reasons.append("LULD_NOT_NORMAL")
        if feed.warmup_until and received < feed.warmup_until:
            reasons.append("RECONNECT_WARMUP")
        if tick.bid == tick.ask:
            reasons.append("LOCKED_MARKET")
        spread = (tick.ask - tick.bid) / tick.last * 100
        if spread > self.broker.costs[tick.market.value].max_spread_pct:
            reasons.append("SPREAD_TOO_WIDE")
        if candidate.strategy_type == "rules" and tick.last < candidate.entry.trigger_price:
            reasons.append("TRIGGER_NOT_REACHED")
        if tick.ask > candidate.entry.limit_price:
            reasons.append("MAX_BUY_LIMIT_EXCEEDED")
        reasons.extend(self._premarket_guard_reasons(plan, candidate, tick))
        reasons.extend(self._indicators_ready(candidate, tick))
        if reasons:
            self._log_rejection(tick, plan.plan_id, reasons)
            return
        context = self.context(tick)
        key = (tick.market, tick.symbol.upper())
        previous = self.previous_context.get(key)
        if candidate.strategy_type == "pullback_rebreak":
            portfolio = self.broker.portfolios[tick.market]
            existing_order = portfolio.pending_orders.get(candidate.symbol)
            existing_position = portfolio.positions.get(candidate.symbol)
            if existing_position and existing_position.plan_id == plan.plan_id:
                self.pullback.mark_terminal(
                    plan.plan_id,
                    candidate,
                    tick,
                    "POSITION_OPEN",
                    "POSITION_CONFIRMED",
                )
                return
            if existing_order and existing_order.plan_id == plan.plan_id:
                self.pullback.mark_submission(
                    plan.plan_id, candidate, tick, True, "ORDER_CONFIRMED"
                )
                return
            if self.pullback.phase(plan.plan_id, candidate.symbol) == "ENTRY_PENDING":
                latest = self.repository.latest_order_state(
                    plan.plan_id, candidate.symbol
                )
                if latest in {
                    OrderState.EXPIRED.value,
                    OrderState.CANCELLED.value,
                    OrderState.MARKET_HALTED.value,
                    OrderState.FORCE_CLOSED.value,
                    OrderState.CLOSED.value,
                }:
                    phase = "CLOSED"
                    if latest == OrderState.EXPIRED.value:
                        phase = "EXPIRED"
                    elif latest in {
                        OrderState.CANCELLED.value,
                        OrderState.MARKET_HALTED.value,
                    }:
                        phase = "CANCELLED"
                    self.pullback.mark_terminal(
                        plan.plan_id,
                        candidate,
                        tick,
                        phase,
                        f"ORDER_{latest}",
                    )
                    return
            decision = self.pullback.evaluate(
                plan.plan_id,
                candidate,
                tick,
                previous,
                entry_allowed=entry_allowed,
            )
            if not decision.signal:
                self._log_rejection(
                    tick,
                    plan.plan_id,
                    [f"PULLBACK_{decision.phase}", *decision.reasons],
                    details={"strategy_metrics": decision.metrics},
                )
                return
        elif not candidate.entry.price_only:
            states = self.cross_states.setdefault((plan.plan_id, tick.symbol.upper()), {})
            if not evaluate_group(
                candidate.entry.rules,
                context,
                previous,
                states=states,
                evaluated_at=source,
            ):
                self._log_rejection(tick, plan.plan_id, ["INDICATOR_RULES_NOT_MET"])
                return
        order, blocked = self.broker.submit_entry(plan.plan_id, candidate, tick)
        if candidate.strategy_type == "pullback_rebreak":
            self.pullback.mark_submission(
                plan.plan_id, candidate, tick, order is not None, blocked
            )
        if order is None:
            self._log_rejection(tick, plan.plan_id, [blocked or "RISK_BLOCKED"])
            return
        self.repository.set_plan_status(plan.plan_id, PlanStatus.RUNNING)

    def _handle_corporate_action(self, tick: MarketTick, plans: list[TradePlan]) -> None:
        if not tick.corporate_action:
            return
        self.broker.cancel_pending(tick.market, tick.symbol, "CORPORATE_ACTION")
        position = self.broker.portfolios[tick.market].positions.get(tick.symbol)
        if position and tick.bid <= tick.ask and tick.symbol_status == "trading":
            self.broker.close_quantity(position, position.remaining, tick, "CORPORATE_ACTION")
        for plan in plans:
            for candidate in plan.approved_symbols:
                if candidate.symbol.upper() != tick.symbol.upper():
                    continue
                if candidate.strategy_type == "pullback_rebreak":
                    self.pullback.mark_terminal(
                        plan.plan_id, candidate, tick, "CANCELLED", "CORPORATE_ACTION"
                    )
                self.repository.set_plan_status(plan.plan_id, PlanStatus.CANCELLED)
        self._log_rejection(tick, None, ["CORPORATE_ACTION"], "PLAN_INVALIDATED")

    def _sync_pullback_execution_states(
        self,
        plans: list[TradePlan],
        tick: MarketTick,
        had_pending: bool,
        had_position: bool,
    ) -> None:
        portfolio = self.broker.portfolios[tick.market]
        has_pending = tick.symbol in portfolio.pending_orders
        has_position = tick.symbol in portfolio.positions
        for plan in plans:
            for candidate in plan.approved_symbols:
                if (
                    candidate.symbol != tick.symbol
                    or candidate.strategy_type != "pullback_rebreak"
                ):
                    continue
                if had_position and not has_position:
                    self.pullback.mark_terminal(
                        plan.plan_id, candidate, tick, "CLOSED", "POSITION_CLOSED"
                    )
                elif has_position:
                    self.pullback.mark_terminal(
                        plan.plan_id, candidate, tick, "POSITION_OPEN", "POSITION_CONFIRMED"
                    )
                elif had_pending and not has_pending:
                    self.pullback.mark_terminal(
                        plan.plan_id, candidate, tick, "EXPIRED", "ENTRY_ORDER_TERMINATED"
                    )

    def process_tick(self, tick: MarketTick) -> dict:
        tick.symbol = tick.symbol.upper()
        feed_errors = self._validate_feed(tick)
        if feed_errors:
            self._log_rejection(tick, None, feed_errors, "DATA_REJECTED")
            return {"accepted": False, "reasons": feed_errors}
        feed = self._update_feed_state(tick)
        key = (tick.market, tick.symbol)
        self.latest_ticks[key] = tick
        local_date = (tick.source_timestamp or tick.timestamp).astimezone(
            MARKET_TZ[tick.market]
        ).date()
        self.repository.save_market_snapshot(tick, local_date)
        plans = self.repository.active_plans(tick.market, local_date)
        if tick.session != "regular":
            return {"accepted": True, "action": "SNAPSHOT_RECORDED", "session": tick.session}
        if feed.context_reset:
            for plan in plans:
                for candidate in plan.approved_symbols:
                    if (
                        candidate.symbol == tick.symbol
                        and candidate.strategy_type == "pullback_rebreak"
                    ):
                        self.pullback.reset(
                            plan.plan_id, candidate, tick, "FEED_CONTEXT_RESET"
                        )
        self._handle_corporate_action(tick, plans)
        if tick.corporate_action:
            return {"accepted": True, "action": "PLAN_INVALIDATED"}
        if feed.halted:
            self.broker.cancel_pending(
                tick.market, tick.symbol, "MARKET_HALTED", state=OrderState.MARKET_HALTED
            )
            self._log_rejection(tick, None, ["MARKET_HALTED"], "DATA_PAUSED")
            for plan in plans:
                for candidate in plan.approved_symbols:
                    if (
                        candidate.symbol == tick.symbol
                        and candidate.strategy_type == "pullback_rebreak"
                    ):
                        self.pullback.reset(plan.plan_id, candidate, tick, "MARKET_HALTED")
            return {"accepted": False, "reasons": ["MARKET_HALTED"]}

        source_local = (tick.source_timestamp or tick.timestamp).astimezone(
            MARKET_TZ[tick.market]
        )
        if is_session(tick.market, local_date) and source_local >= force_exit_at(
            tick.market, local_date
        ):
            portfolio_before = self.broker.portfolios[tick.market]
            had_pending = tick.symbol in portfolio_before.pending_orders
            had_position = tick.symbol in portfolio_before.positions
            self.broker.on_tick(tick)
            self._sync_pullback_execution_states(
                plans, tick, had_pending, had_position
            )
            self.force_close_market(tick.market)
            portfolio = self.broker.view(tick.market)
            if not portfolio["positions"] and not portfolio["pending_orders"]:
                for plan in plans:
                    self.repository.set_plan_status(plan.plan_id, PlanStatus.COMPLETED)
                    for candidate in plan.approved_symbols:
                        if candidate.strategy_type == "pullback_rebreak":
                            self.pullback.mark_terminal(
                                plan.plan_id,
                                candidate,
                                tick,
                                "CLOSED",
                                "SESSION_FORCE_CLOSE",
                            )
            return {"accepted": True, "action": "SESSION_FORCE_CLOSE"}

        portfolio_before = self.broker.portfolios[tick.market]
        had_pending = tick.symbol in portfolio_before.pending_orders
        had_position = tick.symbol in portfolio_before.positions
        self.broker.on_tick(tick)
        self._sync_pullback_execution_states(plans, tick, had_pending, had_position)
        for plan in plans:
            if datetime.now(UTC) > plan.expires_at.astimezone(UTC):
                self.repository.set_plan_status(plan.plan_id, PlanStatus.EXPIRED)
                for candidate in plan.approved_symbols:
                    self.broker.cancel_pending(tick.market, candidate.symbol, "PLAN_EXPIRED")
                    if candidate.strategy_type == "pullback_rebreak":
                        self.pullback.mark_terminal(
                            plan.plan_id, candidate, tick, "EXPIRED", "PLAN_EXPIRED"
                        )
                continue
            for candidate in plan.approved_symbols:
                if candidate.symbol.upper() == tick.symbol:
                    self._try_entry(plan, candidate, tick, feed)
        if tick.bid < tick.ask:
            self.previous_context[key] = self.context(tick)
        return {"accepted": True}

    def force_close_market(self, market: Market, reason: str = "SESSION_FORCE_CLOSE") -> None:
        ticks = {
            symbol: tick
            for (tick_market, symbol), tick in self.latest_ticks.items()
            if tick_market == market
        }
        self.broker.force_close(market, ticks, reason)

    def maintenance(self) -> None:
        self.broker.expire_pending(datetime.now(UTC))
