from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

from .models import Market, MarketTick, PlanStatus, TradePlan


class PlanConflictError(ValueError):
    pass


class Repository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS nonces (
                    market TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    PRIMARY KEY (market, trade_date)
                );
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_market_date_hash
                    ON plans(market, trade_date, content_hash);
                DROP INDEX IF EXISTS idx_one_plan_per_market_date;
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    market TEXT,
                    symbol TEXT,
                    plan_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_states (
                    market TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_states (
                    plan_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS kis_order_intents (
                    intent_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    side TEXT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    session TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol, trade_date, session)
                );
                CREATE INDEX IF NOT EXISTS idx_market_snapshots_lookup
                    ON market_snapshots(market, trade_date, session, updated_at);
                CREATE TABLE IF NOT EXISTS candidate_guard_states (
                    plan_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, symbol)
                );
                """
            )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def issue_nonce(
        self, market: Market, trade_date: date, ttl_minutes: int = 45
    ) -> tuple[str, datetime]:
        nonce = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO nonces(market, trade_date, nonce_hash, expires_at, used_at)
                   VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(market, trade_date) DO UPDATE SET
                     nonce_hash=excluded.nonce_hash,
                     expires_at=excluded.expires_at,
                     used_at=NULL""",
                (market.value, trade_date.isoformat(), self._hash(nonce), expires_at.isoformat()),
            )
        return nonce, expires_at

    def consume_nonce(self, market: Market, trade_date: date, nonce: str) -> bool:
        now = datetime.now(UTC)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM nonces WHERE market=? AND trade_date=?",
                (market.value, trade_date.isoformat()),
            ).fetchone()
            if not row or row["used_at"] or row["nonce_hash"] != self._hash(nonce):
                return False
            if datetime.fromisoformat(row["expires_at"]) < now:
                return False
            db.execute(
                "UPDATE nonces SET used_at=? WHERE market=? AND trade_date=? AND used_at IS NULL",
                (now.isoformat(), market.value, trade_date.isoformat()),
            )
            return db.total_changes == 1

    def store_plan(self, plan: TradePlan, status: PlanStatus) -> str:
        payload = plan.model_copy(update={"approval_nonce": "000000"}).model_dump_json()
        content_hash = self._hash(payload)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO plans(plan_id, market, trade_date, status, content_hash, payload,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.market.value,
                    plan.trade_date.isoformat(),
                    status.value,
                    content_hash,
                    payload,
                    now,
                    now,
                ),
            )
        self.add_event(
            "PLAN_STORED",
            plan.market,
            None,
            plan.plan_id,
            {"hash": content_hash, "status": status.value, "version": plan.plan_version},
        )
        return content_hash

    def approve_plan(self, plan: TradePlan) -> str | None:
        """Atomically consume the one-time code and persist an armed plan."""
        payload = plan.model_copy(update={"approval_nonce": "000000"}).model_dump_json()
        content_hash = self._hash(payload)
        now = datetime.now(UTC)
        replaced_plan_id = None
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM nonces WHERE market=? AND trade_date=?",
                (plan.market.value, plan.trade_date.isoformat()),
            ).fetchone()
            if (
                not row
                or row["used_at"]
                or row["nonce_hash"] != self._hash(plan.approval_nonce)
                or datetime.fromisoformat(row["expires_at"]) < now
            ):
                return None
            latest = db.execute(
                """SELECT plan_id, status, payload FROM plans
                   WHERE market=? AND trade_date=? ORDER BY created_at DESC LIMIT 1""",
                (plan.market.value, plan.trade_date.isoformat()),
            ).fetchone()
            if latest:
                previous = TradePlan.model_validate_json(latest["payload"])
                if plan.plan_version <= previous.plan_version:
                    raise PlanConflictError("plan_version must increase for a revised plan")
                if latest["status"] == PlanStatus.RUNNING.value:
                    raise PlanConflictError("a running plan cannot be replaced")
                if latest["status"] == PlanStatus.ARMED.value:
                    replaced_plan_id = latest["plan_id"]
                    db.execute(
                        "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?",
                        (PlanStatus.CANCELLED.value, now.isoformat(), replaced_plan_id),
                    )
            db.execute(
                """INSERT INTO plans(plan_id, market, trade_date, status, content_hash, payload,
                                      created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.market.value,
                    plan.trade_date.isoformat(),
                    PlanStatus.ARMED.value,
                    content_hash,
                    payload,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            db.execute(
                "UPDATE nonces SET used_at=? WHERE market=? AND trade_date=? AND used_at IS NULL",
                (now.isoformat(), plan.market.value, plan.trade_date.isoformat()),
            )
        self.add_event(
            "PLAN_STORED",
            plan.market,
            None,
            plan.plan_id,
            {
                "hash": content_hash,
                "status": PlanStatus.ARMED.value,
                "version": plan.plan_version,
            },
        )
        if replaced_plan_id:
            self.add_event(
                "PLAN_STATUS_CHANGED",
                plan.market,
                None,
                replaced_plan_id,
                {
                    "from": PlanStatus.ARMED.value,
                    "to": PlanStatus.CANCELLED.value,
                    "reason": "REPLACED_BY_NEW_VERSION",
                    "replacement_plan_id": plan.plan_id,
                },
            )
        return content_hash

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def active_plans(self, market: Market, trade_date: date) -> list[TradePlan]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload FROM plans WHERE market=? AND trade_date=?
                   AND status IN ('ARMED', 'RUNNING')""",
                (market.value, trade_date.isoformat()),
            ).fetchall()
        return [TradePlan.model_validate_json(row["payload"]) for row in rows]

    def set_plan_status(self, plan_id: str, status: PlanStatus) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT market, status FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if not row or row["status"] == status.value:
                return
            db.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?",
                (status.value, now, plan_id),
            )
        self.add_event(
            "PLAN_STATUS_CHANGED",
            Market(row["market"]),
            None,
            plan_id,
            {"from": row["status"], "to": status.value},
        )

    def add_event(
        self,
        event_type: str,
        market: Market | None,
        symbol: str | None,
        plan_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO events(event_type, market, symbol, plan_id, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event_type,
                    market.value if market else None,
                    symbol,
                    plan_id,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def save_market_snapshot(self, tick: MarketTick, trade_date: date) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO market_snapshots(
                       market, symbol, trade_date, session, payload, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(market, symbol, trade_date, session) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    tick.market.value,
                    tick.symbol.upper(),
                    trade_date.isoformat(),
                    tick.session,
                    tick.model_dump_json(),
                    now,
                ),
            )

    def market_snapshots(
        self, market: Market, trade_date: date, session: str
    ) -> list[MarketTick]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload FROM market_snapshots
                   WHERE market=? AND trade_date=? AND session=? ORDER BY updated_at DESC""",
                (market.value, trade_date.isoformat(), session),
            ).fetchall()
        return [MarketTick.model_validate_json(row["payload"]) for row in rows]

    def block_candidate(
        self,
        plan_id: str,
        symbol: str,
        market: Market,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO candidate_guard_states(
                       plan_id, symbol, market, status, reason, payload, updated_at
                   ) VALUES (?, ?, ?, 'RISK_BLOCKED', ?, ?, ?)
                   ON CONFLICT(plan_id, symbol) DO UPDATE SET
                     status='RISK_BLOCKED', reason=excluded.reason,
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    plan_id,
                    symbol.upper(),
                    market.value,
                    reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                ),
            )

    def candidate_guard_state(self, plan_id: str, symbol: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM candidate_guard_states WHERE plan_id=? AND symbol=?",
                (plan_id, symbol.upper()),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def candidate_guard_states(self, plan_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM candidate_guard_states WHERE plan_id=? ORDER BY symbol",
                (plan_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_kis_order_intent(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        market: Market,
        symbol: str,
        plan_id: str,
        action: str,
        side: str | None,
        status: str,
        payload: dict[str, Any],
    ) -> bool:
        """Persist a sanitized KIS order intent exactly once.

        Account numbers, app keys, tokens, and secrets must never be included in payload.
        """
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    """INSERT INTO kis_order_intents(
                           intent_id, idempotency_key, market, symbol, plan_id,
                           action, side, status, payload, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        intent_id,
                        idempotency_key,
                        market.value,
                        symbol.upper(),
                        plan_id,
                        action,
                        side,
                        status,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_kis_order_intents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM kis_order_intents ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def plan_events(self, plan_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE plan_id=? ORDER BY id DESC LIMIT ?",
                (plan_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_order_state(self, plan_id: str, symbol: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT payload FROM events
                   WHERE plan_id=? AND symbol=? AND event_type='ORDER_STATE_CHANGED'
                   ORDER BY id DESC LIMIT 1""",
                (plan_id, symbol.upper()),
            ).fetchone()
        return str(json.loads(row["payload"]).get("state")) if row else None

    def save_portfolio(self, market: Market, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO portfolio_states(market, payload, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(market) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    market.value,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_portfolio(self, market: Market) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM portfolio_states WHERE market=?", (market.value,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def closed_trade_pnls(self, market: Market) -> list[float]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload FROM events
                   WHERE event_type='PAPER_POSITION_CLOSED' AND market=? ORDER BY id""",
                (market.value,),
            ).fetchall()
        return [float(json.loads(row["payload"]).get("pnl", 0.0)) for row in rows]

    def paper_session_count(self, market: Market) -> int:
        with self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM events
                   WHERE event_type='PAPER_SESSION_STARTED' AND market=?""",
                (market.value,),
            ).fetchone()
        return int(row["count"])

    def claim_idempotency_key(
        self, idempotency_key: str, market: Market, symbol: str, plan_id: str
    ) -> bool:
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    """INSERT INTO idempotency_keys(
                           idempotency_key, market, symbol, plan_id, created_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        idempotency_key,
                        market.value,
                        symbol,
                        plan_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def save_strategy_state(
        self, plan_id: str, symbol: str, market: Market, payload: dict[str, Any]
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO strategy_states(plan_id, symbol, market, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(plan_id, symbol) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (
                    plan_id,
                    symbol.upper(),
                    market.value,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_strategy_state(self, plan_id: str, symbol: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM strategy_states WHERE plan_id=? AND symbol=?",
                (plan_id, symbol.upper()),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def delete_strategy_state(self, plan_id: str, symbol: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM strategy_states WHERE plan_id=? AND symbol=?",
                (plan_id, symbol.upper()),
            )
