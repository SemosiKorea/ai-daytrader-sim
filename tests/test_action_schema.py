from __future__ import annotations

import json
from pathlib import Path

import yaml

from daytrader.models import ALLOWED_INDICATORS, TradePlan


ROOT = Path(__file__).resolve().parents[1]


def test_action_indicator_enum_matches_runtime() -> None:
    schema = yaml.safe_load((ROOT / "gpt_action_openapi.yaml").read_text(encoding="utf-8"))
    action_indicators = set(
        schema["components"]["schemas"]["Predicate"]["properties"]["indicator"]["enum"]
    )
    assert action_indicators == ALLOWED_INDICATORS
    operators = set(
        schema["components"]["schemas"]["Predicate"]["properties"]["operator"]["enum"]
    )
    assert {"eq", "ne"}.issubset(operators)


def test_sample_plan_matches_runtime_schema() -> None:
    payload = json.loads((ROOT / "samples" / "kr_plan.json").read_text(encoding="utf-8"))
    plan = TradePlan.model_validate(payload)
    assert plan.plan_version == 1
    pullback_payload = json.loads(
        (ROOT / "samples" / "us_pullback_plan.json").read_text(encoding="utf-8")
    )
    pullback = TradePlan.model_validate(pullback_payload)
    assert pullback.approved_symbols[0].strategy_type == "pullback_rebreak"
