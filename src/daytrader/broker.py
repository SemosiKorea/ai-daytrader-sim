from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import CostConfig
from .models import CandidatePlan, Market, MarketTick, OrderState
from .repository import Repository


@dataclass
class PendingOrder:
    order_id: str
    idempotency_key: str
    market: Market
    symbol: str
    plan_id: str
    state: OrderState
    created_at: datetime
    eligible_fill_at: datetime
    expires_at: datetime
    limit_price: float
    desired_quantity: int
    filled_quantity: int
    average_fill_price: float
    risk_per_share: float
    stop_price: float
    stop_limit_offset_pct: float
    emergency_exit_after_sec: float
    target_specs: list[tuple[float, int]]
    exit_policy: dict

    @property
    def remaining(self) -> int:
        return self.desired_quantity - self.filled_quantity


@dataclass
class Position:
    market: Market
    symbol: str
    plan_id: str
    quantity: int
    remaining: int
    entry_price: float
    stop_price: float
    stop_limit_offset_pct: float
    emergency_exit_after_sec: float
    targets: list[tuple[float, int]]
    target_specs: list[tuple[float, int]]
    opened_at: datetime
    exit_policy: dict
    highest_price: float
    realized_pnl: float = 0.0
    fees: float = 0.0
    filled_targets: set[int] = field(default_factory=set)
    exit_pending_at: datetime | None = None
    exit_reason: str | None = None
    below_vwap_since: datetime | None = None


@dataclass
class Portfolio:
    market: Market
    initial_cash: float
    cash: float
    realized_pnl: float = 0.0
    session_realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: dict[str, PendingOrder] = field(default_factory=dict)
    trades_today: int = 0
    order_submissions_today: int = 0
    consecutive_stops: int = 0
    cooldown_until: datetime | None = None
    session_date: str | None = None


