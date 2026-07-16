from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from .config import CostConfig
from .market_clock import MARKET_TZ, force_exit_at, is_session, regular_session_open, session_bounds
from .models import Market, RuleGroup, TradePlan


ENTRY_WINDOWS = {
    Market.KR: (time(9, 10), time(14, 30)),
    Market.US: (time(9, 40), time(14, 30)),
}


class PlanValidationError(ValueError):
    pass


def _rule_indicators(group: RuleGroup) -> set[str]:
    values = {predicate.indicator for predicate in group.predicates}
    values.update(
        predicate.value for predicate in group.predicates if isinstance(predicate.value, str)
    )
    for child in group.groups:
        values.update(_rule_indicators(child))
    return values


def _assert_no_contradictions(group: RuleGroup) -> None:
    def conjunctive_predicates(value: RuleGroup) -> list:
        predicates = list(value.predicates)
        for child in value.groups:
            if child.mode == "all":
                predicates.extend(conjunctive_predicates(child))
        return predicates

    if group.mode == "all":
        predicates = conjunctive_predicates(group)
        bounds: dict[str, dict[str, tuple[float, bool]]] = {}
        equals: dict[str, float | bool] = {}
        not_equals: dict[str, set[float | bool]] = {}
        for predicate in predicates:
            if predicate.operator == "eq" and not isinstance(predicate.value, str):
                if (
                    predicate.indicator in equals
                    and equals[predicate.indicator] != predicate.value
                ):
                    raise PlanValidationError(
                        f"contradictory equality conditions for {predicate.indicator}"
                    )
                equals[predicate.indicator] = predicate.value
                continue
            if predicate.operator == "ne" and not isinstance(predicate.value, str):
                not_equals.setdefault(predicate.indicator, set()).add(predicate.value)
                continue
            if not isinstance(predicate.value, (int, float)) or isinstance(predicate.value, bool):
                continue
            item = bounds.setdefault(predicate.indicator, {})
            value = float(predicate.value)
            if predicate.operator in {"gt", "gte"}:
                current = item.get("lower")
                strict = predicate.operator == "gt"
                if current is None or value > current[0] or (
                    value == current[0] and strict and not current[1]
                ):
                    item["lower"] = (value, strict)
            elif predicate.operator in {"lt", "lte"}:
                current = item.get("upper")
                strict = predicate.operator == "lt"
                if current is None or value < current[0] or (
                    value == current[0] and strict and not current[1]
                ):
                    item["upper"] = (value, strict)
        for indicator, item in bounds.items():
            lower = item.get("lower")
            upper = item.get("upper")
            if lower and upper and (
                lower[0] > upper[0]
                or (lower[0] == upper[0] and (lower[1] or upper[1]))
            ):
                raise PlanValidationError(f"contradictory conditions for {indicator}")
            equal = equals.get(indicator)
            if isinstance(equal, (int, float)) and not isinstance(equal, bool):
                if lower and (equal < lower[0] or (equal == lower[0] and lower[1])):
                    raise PlanValidationError(f"equality conflicts with lower bound for {indicator}")
                if upper and (equal > upper[0] or (equal == upper[0] and upper[1])):
                    raise PlanValidationError(f"equality conflicts with upper bound for {indicator}")
        for indicator, equal in equals.items():
            if equal in not_equals.get(indicator, set()):
                raise PlanValidationError(f"eq/ne conditions conflict for {indicator}")
    for child in group.groups:
        _assert_no_contradictions(child)


def _net_sale_price(price: float, cost: CostConfig) -> float:
    bps = (
        cost.commission_bps_each_side
        + cost.slippage_bps_each_side
        + cost.fx_bps_each_side
        + (cost.sell_tax_bps or 0.0)
    )
    return price * (1 - bps / 10_000)


