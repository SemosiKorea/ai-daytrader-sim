from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .broker import PaperBroker
from .engine import TradingEngine
from .market_clock import MARKET_TZ, force_exit_at, is_session, session_bounds
from .models import Market, PlanStatus
from .notifications import TelegramNotifier
from .repository import Repository

if TYPE_CHECKING:
    from .experiment import PortfolioExperimentManager


class SessionScheduler:
    def __init__(
        self,
        repository: Repository,
        engine: TradingEngine,
        broker: PaperBroker,
        notifier: TelegramNotifier,
        experiment_manager: "PortfolioExperimentManager | None" = None,
        *,
        issue_approval_nonces: bool = True,
    ):
        self.repository = repository
        self.engine = engine
        self.broker = broker
        self.notifier = notifier
        self.experiment_manager = experiment_manager
        self.issue_approval_nonces = issue_approval_nonces
        self.scheduler = AsyncIOScheduler()
        self.closed_sessions: set[tuple[Market, str]] = set()
        self.pending_close_notifications: set[tuple[Market, str]] = set()

    async def issue_nonce(self, market: Market) -> None:
        local_date = datetime.now(MARKET_TZ[market]).date()
        if not is_session(market, local_date):
            return
        nonce, expires_at = self.repository.issue_nonce(market, local_date)
        await self.notifier.send_best_effort(
            f"[{market.value}] GPT 거래계획 승인코드: {nonce}\n"
            f"거래일: {local_date}\n만료: {expires_at.isoformat()}"
        )

    async def force_close(self, market: Market, trade_date) -> bool:
        self.engine.force_close_market(market)
        portfolio = self.broker.view(market)
        main_closed = not portfolio["positions"] and not portfolio["pending_orders"]
        _, session_close = session_bounds(market, trade_date)
        if (
            not main_closed
            and datetime.now(MARKET_TZ[market]) >= session_close.astimezone(MARKET_TZ[market])
        ):
            main_closed = self.engine.recovery_force_close_market(
                market, trade_date, "SESSION_CLOSE_LAST_KNOWN_QUOTE"
            )
            portfolio = self.broker.view(market)
        experiments_closed = (
            self.experiment_manager.force_close_market(market, trade_date)
            if self.experiment_manager
            else True
        )
        fully_closed = main_closed and experiments_closed
        for plan in self.repository.active_plans(market, trade_date):
            if main_closed:
                self.repository.set_plan_status(plan.plan_id, PlanStatus.COMPLETED)
        if not fully_closed:
            self.repository.add_event(
                "UNPRICED_FORCE_CLOSE_PENDING",
                market,
                None,
                None,
                {"trade_date": trade_date, "positions": portfolio["positions"]},
            )
        notification_key = (market, trade_date.isoformat())
        if fully_closed or notification_key not in self.pending_close_notifications:
            await self.notifier.send_best_effort(
                f"[{market.value}] 가상 장마감 처리 {'완료' if fully_closed else '대기'}\n"
                f"현금: {portfolio['cash']:.2f}\n누적 실현손익: {portfolio['realized_pnl']:.2f}"
            )
        if fully_closed:
            self.pending_close_notifications.discard(notification_key)
        else:
            self.pending_close_notifications.add(notification_key)
        return fully_closed

    async def session_guard(self, market: Market) -> None:
        now = datetime.now(MARKET_TZ[market])
        trade_date = now.date()
        key = (market, trade_date.isoformat())
        if key in self.closed_sessions or not is_session(market, trade_date):
            return
        exit_at = force_exit_at(market, trade_date)
        if now >= exit_at:
            if await self.force_close(market, trade_date):
                self.closed_sessions.add(key)

    def reset_trade_counter(self, market: Market) -> None:
        self.broker.reset_day(market)

    def maintenance(self) -> None:
        self.engine.maintenance()
        if self.experiment_manager:
            self.experiment_manager.maintenance()

    def start(self) -> None:
        jobs = [
            (self.reset_trade_counter, (Market.KR,), 0, 8, "Asia/Seoul", "kr_reset"),
            (
                self.reset_trade_counter,
                (Market.US,),
                30,
                8,
                "America/New_York",
                "us_reset",
            ),
        ]
        if self.issue_approval_nonces:
            jobs.extend(
                [
                    (self.issue_nonce, (Market.KR,), 15, 8, "Asia/Seoul", "kr_nonce"),
                    (self.issue_nonce, (Market.US,), 45, 8, "America/New_York", "us_nonce"),
                ]
            )
        for function, args, minute, hour, timezone, job_id in jobs:
            self.scheduler.add_job(
                function,
                CronTrigger(
                    minute=minute, hour=hour, day_of_week="mon-fri", timezone=timezone
                ),
                args=args,
                id=job_id,
                replace_existing=True,
            )
        for market in Market:
            self.scheduler.add_job(
                self.session_guard,
                CronTrigger(minute="*", second=0, timezone=MARKET_TZ[market]),
                args=(market,),
                id=f"{market.value.lower()}_session_guard",
                replace_existing=True,
            )
        self.scheduler.add_job(
            self.maintenance,
            IntervalTrigger(seconds=1),
            id="order_timeout_maintenance",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
