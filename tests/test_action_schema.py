from __future__ import annotations

import json
from pathlib import Path

import yaml

from daytrader.models import ALLOWED_INDICATORS, TradePlan


ROOT = Path(__file__).resolve().parents[1]


def _component_dependencies(value: object) -> set[str]:
    if isinstance(value, dict):
        dependencies = set()
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            dependencies.add(reference.rsplit("/", 1)[-1])
        for child in value.values():
            dependencies.update(_component_dependencies(child))
        return dependencies
    if isinstance(value, list):
        dependencies = set()
        for child in value:
            dependencies.update(_component_dependencies(child))
        return dependencies
    return set()


def test_action_schema_has_no_component_reference_cycles() -> None:
    schema = yaml.safe_load((ROOT / "gpt_action_openapi.yaml").read_text(encoding="utf-8"))
    components = schema["components"]["schemas"]
    graph = {name: _component_dependencies(value) for name, value in components.items()}

    def visit(name: str, path: tuple[str, ...]) -> None:
        assert name not in path, f"circular component reference: {' -> '.join((*path, name))}"
        for dependency in graph[name]:
            assert dependency in graph, f"unknown component reference: {dependency}"
            visit(dependency, (*path, name))

    for component_name in graph:
        visit(component_name, ())


def test_action_object_responses_declare_properties() -> None:
    schema = yaml.safe_load((ROOT / "gpt_action_openapi.yaml").read_text(encoding="utf-8"))
    status_schema = schema["components"]["schemas"]["PlanStatusResponse"]
    assert status_schema["type"] == "object"
    assert status_schema["properties"]
    experiment = schema["paths"]["/v1/gpt-actions/experiments/{experiment_id}"]["get"]
    response = experiment["responses"]["200"]["content"]["application/json"]["schema"]
    cohorts = response["properties"]["cohorts"]
    assert cohorts["properties"]
    assert set(cohorts["properties"]) == {
        "GPT_ALL_EQUAL",
        "USER_FIXED_SLEEVE",
        "USER_REALLOCATED",
    }


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