class PaperBroker:
    """Stateful paper broker with conservative fills and no live-order capability."""

    def __init__(self, repository: Repository, costs: dict[str, CostConfig]):
        self.repository = repository
        self.costs = costs
        self.portfolios = {
            Market(market): Portfolio(Market(market), cost.initial_cash, cost.initial_cash)
            for market, cost in costs.items()
        }
        for market in self.portfolios:
            self._restore(market)

    @staticmethod
    def _bps(value: float, bps: float) -> float:
        return value * bps / 10_000

    def _entry_unit_cost_at_limit(self, market: Market, limit_price: float) -> float:
        cost = self.costs[market.value]
        return limit_price + self._bps(
            limit_price, cost.commission_bps_each_side + cost.fx_bps_each_side
        )

    def _entry_fill(self, market: Market, ask: float, limit_price: float) -> tuple[float, float]:
        cost = self.costs[market.value]
        execution_price = ask + self._bps(ask, cost.slippage_bps_each_side)
        if execution_price > limit_price:
            return 0.0, 0.0
        fee = self._bps(
            execution_price, cost.commission_bps_each_side + cost.fx_bps_each_side
        )
        return execution_price, fee

    def _exit_net(self, market: Market, gross: float) -> tuple[float, float]:
        cost = self.costs[market.value]
        fees = self._bps(
            gross,
            cost.commission_bps_each_side
            + cost.slippage_bps_each_side
            + cost.fx_bps_each_side
            + (cost.sell_tax_bps or 0.0),
        )
        return gross - fees, fees

    def expected_stop_net_per_share(
        self, market: Market, stop_price: float, offset_pct: float
    ) -> float:
        marketable_limit = stop_price * (1 - offset_pct / 100)
        net, _ = self._exit_net(market, marketable_limit)
        return net

    def worst_case_risk_per_share(self, market: Market, candidate: CandidatePlan) -> float:
        entry = self._entry_unit_cost_at_limit(market, candidate.entry.limit_price)
        stop = self.expected_stop_net_per_share(
            market, candidate.stop_loss.price, candidate.stop_loss.limit_offset_pct
        )
        return max(0.0, entry - stop)

    def _serialize_position(self, position: Position) -> dict:
        return {
            "market": position.market.value,
            "symbol": position.symbol,
            "plan_id": position.plan_id,
            "quantity": position.quantity,
            "remaining": position.remaining,
            "entry_price": position.entry_price,
            "stop_price": position.stop_price,
            "stop_limit_offset_pct": position.stop_limit_offset_pct,
            "emergency_exit_after_sec": position.emergency_exit_after_sec,
            "targets": position.targets,
            "target_specs": position.target_specs,
            "opened_at": position.opened_at.isoformat(),
            "exit_policy": position.exit_policy,
            "highest_price": position.highest_price,
            "realized_pnl": position.realized_pnl,
            "fees": position.fees,
            "filled_targets": sorted(position.filled_targets),
            "exit_pending_at": (
                position.exit_pending_at.isoformat() if position.exit_pending_at else None
            ),
            "exit_reason": position.exit_reason,
            "below_vwap_since": (
                position.below_vwap_since.isoformat() if position.below_vwap_since else None
            ),
        }

    @staticmethod
    def _serialize_order(order: PendingOrder) -> dict:
        return {
            **order.__dict__,
            "market": order.market.value,
            "state": order.state.value,
            "created_at": order.created_at.isoformat(),
            "eligible_fill_at": order.eligible_fill_at.isoformat(),
            "expires_at": order.expires_at.isoformat(),
        }

    def _state(self, market: Market) -> dict:
        portfolio = self.portfolios[market]
        return {
            "initial_cash": portfolio.initial_cash,
            "cash": portfolio.cash,
            "realized_pnl": portfolio.realized_pnl,
            "session_realized_pnl": portfolio.session_realized_pnl,
            "trades_today": portfolio.trades_today,
            "order_submissions_today": portfolio.order_submissions_today,
            "consecutive_stops": portfolio.consecutive_stops,
            "cooldown_until": (
                portfolio.cooldown_until.isoformat() if portfolio.cooldown_until else None
            ),
            "session_date": portfolio.session_date,
            "positions": [
                self._serialize_position(position) for position in portfolio.positions.values()
            ],
            "pending_orders": [
                self._serialize_order(order) for order in portfolio.pending_orders.values()
            ],
        }

    def _save(self, market: Market) -> None:
        self.repository.save_portfolio(market, self._state(market))

    def _restore(self, market: Market) -> None:
        state = self.repository.load_portfolio(market)
        if not state:
            return
        portfolio = self.portfolios[market]
        portfolio.cash = float(state["cash"])
        portfolio.realized_pnl = float(state.get("realized_pnl", 0.0))
        portfolio.session_realized_pnl = float(state.get("session_realized_pnl", 0.0))
        portfolio.trades_today = int(state.get("trades_today", 0))
        portfolio.order_submissions_today = int(state.get("order_submissions_today", 0))
        portfolio.consecutive_stops = int(state.get("consecutive_stops", 0))
        portfolio.cooldown_until = (
            datetime.fromisoformat(state["cooldown_until"])
            if state.get("cooldown_until")
            else None
        )
        portfolio.session_date = state.get("session_date")
        for raw in state.get("positions", []):
            position = Position(
                market=market,
                symbol=raw["symbol"],
                plan_id=raw["plan_id"],
                quantity=int(raw["quantity"]),
                remaining=int(raw["remaining"]),
                entry_price=float(raw["entry_price"]),
                stop_price=float(raw["stop_price"]),
                stop_limit_offset_pct=float(raw.get("stop_limit_offset_pct", 0.2)),
                emergency_exit_after_sec=float(raw.get("emergency_exit_after_sec", 2.0)),
                targets=[(float(price), int(qty)) for price, qty in raw["targets"]],
                target_specs=[
                    (float(price), int(pct)) for price, pct in raw.get("target_specs", [])
                ],
                opened_at=datetime.fromisoformat(raw["opened_at"]),
                exit_policy=raw.get("exit_policy", {}),
                highest_price=float(raw.get("highest_price", raw["entry_price"])),
                realized_pnl=float(raw.get("realized_pnl", 0.0)),
                fees=float(raw.get("fees", 0.0)),
                filled_targets=set(raw.get("filled_targets", [])),
                exit_pending_at=(
                    datetime.fromisoformat(raw["exit_pending_at"])
                    if raw.get("exit_pending_at")
                    else None
                ),
                exit_reason=raw.get("exit_reason"),
                below_vwap_since=(
                    datetime.fromisoformat(raw["below_vwap_since"])
                    if raw.get("below_vwap_since")
                    else None
                ),
            )
            portfolio.positions[position.symbol] = position
        for raw in state.get("pending_orders", []):
            order = PendingOrder(
                order_id=raw["order_id"],
                idempotency_key=raw["idempotency_key"],
                market=market,
                symbol=raw["symbol"],
                plan_id=raw["plan_id"],
                state=OrderState(raw["state"]),
                created_at=datetime.fromisoformat(raw["created_at"]),
                eligible_fill_at=datetime.fromisoformat(raw["eligible_fill_at"]),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
                limit_price=float(raw["limit_price"]),
                desired_quantity=int(raw["desired_quantity"]),
                filled_quantity=int(raw["filled_quantity"]),
                average_fill_price=float(raw["average_fill_price"]),
                risk_per_share=float(raw["risk_per_share"]),
                stop_price=float(raw["stop_price"]),
                stop_limit_offset_pct=float(raw["stop_limit_offset_pct"]),
                emergency_exit_after_sec=float(raw["emergency_exit_after_sec"]),
                target_specs=[(float(p), int(q)) for p, q in raw["target_specs"]],
                exit_policy=raw["exit_policy"],
            )
            portfolio.pending_orders[order.symbol] = order

    def _ensure_session(self, market: Market, timestamp: datetime) -> None:
        timezone = ZoneInfo("Asia/Seoul" if market == Market.KR else "America/New_York")
        session_date = timestamp.astimezone(timezone).date().isoformat()
        portfolio = self.portfolios[market]
        if portfolio.session_date != session_date:
            portfolio.session_date = session_date
            portfolio.session_realized_pnl = 0.0
            portfolio.trades_today = 0
            portfolio.order_submissions_today = 0
            portfolio.consecutive_stops = 0
            portfolio.cooldown_until = None
            for symbol in list(portfolio.pending_orders):
                self.cancel_pending(market, symbol, "SESSION_CHANGED", OrderState.EXPIRED)
            self._save(market)
            self.repository.add_event(
                "PAPER_SESSION_STARTED", market, None, None, {"session_date": session_date}
            )

    def pending_risk(self, market: Market) -> float:
        return sum(
            order.remaining * order.risk_per_share
            for order in self.portfolios[market].pending_orders.values()
        )

    def position_risk(self, market: Market) -> float:
        total = 0.0
        for position in self.portfolios[market].positions.values():
            stop_net = self.expected_stop_net_per_share(
                market, position.stop_price, position.stop_limit_offset_pct
            )
            total += max(0.0, position.entry_price - stop_net) * position.remaining
        return total

    def daily_risk_used(self, market: Market) -> float:
        portfolio = self.portfolios[market]
        return (
            max(0.0, -portfolio.session_realized_pnl)
            + self.position_risk(market)
            + self.pending_risk(market)
        )

    def can_submit(self, market: Market, timestamp: datetime) -> tuple[bool, str | None]:
        self._ensure_session(market, timestamp)
        portfolio = self.portfolios[market]
        cost = self.costs[market.value]
        if portfolio.positions or portfolio.pending_orders:
            return False, "POSITION_SLOT_OCCUPIED"
        if portfolio.trades_today >= cost.max_filled_entries_per_day:
            return False, "DAILY_ENTRY_LIMIT"
        if portfolio.order_submissions_today >= cost.max_order_submissions_per_day:
            return False, "DAILY_ORDER_LIMIT"
        if portfolio.consecutive_stops >= cost.consecutive_stop_limit:
            return False, "CONSECUTIVE_STOP_LIMIT"
        if portfolio.cooldown_until and timestamp < portfolio.cooldown_until:
            return False, "STOP_COOLDOWN"
        limit = portfolio.initial_cash * cost.daily_loss_limit_pct / 100
        if self.daily_risk_used(market) >= limit:
            return False, "DAILY_RISK_LIMIT"
        return True, None

    def submit_entry(
        self, plan_id: str, candidate: CandidatePlan, tick: MarketTick
    ) -> tuple[PendingOrder | None, str | None]:
        market = tick.market
        timestamp = tick.received_timestamp or tick.timestamp
        if candidate.symbol.upper() != tick.symbol.upper():
            return None, "SYMBOL_MISMATCH"
        allowed, reason = self.can_submit(market, timestamp)
        if not allowed:
            return None, reason
        key = f"{plan_id}:{candidate.symbol.upper()}:ENTRY"
        portfolio = self.portfolios[market]
        policy = self.costs[market.value]
        risk_per_share = self.worst_case_risk_per_share(market, candidate)
        if risk_per_share <= 0:
            return None, "RISK_REWARD_INVALID"
        per_trade_budget = portfolio.initial_cash * policy.trade_risk_pct / 100
        daily_budget = portfolio.initial_cash * policy.daily_loss_limit_pct / 100
        available_daily_risk = max(0.0, daily_budget - self.daily_risk_used(market))
        risk_quantity = math.floor(min(per_trade_budget, available_daily_risk) / risk_per_share)
        cash_quantity = math.floor(
            portfolio.cash / self._entry_unit_cost_at_limit(market, candidate.entry.limit_price)
        )
        quantity = min(risk_quantity, cash_quantity, policy.max_position_quantity)
        if quantity <= 0:
            return None, "RISK_BLOCKED"
        if not self.repository.claim_idempotency_key(key, market, candidate.symbol, plan_id):
            return None, "ORDER_ALREADY_SUBMITTED"
        order = PendingOrder(
            order_id=str(uuid.uuid4()),
            idempotency_key=key,
            market=market,
            symbol=candidate.symbol,
            plan_id=plan_id,
            state=OrderState.ENTRY_PENDING,
            created_at=timestamp,
            eligible_fill_at=timestamp + timedelta(milliseconds=policy.fill_latency_ms),
            expires_at=timestamp + timedelta(seconds=policy.fill_timeout_sec),
            limit_price=candidate.entry.limit_price,
            desired_quantity=quantity,
            filled_quantity=0,
            average_fill_price=0.0,
            risk_per_share=risk_per_share,
            stop_price=candidate.stop_loss.price,
            stop_limit_offset_pct=candidate.stop_loss.limit_offset_pct,
            emergency_exit_after_sec=candidate.stop_loss.emergency_exit_after_sec,
            target_specs=[(target.price, target.quantity_pct) for target in candidate.take_profit],
            exit_policy=candidate.exit_policy.model_dump(mode="json"),
        )
        portfolio.pending_orders[candidate.symbol] = order
        portfolio.order_submissions_today += 1
        self._save(market)
        self._order_event(order, OrderState.SIGNAL_TRIGGERED, {"signal_ask": tick.ask})
        self._order_event(
            order,
            OrderState.ENTRY_PENDING,
            {
                "desired_quantity": quantity,
                "limit_price": order.limit_price,
                "eligible_fill_at": order.eligible_fill_at,
                "expires_at": order.expires_at,
                "risk_per_share": risk_per_share,
            },
        )
        return order, None

    def _order_event(
        self, order: PendingOrder, state: OrderState, payload: dict | None = None
    ) -> None:
        self.repository.add_event(
            "ORDER_STATE_CHANGED",
            order.market,
            order.symbol,
            order.plan_id,
            {"order_id": order.order_id, "state": state.value, **(payload or {})},
        )

    @staticmethod
    def _allocate_targets(quantity: int, specs: list[tuple[float, int]]) -> list[tuple[float, int]]:
        allocations = []
        allocated = 0
        for index, (price, pct) in enumerate(specs):
            if index == len(specs) - 1:
                qty = quantity - allocated
            else:
                qty = min(quantity - allocated, math.ceil(quantity * pct / 100))
            allocations.append((price, max(0, qty)))
            allocated += qty
        return allocations

    def _apply_entry_fill(
        self, order: PendingOrder, tick: MarketTick, quantity: int, execution: float, fee: float
    ) -> None:
        portfolio = self.portfolios[order.market]
        all_in = execution + fee
        affordable = math.floor(portfolio.cash / all_in)
        quantity = min(quantity, affordable, order.remaining)
        if quantity <= 0:
            return
        first_fill = order.filled_quantity == 0
        previous_notional = order.average_fill_price * order.filled_quantity
        portfolio.cash -= all_in * quantity
        order.filled_quantity += quantity
        order.average_fill_price = (
            previous_notional + all_in * quantity
        ) / order.filled_quantity
        position = portfolio.positions.get(order.symbol)
        if position is None:
            position = Position(
                market=order.market,
                symbol=order.symbol,
                plan_id=order.plan_id,
                quantity=0,
                remaining=0,
                entry_price=0.0,
                stop_price=order.stop_price,
                stop_limit_offset_pct=order.stop_limit_offset_pct,
                emergency_exit_after_sec=order.emergency_exit_after_sec,
                targets=[],
                target_specs=order.target_specs,
                opened_at=tick.source_timestamp or tick.timestamp,
                exit_policy=order.exit_policy,
                highest_price=tick.bid,
            )
            portfolio.positions[order.symbol] = position
        combined_cost = position.entry_price * position.quantity + all_in * quantity
        position.quantity += quantity
        position.remaining += quantity
        position.entry_price = combined_cost / position.quantity
        position.fees += fee * quantity
        position.targets = self._allocate_targets(position.quantity, position.target_specs)
        if first_fill:
            portfolio.trades_today += 1
        order.state = (
            OrderState.FILLED if order.remaining == 0 else OrderState.PARTIALLY_FILLED
        )
        self.repository.add_event(
            "PAPER_BUY_FILLED",
            order.market,
            order.symbol,
            order.plan_id,
            {
                "order_id": order.order_id,
                "quantity": quantity,
                "price": execution,
                "fees": fee * quantity,
                "cumulative_quantity": order.filled_quantity,
                "fill_latency_ms": int(
                    (
                        (tick.received_timestamp or tick.timestamp) - order.created_at
                    ).total_seconds()
                    * 1000
                ),
                "visible_ask_size": tick.ask_size,
            },
        )
        self._order_event(order, order.state, {"filled_quantity": order.filled_quantity})
        if first_fill:
            self._order_event(
                order,
                OrderState.POSITION_OPEN,
                {"position_quantity": position.quantity},
            )
        if order.state == OrderState.FILLED:
            portfolio.pending_orders.pop(order.symbol, None)
        self._save(order.market)

    def process_pending(self, tick: MarketTick) -> None:
        portfolio = self.portfolios[tick.market]
        order = portfolio.pending_orders.get(tick.symbol)
        if not order:
            return
        timestamp = tick.received_timestamp or tick.timestamp
        if tick.market_status != "open" or tick.symbol_status != "trading":
            self.cancel_pending(tick.market, tick.symbol, "MARKET_HALTED", OrderState.MARKET_HALTED)
            return
        if (
            tick.session != "regular"
            or tick.luld_status != "normal"
            or tick.bid >= tick.ask
            or (tick.market == Market.US and tick.quote_scope != "consolidated")
        ):
            return
        spread_pct = (tick.ask - tick.bid) / tick.last * 100
        if spread_pct > self.costs[tick.market.value].max_spread_pct:
            return
        if timestamp >= order.expires_at:
            self.cancel_pending(tick.market, tick.symbol, "FILL_TIMEOUT", OrderState.EXPIRED)
            return
        if timestamp < order.eligible_fill_at or tick.ask > order.limit_price:
            return
        execution, fee = self._entry_fill(tick.market, tick.ask, order.limit_price)
        if execution <= 0:
            return
        policy = self.costs[tick.market.value]
        visible = tick.ask_size
        if visible <= 0:
            visible = math.floor(tick.trade_size * policy.volume_participation_pct / 100)
        visible = min(visible, policy.max_fill_quantity_per_tick)
        if visible <= 0:
            return
        if not policy.allow_partial_fill and visible < order.remaining:
            return
        self._apply_entry_fill(order, tick, min(order.remaining, visible), execution, fee)

    def cancel_pending(
        self, market: Market, symbol: str, reason: str, state: OrderState = OrderState.CANCELLED
    ) -> None:
        portfolio = self.portfolios[market]
        order = portfolio.pending_orders.pop(symbol, None)
        if not order:
            return
        order.state = state
        self._order_event(
            order,
            state,
            {"reason": reason, "filled_quantity": order.filled_quantity},
        )
        self._save(market)

    def expire_pending(self, now: datetime) -> None:
        for market, portfolio in self.portfolios.items():
            for symbol, order in list(portfolio.pending_orders.items()):
                if now >= order.expires_at:
                    self.cancel_pending(market, symbol, "FILL_TIMEOUT", OrderState.EXPIRED)

    def close_quantity(
        self,
        position: Position,
        quantity: int,
        tick: MarketTick,
        reason: str,
        state: OrderState = OrderState.CLOSED,
    ) -> None:
        quantity = min(quantity, position.remaining)
        if quantity <= 0:
            return
        gross = tick.bid * quantity
        net, fees = self._exit_net(position.market, gross)
        pnl = net - position.entry_price * quantity
        portfolio = self.portfolios[position.market]
        portfolio.cash += net
        portfolio.realized_pnl += pnl
        portfolio.session_realized_pnl += pnl
        position.realized_pnl += pnl
        position.fees += fees
        position.remaining -= quantity
        self.repository.add_event(
            "PAPER_SELL_FILLED",
            position.market,
            position.symbol,
            position.plan_id,
            {
                "order_id": str(uuid.uuid4()),
                "state": state.value,
                "reason": reason,
                "quantity": quantity,
                "price": tick.bid,
                "fees": fees,
                "pnl": pnl,
            },
        )
        if position.remaining == 0:
            if reason == "STOP_LOSS":
                portfolio.consecutive_stops += 1
                minutes = self.costs[position.market.value].stop_cooldown_minutes
                portfolio.cooldown_until = (tick.received_timestamp or tick.timestamp) + timedelta(
                    minutes=minutes
                )
            elif pnl > 0:
                portfolio.consecutive_stops = 0
            self.repository.add_event(
                "PAPER_POSITION_CLOSED",
                position.market,
                position.symbol,
                position.plan_id,
                {"pnl": position.realized_pnl, "fees": position.fees, "reason": reason},
            )
            self.repository.add_event(
                "ORDER_STATE_CHANGED",
                position.market,
                position.symbol,
                position.plan_id,
                {"state": state.value, "reason": reason},
            )
            portfolio.positions.pop(position.symbol, None)
        self._save(position.market)

    def _begin_exit(self, position: Position, tick: MarketTick, reason: str) -> None:
        if position.exit_pending_at is None:
            position.exit_pending_at = tick.received_timestamp or tick.timestamp
            position.exit_reason = reason
            self.repository.add_event(
                "ORDER_STATE_CHANGED",
                position.market,
                position.symbol,
                position.plan_id,
                {"state": OrderState.EXIT_PENDING.value, "reason": reason},
            )
            self.cancel_pending(position.market, position.symbol, "EXIT_TRIGGERED")

    def _process_exit_pending(self, position: Position, tick: MarketTick) -> bool:
        if not position.exit_pending_at:
            return False
        timestamp = tick.received_timestamp or tick.timestamp
        stop_limit = position.stop_price * (1 - position.stop_limit_offset_pct / 100)
        emergency = (timestamp - position.exit_pending_at).total_seconds()
        if tick.bid >= stop_limit or emergency >= position.emergency_exit_after_sec:
            self.close_quantity(
                position,
                position.remaining,
                tick,
                position.exit_reason or "STOP_LOSS",
            )
            return True
        return False

    def on_tick(self, tick: MarketTick) -> None:
        self.process_pending(tick)
        position = self.portfolios[tick.market].positions.get(tick.symbol)
        if not position or tick.market_status != "open" or tick.symbol_status != "trading":
            return
        timestamp = tick.source_timestamp or tick.timestamp
        position.highest_price = max(position.highest_price, tick.bid)
        if self._process_exit_pending(position, tick):
            return
        if tick.bid <= position.stop_price:
            self._begin_exit(position, tick, "STOP_LOSS")
            self._process_exit_pending(position, tick)
            return

        max_minutes = int(position.exit_policy.get("max_holding_minutes", 90))
        if timestamp - position.opened_at >= timedelta(minutes=max_minutes):
            self.close_quantity(position, position.remaining, tick, "MAX_HOLDING_TIME")
            return
        progress_minutes = position.exit_policy.get("no_progress_exit_minutes")
        min_progress = float(position.exit_policy.get("no_progress_min_pct", 0.3))
        if progress_minutes and timestamp - position.opened_at >= timedelta(
            minutes=int(progress_minutes)
        ):
            progress = (position.highest_price - position.entry_price) / position.entry_price * 100
            if progress < min_progress:
                self.close_quantity(position, position.remaining, tick, "NO_PROGRESS")
                return
        below_vwap_seconds = position.exit_policy.get("exit_if_below_vwap_sec")
        vwap = tick.indicators.get("vwap_regular")
        if below_vwap_seconds and vwap is not None:
            if tick.bid < float(vwap):
                position.below_vwap_since = position.below_vwap_since or timestamp
                if (timestamp - position.below_vwap_since).total_seconds() >= int(
                    below_vwap_seconds
                ):
                    self.close_quantity(position, position.remaining, tick, "VWAP_FAILURE")
                    return
            else:
                position.below_vwap_since = None

        for index, (target_price, quantity) in enumerate(position.targets):
            if index in position.filled_targets or quantity <= 0:
                continue
            if tick.bid >= target_price:
                self.cancel_pending(position.market, position.symbol, "TARGET_REACHED")
                position.filled_targets.add(index)
                self.close_quantity(position, quantity, tick, f"TAKE_PROFIT_{index + 1}")
                if (
                    index == 0
                    and position.remaining > 0
                    and position.exit_policy.get("move_stop_to_entry_after_tp1", False)
                ):
                    position.stop_price = max(position.stop_price, position.entry_price)
                if position.remaining == 0:
                    return
        self._save(tick.market)

    def force_close(self, market: Market, ticks: dict[str, MarketTick], reason: str) -> None:
        portfolio = self.portfolios[market]
        for symbol in list(portfolio.pending_orders):
            self.cancel_pending(market, symbol, reason, OrderState.FORCE_CLOSED)
        for symbol, position in list(portfolio.positions.items()):
            if symbol in ticks:
                self.close_quantity(
                    position, position.remaining, ticks[symbol], reason, OrderState.FORCE_CLOSED
                )

    def reset_day(self, market: Market) -> None:
        portfolio = self.portfolios[market]
        portfolio.trades_today = 0
        portfolio.order_submissions_today = 0
        portfolio.session_realized_pnl = 0.0
        portfolio.consecutive_stops = 0
        portfolio.cooldown_until = None
        self._save(market)

    def performance(self, market: Market) -> dict:
        pnls = self.repository.closed_trade_pnls(market)
        gross_profit = sum(value for value in pnls if value > 0)
        gross_loss = abs(sum(value for value in pnls if value < 0))
        equity = self.portfolios[market].initial_cash
        peak = equity
        max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        return {
            "market": market.value,
            "paper_sessions": self.repository.paper_session_count(market),
            "closed_trades": len(pnls),
            "net_pnl": sum(pnls),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "max_drawdown_pct": max_drawdown,
            "target_profit_factor": 1.2,
            "target_max_drawdown_pct": 5.0,
        }

    def view(self, market: Market, marks: dict[str, float] | None = None) -> dict:
        marks = marks or {}
        portfolio = self.portfolios[market]
        market_value = sum(
            position.remaining * marks.get(symbol, position.entry_price)
            for symbol, position in portfolio.positions.items()
        )
        return {
            "market": market.value,
            "cash": portfolio.cash,
            "equity": portfolio.cash + market_value,
            "realized_pnl": portfolio.realized_pnl,
            "session_realized_pnl": portfolio.session_realized_pnl,
            "daily_risk_used": self.daily_risk_used(market),
            "trades_today": portfolio.trades_today,
            "order_submissions_today": portfolio.order_submissions_today,
            "positions": [
                self._serialize_position(position) for position in portfolio.positions.values()
            ],
            "pending_orders": [
                self._serialize_order(order) for order in portfolio.pending_orders.values()
            ],
        }
