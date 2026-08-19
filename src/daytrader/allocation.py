from __future__ import annotations

import math
from dataclasses import dataclass

from .config import CostConfig
from .models import CandidatePlan


@dataclass(frozen=True, slots=True)
class AllocationResult:
    quantities: dict[str, int]
    target_weights: dict[str, float]
    estimated_invested: float
    estimated_cash: float


class WholeShareAllocator:
    """Deterministic whole-share allocator using approved maximum entry limits."""

    @staticmethod
    def _unit_cost(candidate: CandidatePlan, costs: CostConfig) -> float:
        entry_bps = costs.commission_bps_each_side + costs.fx_bps_each_side
        return candidate.entry.limit_price * (1 + entry_bps / 10_000)

    def allocate(
        self,
        initial_cash: float,
        candidates: list[CandidatePlan],
        target_weights: dict[str, float],
        costs: CostConfig,
        *,
        redistribute_residual: bool,
        max_quantity: int | None = None,
    ) -> AllocationResult:
        symbols = {candidate.symbol: candidate for candidate in candidates}
        if set(target_weights) != set(symbols):
            raise ValueError("target weights must match allocation candidates")
        if any(weight < 0 for weight in target_weights.values()):
            raise ValueError("target weights cannot be negative")
        if sum(target_weights.values()) > 1.0000001:
            raise ValueError("target weights cannot exceed 100%")
        unit_costs = {
            symbol: self._unit_cost(candidate, costs)
            for symbol, candidate in symbols.items()
        }
        quantity_limit = (
            max_quantity if max_quantity is not None else costs.max_position_quantity
        )
        if quantity_limit <= 0:
            raise ValueError("maximum allocation quantity must be positive")
        quantities = {
            symbol: min(
                quantity_limit,
                math.floor(initial_cash * target_weights[symbol] / unit_costs[symbol]),
            )
            for symbol in symbols
        }
        invested = sum(unit_costs[symbol] * quantity for symbol, quantity in quantities.items())
        remaining = max(0.0, initial_cash - invested)
        if redistribute_residual:
            targets = {
                symbol: initial_cash * target_weights[symbol] for symbol in symbols
            }
            while True:
                affordable = [
                    symbol
                    for symbol in symbols
                    if unit_costs[symbol] <= remaining + 1e-9
                    and quantities[symbol] < quantity_limit
                ]
                if not affordable:
                    break

                def deviation_after(symbol_to_add: str) -> tuple[float, float, str]:
                    deviation = 0.0
                    for symbol in symbols:
                        quantity = quantities[symbol] + (symbol == symbol_to_add)
                        deviation += abs(unit_costs[symbol] * quantity - targets[symbol])
                    return deviation, -unit_costs[symbol_to_add], symbol_to_add

                chosen = min(affordable, key=deviation_after)
                quantities[chosen] += 1
                remaining -= unit_costs[chosen]
        invested = sum(unit_costs[symbol] * quantity for symbol, quantity in quantities.items())
        return AllocationResult(
            quantities=quantities,
            target_weights=target_weights,
            estimated_invested=invested,
            estimated_cash=max(0.0, initial_cash - invested),
        )
