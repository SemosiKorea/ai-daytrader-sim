from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import Predicate, RuleGroup


@dataclass
class CrossDebounceState:
    active: bool = False
    started_at: datetime | None = None
    confirm_count: int = 0
    last_fired_at: datetime | None = None


def _resolve(value: float | bool | str, context: dict[str, Any]) -> float | bool:
    if isinstance(value, str):
        if value not in context:
            raise KeyError(f"missing referenced indicator: {value}")
        return context[value]
    return value


def _cross_condition(predicate: Predicate, left: float, right: float) -> bool:
    margin = abs(right) * predicate.minimum_cross_pct / 100
    if predicate.operator == "cross_above":
        return left >= right + margin
    return left <= right - margin


def evaluate_predicate(
    predicate: Predicate,
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    state: CrossDebounceState | None = None,
    evaluated_at: datetime | None = None,
) -> bool:
    if predicate.indicator not in current:
        return False
    try:
        left = current[predicate.indicator]
        right = _resolve(predicate.value, current)
    except KeyError:
        return False
    if predicate.operator == "eq":
        return left == right
    if predicate.operator == "ne":
        return left != right
    if predicate.operator == "gt":
        return left > right
    if predicate.operator == "gte":
        return left >= right
    if predicate.operator == "lt":
        return left < right
    if predicate.operator == "lte":
        return left <= right
    if previous is None or predicate.indicator not in previous:
        return False
    try:
        previous_left = previous[predicate.indicator]
        previous_right = _resolve(predicate.value, previous)
        condition = _cross_condition(predicate, float(left), float(right))
    except (KeyError, TypeError, ValueError):
        return False
    crossed = (
        previous_left <= previous_right
        if predicate.operator == "cross_above"
        else previous_left >= previous_right
    ) and condition
    if state is None or evaluated_at is None:
        return crossed

    if crossed:
        state.active = True
        state.started_at = evaluated_at
        state.confirm_count = 1
    elif state.active and condition:
        state.confirm_count += 1
    else:
        state.active = False
        state.started_at = None
        state.confirm_count = 0
        return False

    held_ms = (
        (evaluated_at - state.started_at).total_seconds() * 1000 if state.started_at else 0
    )
    cooled_down = (
        state.last_fired_at is None
        or (evaluated_at - state.last_fired_at).total_seconds() >= predicate.cooldown_sec
    )
    confirmed = (
        state.confirm_count >= predicate.confirm_ticks
        and held_ms >= predicate.hold_above_ms
        and cooled_down
    )
    if confirmed:
        state.last_fired_at = evaluated_at
        state.active = False
        state.started_at = None
        state.confirm_count = 0
    return confirmed


def evaluate_group(
    group: RuleGroup,
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    states: dict[str, CrossDebounceState] | None = None,
    evaluated_at: datetime | None = None,
    path: str = "root",
) -> bool:
    results = []
    for index, predicate in enumerate(group.predicates):
        state = None
        if states is not None and predicate.operator.startswith("cross_"):
            state = states.setdefault(f"{path}.p{index}", CrossDebounceState())
        results.append(
            evaluate_predicate(
                predicate,
                current,
                previous,
                state=state,
                evaluated_at=evaluated_at,
            )
        )
    results.extend(
        evaluate_group(
            child,
            current,
            previous,
            states=states,
            evaluated_at=evaluated_at,
            path=f"{path}.g{index}",
        )
        for index, child in enumerate(group.groups)
    )
    if not results:
        return False
    return all(results) if group.mode == "all" else any(results)
