from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .allocation import AllocationResult, WholeShareAllocator
from .broker import PaperBroker
from .config import CostConfig
from .engine import TradingEngine
from .market_clock import MARKET_TZ
from .models import (
    ExperimentCohort,
    MarketTick,
    PlanStatus,
    PortfolioExperimentRequest,
    TradePlan,
)
from .repository import Repository


TERMINAL_PLAN_STATUSES = {
    PlanStatus.COMPLETED.value,
    PlanStatus.EXPIRED.value,
    PlanStatus.CANCELLED.value,
    PlanStatus.RISK_BLOCKED.value,
}


@dataclass(slots=True)
class CohortRuntime:
    cohort: ExperimentCohort
    repository: Repository
    broker: PaperBroker
    engine: TradingEngine
    plan: TradePlan
    allocation: AllocationResult


@dataclass(slots=True)
class ExperimentRuntime:
    request: PortfolioExperimentRequest
    cohorts: dict[ExperimentCohort, CohortRuntime]


class PortfolioExperimentManager:
    """Fan out identical ticks to three isolated paper-only comparison cohorts."""

    def __init__(
        self,
        repository: Repository,
        costs: dict[str, CostConfig],
        data_path: Path,
    ):
        self.repository = repository
        self.costs = costs
        self.data_path = data_path
        self.allocator = WholeShareAllocator()
        self.runtimes: dict[str, ExperimentRuntime] = {}
        self._restore_active()

    def _restore_active(self) -> None:
        now = datetime.now(UTC)
        for request in self.repository.active_portfolio_experiments():
            if request.expires_at.astimezone(UTC) <= now:
                self.repository.set_portfolio_experiment_status(
                    request.experiment_id, "EXPIRED"
                )
                continue
            self.register(request)

    @staticmethod
    def _cohort_candidates(
        request: PortfolioExperimentRequest, cohort: ExperimentCohort
    ) -> list:
        if cohort == ExperimentCohort.GPT_ALL_EQUAL:
            return list(request.candidates)
        selected = set(request.user_selected_symbols)
        return [candidate for candidate in request.candidates if candidate.symbol in selected]

    def _allocation(
        self,
        request: PortfolioExperimentRequest,
        cohort: ExperimentCohort,
        candidates: list,
    ) -> AllocationResult:
        initial_cash = self.costs[request.market.value].initial_cash
        if cohort == ExperimentCohort.USER_FIXED_SLEEVE:
            weight = 1 / len(request.candidates)
            redistribute = False
        else:
            weight = 1 / len(candidates)
            redistribute = True
        weights = {candidate.symbol: weight for candidate in candidates}
        whole_share_limit = max(
            1,
            math.ceil(
                initial_cash
                / min(candidate.entry.limit_price for candidate in candidates)
            ),
        )
        return self.allocator.allocate(
            initial_cash,
            candidates,
            weights,
            self.costs[request.market.value],
            redistribute_residual=redistribute,
            max_quantity=whole_share_limit,
        )

    def _plan(
        self,
        request: PortfolioExperimentRequest,
        cohort: ExperimentCohort,
        candidates: list,
    ) -> TradePlan:
        return TradePlan(
            plan_id=f"{request.experiment_id}_{cohort.value}",
            created_at=request.created_at,
            market=request.market,
            trade_date=request.trade_date,
            expires_at=request.expires_at,
            approval_nonce="000000",
            approved_symbols=candidates,
        )

    def register(
        self, request: PortfolioExperimentRequest, *, active: bool = True
    ) -> ExperimentRuntime:
        existing = self.runtimes.get(request.experiment_id) if active else None
        if existing:
            return existing
        cohorts: dict[ExperimentCohort, CohortRuntime] = {}
        for cohort in ExperimentCohort:
            candidates = self._cohort_candidates(request, cohort)
            allocation = self._allocation(request, cohort, candidates)
            cohort_path = self.data_path / request.experiment_id / f"{cohort.value}.db"
            cohort_repository = Repository(cohort_path)
            plan = self._plan(request, cohort, candidates)
            if cohort_repository.get_plan(plan.plan_id) is None:
                cohort_repository.store_plan(plan, PlanStatus.ARMED)
            base_cost = self.costs[request.market.value]
            experiment_cost = replace(
                base_cost,
                max_order_submissions_per_day=max(
                    base_cost.max_order_submissions_per_day, len(candidates) * 2
                ),
                max_filled_entries_per_day=max(
                    base_cost.max_filled_entries_per_day, len(candidates)
                ),
                max_position_quantity=max(
                    base_cost.max_position_quantity,
                    max(allocation.quantities.values(), default=1),
                ),
            )
            broker = PaperBroker(
                cohort_repository,
                {request.market.value: experiment_cost},
                max_concurrent_positions=len(candidates),
                fixed_quantities={
                    (request.market, symbol): quantity
                    for symbol, quantity in allocation.quantities.items()
                },
                enforce_portfolio_gates=False,
            )
            cohorts[cohort] = CohortRuntime(
                cohort=cohort,
                repository=cohort_repository,
                broker=broker,
                engine=TradingEngine(cohort_repository, broker),
                plan=plan,
                allocation=allocation,
            )
        runtime = ExperimentRuntime(request=request, cohorts=cohorts)
        if active:
            self.runtimes[request.experiment_id] = runtime
        return runtime

    def process_tick(self, tick: MarketTick) -> None:
        local_date = tick.source_timestamp.astimezone(MARKET_TZ[tick.market]).date()
        for experiment_id, runtime in list(self.runtimes.items()):
            request = runtime.request
            if request.market != tick.market or request.trade_date != local_date:
                continue
            for cohort in runtime.cohorts.values():
                cohort.engine.process_tick(tick.model_copy(deep=True))
            statuses = [
                cohort.repository.get_plan(cohort.plan.plan_id)["status"]
                for cohort in runtime.cohorts.values()
            ]
            if all(status in TERMINAL_PLAN_STATUSES for status in statuses):
                self.repository.set_portfolio_experiment_status(experiment_id, "COMPLETED")
                self.runtimes.pop(experiment_id, None)

    def maintenance(self) -> None:
        for runtime in self.runtimes.values():
            for cohort in runtime.cohorts.values():
                cohort.engine.maintenance()

    @staticmethod
    def _symbol_results(repository: Repository) -> dict[str, dict[str, float | int]]:
        results: dict[str, dict[str, float | int]] = {}
        for event in repository.recent_events(10_000):
            if event["event_type"] != "PAPER_POSITION_CLOSED" or not event["symbol"]:
                continue
            payload = json.loads(event["payload"])
            item = results.setdefault(event["symbol"], {"trades": 0, "net_pnl": 0.0})
            item["trades"] = int(item["trades"]) + 1
            item["net_pnl"] = float(item["net_pnl"]) + float(payload.get("pnl", 0.0))
        return results

    def view(self, experiment_id: str) -> dict[str, Any] | None:
        runtime = self.runtimes.get(experiment_id)
        row = self.repository.get_portfolio_experiment(experiment_id)
        if row is None:
            return None
        if runtime is None:
            request = PortfolioExperimentRequest.model_validate_json(row["payload"])
            runtime = self.register(request, active=row["status"] == "ACTIVE")
        cohorts = {}
        for cohort_id, cohort in runtime.cohorts.items():
            marks = {
                symbol: tick.last
                for (market, symbol), tick in cohort.engine.latest_ticks.items()
                if market == runtime.request.market
            }
            plan_row = cohort.repository.get_plan(cohort.plan.plan_id)
            cohorts[cohort_id.value] = {
                "plan_status": plan_row["status"],
                "allocation": {
                    "quantities": cohort.allocation.quantities,
                    "target_weights": cohort.allocation.target_weights,
                    "estimated_invested": cohort.allocation.estimated_invested,
                    "estimated_cash": cohort.allocation.estimated_cash,
                },
                "portfolio": cohort.broker.view(runtime.request.market, marks),
                "performance": cohort.broker.performance(runtime.request.market),
                "symbol_results": self._symbol_results(cohort.repository),
            }
        all_cohort = cohorts[ExperimentCohort.GPT_ALL_EQUAL.value]
        fixed_cohort = cohorts[ExperimentCohort.USER_FIXED_SLEEVE.value]
        reallocated_cohort = cohorts[ExperimentCohort.USER_REALLOCATED.value]
        all_results = all_cohort["symbol_results"]
        selected = set(runtime.request.user_selected_symbols)

        def result_totals(symbols: set[str]) -> tuple[int, float]:
            trades = sum(int(all_results.get(symbol, {}).get("trades", 0)) for symbol in symbols)
            pnl = sum(float(all_results.get(symbol, {}).get("net_pnl", 0.0)) for symbol in symbols)
            return trades, pnl

        candidate_symbols = {item.symbol for item in runtime.request.candidates}
        selected_trades, selected_pnl = result_totals(selected)
        excluded_trades, excluded_pnl = result_totals(candidate_symbols - selected)
        selected_average = selected_pnl / selected_trades if selected_trades else None
        excluded_average = excluded_pnl / excluded_trades if excluded_trades else None
        comparison = {
            "user_fixed_minus_gpt_all_net_pnl": (
                fixed_cohort["performance"]["net_pnl"]
                - all_cohort["performance"]["net_pnl"]
            ),
            "user_reallocated_minus_gpt_all_net_pnl": (
                reallocated_cohort["performance"]["net_pnl"]
                - all_cohort["performance"]["net_pnl"]
            ),
            "concentration_effect_net_pnl": (
                reallocated_cohort["performance"]["net_pnl"]
                - fixed_cohort["performance"]["net_pnl"]
            ),
            "selected_average_trade_pnl_in_gpt_all": selected_average,
            "excluded_average_trade_pnl_in_gpt_all": excluded_average,
            "selection_uplift_per_trade": (
                selected_average - excluded_average
                if selected_average is not None and excluded_average is not None
                else None
            ),
        }
        return {
            "experiment_id": experiment_id,
            "status": row["status"],
            "market": runtime.request.market.value,
            "trade_date": runtime.request.trade_date.isoformat(),
            "candidate_symbols": [item.symbol for item in runtime.request.candidates],
            "user_selected_symbols": runtime.request.user_selected_symbols,
            "cohorts": cohorts,
            "comparison": comparison,
        }
