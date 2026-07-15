from __future__ import annotations

import pytest
from pydantic import ValidationError

from daytrader.config import CostConfig
from daytrader.models import TradePlan
from daytrader.validator import PlanValidationError, validate_plan


def test_rejects_weak_reward_risk(kr_plan_dict: dict) -> None:
    kr_plan_dict["approved_symbols"][0]["take_profit"][0]["price"] = 101.4
    with pytest.raises(ValidationError, match="reward/risk"):
        TradePlan.model_validate(kr_plan_dict)


def test_requires_current_cost_configuration(kr_plan: TradePlan) -> None:
    universe = {"KR": {"005930": {"exchange": "KRX", "name": "Samsung"}}}
    costs = {"KR": CostConfig(3_000_000, 5, 5, None, 0)}
    with pytest.raises(PlanValidationError, match="taxes/fees"):
        validate_plan(kr_plan, universe, costs)


def test_valid_plan_passes(kr_plan: TradePlan) -> None:
    universe = {"KR": {"005930": {"exchange": "KRX", "name": "Samsung"}}}
    costs = {"KR": CostConfig(3_000_000, 5, 5, 10, 0)}
    validate_plan(kr_plan, universe, costs)


def test_cost_adjusted_worst_entry_reward_risk(kr_plan_dict: dict) -> None:
    kr_plan_dict["approved_symbols"][0]["take_profit"][0]["price"] = 102.0
    plan = TradePlan.model_validate(kr_plan_dict)
    universe = {"KR": {"005930": {"exchange": "KRX", "name": "Samsung"}}}
    expensive = {"KR": CostConfig(3_000_000, 50, 50, 0, 0)}
    with pytest.raises(PlanValidationError, match="cost-adjusted"):
        validate_plan(plan, universe, expensive)


def test_contradictory_all_group_is_rejected(kr_plan_dict: dict) -> None:
    entry = kr_plan_dict["approved_symbols"][0]["entry"]
    entry["price_only"] = False
    entry["rules"]["predicates"] = [
        {"indicator": "rsi_14_1m_regular", "operator": "gte", "value": 70},
        {"indicator": "rsi_14_1m_regular", "operator": "lte", "value": 50},
    ]
    plan = TradePlan.model_validate(kr_plan_dict)
    universe = {"KR": {"005930": {"exchange": "KRX", "name": "Samsung"}}}
    costs = {"KR": CostConfig(3_000_000, 0, 0, 0, 0)}
    with pytest.raises(PlanValidationError, match="contradictory"):
        validate_plan(plan, universe, costs)