def _validate_cost_adjusted_reward_risk(plan: TradePlan, cost: CostConfig) -> None:
    for candidate in plan.approved_symbols:
        worst_entry = candidate.entry.limit_price * (
            1 + (cost.commission_bps_each_side + cost.fx_bps_each_side) / 10_000
        )
        stop_marketable_limit = candidate.stop_loss.price * (
            1 - candidate.stop_loss.limit_offset_pct / 100
        )
        expected_stop = _net_sale_price(stop_marketable_limit, cost)
        expected_target = _net_sale_price(candidate.take_profit[0].price, cost)
        risk = worst_entry - expected_stop
        reward = expected_target - worst_entry
        if risk <= 0 or reward / risk < 1.5:
            raise PlanValidationError(
                f"cost-adjusted worst-entry reward/risk is below 1.5 for {candidate.symbol}"
            )


def validate_plan(
    plan: TradePlan,
    universe: dict[str, dict[str, dict[str, str]]],
    costs: dict[str, CostConfig],
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    if plan.expires_at.tzinfo is None:
        raise PlanValidationError("expires_at must include a timezone")
    if plan.expires_at.astimezone(UTC) <= now:
        raise PlanValidationError("plan has already expired")
    if plan.created_at.astimezone(UTC) > now + timedelta(minutes=1):
        raise PlanValidationError("created_at cannot be in the future")
    if not is_session(plan.market, plan.trade_date):
        raise PlanValidationError("trade_date is not an official exchange session")
    cost = costs[plan.market.value]
    if not cost.session_ready:
        raise PlanValidationError("current official sell taxes/fees must be configured")
    local_expiry = plan.expires_at.astimezone(MARKET_TZ[plan.market])
    if local_expiry.date() != plan.trade_date:
        raise PlanValidationError("expiry date does not match market trade date")
    expected_force_exit = force_exit_at(plan.market, plan.trade_date)
    _, session_close = session_bounds(plan.market, plan.trade_date)
    if not expected_force_exit <= local_expiry <= session_close.astimezone(
        MARKET_TZ[plan.market]
    ):
        raise PlanValidationError("expiry must be between force-exit and official session close")

    allowed = universe.get(plan.market.value, {})
    earliest, normal_latest = ENTRY_WINDOWS[plan.market]
    latest = min(normal_latest, expected_force_exit.time().replace(tzinfo=None))
    session_open = regular_session_open(plan.market, plan.trade_date)
    for candidate in plan.approved_symbols:
        symbol = candidate.symbol.upper()
        if symbol not in allowed:
            raise PlanValidationError(f"symbol is not in the allowlist: {symbol}")
        if candidate.exchange.upper() != allowed[symbol]["exchange"].upper():
            raise PlanValidationError(f"exchange mismatch for {symbol}")
        if candidate.entry.start_time < earliest or candidate.entry.end_time > latest:
            raise PlanValidationError(f"entry window is outside allowed hours for {symbol}")
        if candidate.entry.end_time >= expected_force_exit.time().replace(tzinfo=None):
            raise PlanValidationError(f"entry must end before force exit for {symbol}")
        if candidate.force_exit_time != expected_force_exit.time().replace(tzinfo=None):
            raise PlanValidationError(
                f"force exit must be {expected_force_exit.time()} for {symbol}"
            )
        if candidate.strategy_type == "pullback_rebreak":
            ready_at = (session_open + timedelta(minutes=5)).time().replace(tzinfo=None)
            if candidate.entry.start_time < ready_at:
                raise PlanValidationError(
                    f"pullback strategy cannot start before the 5-minute range for {symbol}"
                )
        elif not candidate.entry.price_only:
            indicators = _rule_indicators(candidate.entry.rules)
            opening_minutes = [
                minutes
                for minutes in (5, 10, 15)
                if any(value.startswith(f"opening_range_{minutes}_") for value in indicators)
            ]
            if opening_minutes:
                ready_at = (session_open + timedelta(minutes=max(opening_minutes))).time()
                if candidate.entry.start_time < ready_at.replace(tzinfo=None):
                    raise PlanValidationError(
                        f"opening range is not complete at entry start for {symbol}"
                    )
            _assert_no_contradictions(candidate.entry.rules)
    _validate_cost_adjusted_reward_risk(plan, cost)
