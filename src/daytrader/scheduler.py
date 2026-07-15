from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .broker import PaperBroker
from .engine import TradingEngine
from .market_clock import MARKET_TZ, force_exit_at, is_session
from .models import Market, PlanStatus
from .notifications import TelegramNotifier
from .repository import Repository


class SessionScheduler:
    def __init__(
        self,
        repository: Repository,
        engine: TradingEngine,
        broker: PaperBroker,
        notifier: TelegramNotifier,
    ):
        self.repository = repository
        self.engine = engine
        self.broker = broker
        self.notifier = notifier
        self.scheduler = AsyncIOScheduler()
        self.closed_sessions: set[tuple[Market, str]] = set()

    async def issue_nonce(self, market: Market) -> None:
        local_date = datetime.now(MARKET_TZ[market]).date()
        if not is_session(market, local_date):
            return
        nonce, expires_at = self.repository.issue_nonce(market, local_date)
        await self.notifier.send(
            f"[{market.value}] GPT 거래계획 승인코드: {nonce}\n"
            f"거래일: {local_date}\n만료: {expires_at.isoformat()}"
        )

    async def force_close(self, market: Market, trade_date) -> None:
        self.engine.force_close_market(market)
        portfolio = self.broker.view(market)
        fully_closed = not portfolio["positions"] and not portfolio["pending_orders"]
        for plan in self.repository.active_plans(market, trade_date):
            if fully_closed:
                self.repository.set_plan_status(plan.plan_id, PlanStatus.COMPLETED)
        if not fully_closed:
            self.repository.add_event(
                "UNPRICED_FORCE_CLOSE_PENDING",
                market,
                None,
                None,
                {"trade_date": trade_date, "positions": portfolio["positions"]},
            )
        await self.notifier.send(
            f"[{market.value}] 가상 장마감 처리 {'완료' if fully_closed else '대기'}\n"
            f"현금: {portfolio['cash']:.2f}\n누적 실현손익: {portfolio['realized_pnl']:.2f}"
        )

    async def session_guard(self, market: Market) -> None:
        now = datetime.now(MARKET_TZ[market])
        trade_date = now.date()
        key = (market, trade_date.isoformat())
        if key in self.closed_sessions or not is_session(market, trade_date):
            return
        exit_at = force_exit_at(market, trade_date)
        if now >= exit_at:
            await self.force_close(market, trade_date)
            self.closed_sessions.add(key)

    def reset_trade_counter(self, market: Market) -> None:
        self.broker.reset_day(market)

    def start(self) -> None:
        jobs = [
            (self.issue_nonce, (Market.KR,), 15, 8, "Asia/Seoul", "kr_nonce"),
            (self.issue_nonce, (Market.US,), 45, 8, "America/New_York", "us_nonce"),
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
            self.engine.maintenance,
            IntervalTrigger(seconds=1),
            id="order_timeout_maintenance",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
