from __future__ import annotations

import hmac
import html
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from .broker import PaperBroker
from .config import Settings, load_costs, load_universe
from .engine import TradingEngine
from .experiment import PortfolioExperimentManager
from .kis_orders import KISOrderIntentRecorder
from .kis_readonly import KISQuotePoller, KISReadOnlyClient
from .models import (
    ExperimentCohort,
    Market,
    MarketTick,
    NonceRequest,
    NonceResponse,
    PlanReceipt,
    PlanStatus,
    PortfolioExperimentReceipt,
    PortfolioExperimentRequest,
    TradePlan,
)
from .notifications import TelegramNotifier
from .repository import PlanConflictError, Repository
from .scheduler import SessionScheduler
from .scanner import CandidateScanner
from .validator import PlanValidationError, validate_plan


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization.removeprefix("Bearer ").strip()


def _require(expected: str, authorization: str | None) -> None:
    if not expected or not hmac.compare_digest(_bearer(authorization), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def create_app(settings: Settings | None = None, *, start_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(settings.database_path)
    universe = load_universe(settings.universe_path)
    costs = load_costs(settings.costs_path)
    order_intent_recorder = KISOrderIntentRecorder(
        repository,
        account_configured=bool(settings.kis_account_number),
        product_code=settings.kis_product_code,
    )
    broker = PaperBroker(repository, costs, order_intent_recorder)
    engine = TradingEngine(repository, broker)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    session_scheduler = SessionScheduler(repository, engine, broker, notifier)
    quote_poller = None
    candidate_scanner = CandidateScanner(repository, universe, settings)
    experiment_manager = PortfolioExperimentManager(
        repository, costs, settings.experiment_data_path
    )
    if settings.kis_poll_enabled:
        if not settings.kis_app_key or not settings.kis_app_secret:
            raise ValueError("KIS polling requires KIS_APP_KEY and KIS_APP_SECRET")
        quote_poller = KISQuotePoller(
            KISReadOnlyClient(settings.kis_app_key, settings.kis_app_secret, settings.kis_env),
            repository,
            engine,
            universe,
            settings.kis_poll_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            session_scheduler.start()
        if quote_poller:
            quote_poller.start()
        yield
        if quote_poller:
            await quote_poller.stop()
        session_scheduler.stop()

    app = FastAPI(
        title="AI Day Trader Simulator",
        version="0.7.0",
        description="GPT-approved paper trading with record-only KIS order intents.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.universe = universe
    app.state.costs = costs
    app.state.broker = broker
    app.state.order_intent_recorder = order_intent_recorder
    app.state.engine = engine
    app.state.notifier = notifier
    app.state.scheduler = session_scheduler
    app.state.quote_poller = quote_poller
    app.state.candidate_scanner = candidate_scanner
    app.state.experiment_manager = experiment_manager

    @app.middleware("http")
    async def reject_large_payload(request: Request, call_next):
        length = int(request.headers.get("content-length", "0") or "0")
        if length > 65_536:
            return JSONResponse(
                status_code=413, content={"detail": "payload too large"}
            )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "mode": "paper-only",
            "kis_order_mode": settings.kis_order_mode,
            "live_orders": False,
        }

    @app.post("/v1/admin/nonces", response_model=NonceResponse)
    async def issue_nonce(
        payload: NonceRequest, authorization: str | None = Header(default=None)
    ) -> NonceResponse:
        _require(settings.admin_bearer, authorization)
        nonce, expires_at = repository.issue_nonce(payload.market, payload.trade_date)
        await notifier.send(f"[{payload.market.value}] 승인코드 {nonce} / {payload.trade_date}")
        return NonceResponse(
            market=payload.market,
            trade_date=payload.trade_date,
            nonce=nonce,
            expires_at=expires_at,
        )

    @app.post("/v1/gpt-actions/plans", response_model=PlanReceipt, status_code=201)
    async def register_plan(
        plan: TradePlan, authorization: str | None = Header(default=None)
    ) -> PlanReceipt:
        _require(settings.gpt_action_bearer, authorization)
        try:
            validate_plan(plan, universe, costs)
        except PlanValidationError as exc:
            repository.add_event(
                "PLAN_REJECTED",
                plan.market,
                None,
                plan.plan_id,
                {"reason": str(exc), "plan_version": plan.plan_version},
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            content_hash = repository.approve_plan(plan)
        except PlanConflictError as exc:
            repository.add_event(
                "PLAN_REJECTED", plan.market, None, plan.plan_id, {"reason": str(exc)}
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="duplicate or conflicting plan") from exc
        if content_hash is None:
            repository.add_event(
                "PLAN_REJECTED",
                plan.market,
                None,
                plan.plan_id,
                {"reason": "INVALID_EXPIRED_OR_REUSED_APPROVAL_CODE"},
            )
            raise HTTPException(status_code=409, detail="invalid, expired, or reused approval code")
        await notifier.send(
            f"[{plan.market.value}] 계획 승인 완료: {plan.plan_id}\n"
            f"종목: {', '.join(c.symbol for c in plan.approved_symbols)}"
        )
        return PlanReceipt(
            plan_id=plan.plan_id,
            status=PlanStatus.ARMED,
            content_hash=content_hash,
            message="approved plan is armed for paper trading",
        )

    @app.get("/v1/gpt-actions/candidates")
    async def candidate_shortlist(
        market: Market,
        phase: Literal["auto", "premarket", "regular"] = "auto",
        limit: int = Query(default=5, ge=1, le=10),
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require(settings.gpt_action_bearer, authorization)
        return candidate_scanner.scan(market, phase, limit)

    @app.post(
        "/v1/gpt-actions/experiments",
        response_model=PortfolioExperimentReceipt,
        status_code=201,
    )
    async def register_portfolio_experiment(
        request: PortfolioExperimentRequest,
        authorization: str | None = Header(default=None),
    ) -> PortfolioExperimentReceipt:
        _require(settings.gpt_action_bearer, authorization)
        validation_plan = TradePlan(
            plan_id=f"{request.experiment_id}_validation",
            created_at=request.created_at,
            market=request.market,
            trade_date=request.trade_date,
            expires_at=request.expires_at,
            approval_nonce=request.approval_nonce,
            approved_symbols=request.candidates,
        )
        try:
            validate_plan(validation_plan, universe, costs)
        except PlanValidationError as exc:
            repository.add_event(
                "PORTFOLIO_EXPERIMENT_REJECTED",
                request.market,
                None,
                None,
                {"experiment_id": request.experiment_id, "reason": str(exc)},
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            approved = repository.approve_portfolio_experiment(request)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="duplicate experiment") from exc
        if not approved:
            raise HTTPException(status_code=409, detail="invalid, expired, or reused approval code")
        experiment_manager.register(request)
        await notifier.send(
            f"[{request.market.value}] 비교실험 시작: {request.experiment_id}\n"
            f"GPT 전체: {', '.join(item.symbol for item in request.candidates)}\n"
            f"사용자 선택: {', '.join(request.user_selected_symbols)}"
        )
        return PortfolioExperimentReceipt(
            experiment_id=request.experiment_id,
            status="ACTIVE",
            cohorts=list(ExperimentCohort),
            message="three isolated paper-only comparison cohorts are active",
        )

    @app.get("/v1/gpt-actions/experiments/{experiment_id}")
    async def portfolio_experiment_status(
        experiment_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require(settings.gpt_action_bearer, authorization)
        result = experiment_manager.view(experiment_id)
        if result is None:
            raise HTTPException(status_code=404, detail="experiment not found")
        return result

    @app.get("/v1/gpt-actions/plans/{plan_id}/status")
    async def plan_status(
        plan_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        _require(settings.gpt_action_bearer, authorization)
        row = repository.get_plan(plan_id)
        if not row:
            raise HTTPException(status_code=404, detail="plan not found")
        stored_plan = TradePlan.model_validate_json(row["payload"])
        strategy_states = [
            {
                "symbol": candidate.symbol,
                **(
                    repository.load_strategy_state(plan_id, candidate.symbol)
                    or {"phase": "NOT_STARTED"}
                ),
            }
            for candidate in stored_plan.approved_symbols
            if candidate.strategy_type == "pullback_rebreak"
        ]
        return {
            "plan_id": row["plan_id"],
            "plan_version": stored_plan.plan_version,
            "market": row["market"],
            "trade_date": row["trade_date"],
            "status": row["status"],
            "content_hash": row["content_hash"],
            "recent_events": repository.plan_events(plan_id),
            "strategy_states": strategy_states,
            "candidate_guards": repository.candidate_guard_states(plan_id),
        }

    @app.post("/v1/market-data/ticks", status_code=202)
    async def ingest_tick(
        tick: MarketTick, authorization: str | None = Header(default=None)
    ) -> dict:
        _require(settings.market_data_bearer, authorization)
        result = engine.process_tick(tick)
        experiment_manager.process_tick(tick)
        experiment_manager.maintenance()
        return {**result, "symbol": tick.symbol, "market": tick.market}

    @app.get("/v1/portfolios/{market}")
    async def portfolio(
        market: Market, authorization: str | None = Header(default=None)
    ) -> dict:
        _require(settings.admin_bearer, authorization)
        marks = {
            symbol: tick.last
            for (tick_market, symbol), tick in engine.latest_ticks.items()
            if tick_market == market
        }
        return broker.view(market, marks)

    @app.get("/v1/performance/{market}")
    async def performance(
        market: Market, authorization: str | None = Header(default=None)
    ) -> dict:
        _require(settings.admin_bearer, authorization)
        return broker.performance(market)

    @app.get("/v1/admin/kis-order-intents")
    async def kis_order_intents(
        limit: int = Query(default=100, ge=1, le=500),
        authorization: str | None = Header(default=None),
    ) -> list[dict]:
        _require(settings.admin_bearer, authorization)
        return repository.recent_kis_order_intents(limit)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(authorization: str | None = Header(default=None)) -> str:
        _require(settings.admin_bearer, authorization)
        events = repository.recent_events(30)
        rows = "".join(
            f"<tr><td>{html.escape(e['created_at'])}</td>"
            f"<td>{html.escape(e['event_type'])}</td>"
            f"<td>{html.escape(e['market'] or '')}</td>"
            f"<td>{html.escape(e['symbol'] or '')}</td>"
            f"<td><code>{html.escape(e['payload'])}</code></td></tr>"
            for e in events
        )
        return f"""<!doctype html><html lang='ko'><meta charset='utf-8'>
        <title>AI Day Trader Simulator</title>
        <style>body{{font-family:system-ui;margin:2rem;max-width:1200px}}
        table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.5rem}}
        code{{white-space:pre-wrap}}</style>
        <h1>AI Day Trader Simulator</h1><p>Mode: <b>paper-only / KIS record-only</b></p>
        <h2>Recent events</h2><table><tr><th>Time</th><th>Event</th><th>Market</th>
        <th>Symbol</th><th>Payload</th></tr>{rows}</table></html>"""

    return app
