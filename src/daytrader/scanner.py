from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from .config import Settings
from .market_clock import MARKET_TZ, is_session, session_bounds
from .models import Market, MarketTick
from .repository import Repository


class CandidateScanner:
    """Rank the fixed multi-theme universe from persisted read-only snapshots."""

    def __init__(
        self,
        repository: Repository,
        universe: dict[str, dict[str, dict[str, str]]],
        settings: Settings,
    ):
        self.repository = repository
        self.universe = universe
        self.settings = settings

    @staticmethod
    def _spread(tick: MarketTick) -> float:
        return (tick.ask - tick.bid) / tick.last * 100

    def _fresh(self, tick: MarketTick, now: datetime) -> bool:
        return (
            abs((now.astimezone(UTC) - tick.received_timestamp.astimezone(UTC)).total_seconds())
            <= self.settings.candidate_snapshot_max_age_seconds
        )

    @staticmethod
    def _ready_value(tick: MarketTick, name: str) -> float | bool | None:
        if not tick.indicator_ready.get(name, False):
            return None
        return tick.indicators.get(name)

    def _window(self, market: Market, trade_date: date, now: datetime) -> dict[str, Any]:
        if not is_session(market, trade_date):
            return {"status": "MARKET_CLOSED", "phase": "closed"}
        session_open, session_close = session_bounds(market, trade_date)
        lead = timedelta(minutes=20 if market == Market.KR else 45)
        selection_end = session_open - timedelta(minutes=5) if market == Market.KR else session_open
        ready_at = session_open + timedelta(minutes=10)
        local_now = now.astimezone(MARKET_TZ[market])
        if local_now < session_open - lead:
            status, phase = "WAITING_FOR_PREMARKET", "premarket"
        elif market == Market.KR and local_now >= selection_end and local_now < session_open:
            status, phase = "PREOPEN_RECHECK", "premarket"
        elif local_now < session_open:
            status, phase = "PREMARKET_SCAN_OPEN", "premarket"
        elif local_now < ready_at:
            status, phase = "REGULAR_WARMUP", "regular"
        elif local_now < session_close:
            status, phase = "REGULAR_CONFIRMATION_OPEN", "regular"
        else:
            status, phase = "MARKET_CLOSED", "closed"
        return {
            "status": status,
            "phase": phase,
            "premarket_scan_start": (session_open - lead).isoformat(),
            "candidate_selection_end": selection_end.isoformat(),
            "regular_open": session_open.isoformat(),
            "regular_confirmation_at": ready_at.isoformat(),
            "regular_close": session_close.isoformat(),
        }

    def scan(
        self,
        market: Market,
        phase: Literal["auto", "premarket", "regular"] = "auto",
        limit: int = 5,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        trade_date = now.astimezone(MARKET_TZ[market]).date()
        window = self._window(market, trade_date, now)
        selected_phase = window.get("phase", "closed") if phase == "auto" else phase
        session = "premarket" if selected_phase == "premarket" else "regular"
        snapshots = self.repository.market_snapshots(market, trade_date, session)
        allowed = self.universe.get(market.value, {})
        candidates: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for tick in snapshots:
            metadata = allowed.get(tick.symbol)
            if metadata is None:
                continue
            reasons: list[str] = []
            spread = self._spread(tick)
            if not self._fresh(tick, now):
                reasons.append("STALE_SNAPSHOT")
            if tick.market_status != "open" or tick.symbol_status != "trading":
                reasons.append("NOT_TRADING")
            if tick.bid >= tick.ask:
                reasons.append("INVALID_OR_LOCKED_QUOTE")
            score = 0.0
            details: dict[str, Any] = {}
            if selected_phase == "premarket":
                if spread > self.settings.candidate_premarket_max_spread_pct:
                    reasons.append("PREMARKET_SPREAD_TOO_WIDE")
                gap = self._ready_value(tick, "premarket_gap_pct")
                volume = self._ready_value(tick, "premarket_volume")
                if gap is None:
                    reasons.append("PREMARKET_GAP_NOT_READY")
                elif not -1.0 <= float(gap) <= 8.0:
                    reasons.append("PREMARKET_GAP_OUT_OF_RANGE")
                if volume is None or float(volume) <= 0:
                    reasons.append("PREMARKET_VOLUME_NOT_READY")
                score = max(0.0, min(float(gap or 0), 5.0)) * 12 + min(
                    float(volume or 0) / 100_000, 25
                ) - spread * 20
                details = {
                    "premarket_gap_pct": gap,
                    "premarket_volume": volume,
                    "premarket_vwap": self._ready_value(tick, "premarket_vwap"),
                    "previous_close": self._ready_value(tick, "previous_close"),
                }
            else:
                relative_name = "relative_volume_cumulative_20d_same_time_regular"
                relative_volume = self._ready_value(tick, relative_name)
                vwap = self._ready_value(tick, "vwap_regular")
                market_above = self._ready_value(tick, "market_above_vwap_regular")
                if spread > self.settings.candidate_regular_max_spread_pct:
                    reasons.append("REGULAR_SPREAD_TOO_WIDE")
                if relative_volume is None:
                    reasons.append("RELATIVE_VOLUME_NOT_READY")
                elif float(relative_volume) < self.settings.candidate_regular_relative_volume_min:
                    reasons.append("RELATIVE_VOLUME_TOO_LOW")
                if vwap is None:
                    reasons.append("VWAP_NOT_READY")
                elif tick.last <= float(vwap):
                    reasons.append("PRICE_NOT_ABOVE_VWAP")
                if market_above is not True:
                    reasons.append("MARKET_NOT_ABOVE_VWAP")
                opening_ready = self._ready_value(tick, "opening_range_5_high")
                if opening_ready is None:
                    reasons.append("OPENING_RANGE_NOT_READY")
                score = min(float(relative_volume or 0), 4.0) * 20 - spread * 25
                if vwap and tick.last > float(vwap):
                    score += min((tick.last / float(vwap) - 1) * 100, 3) * 8
                details = {
                    "relative_volume": relative_volume,
                    "vwap_regular": vwap,
                    "market_above_vwap_regular": market_above,
                    "opening_range_5_high": opening_ready,
                    "rsi_14_1m_regular": self._ready_value(tick, "rsi_14_1m_regular"),
                }
            if reasons:
                exclusions.append({"symbol": tick.symbol, "reasons": reasons})
                continue
            candidates.append(
                {
                    "symbol": tick.symbol,
                    "name": metadata["name"],
                    "theme": metadata.get("theme", "미분류"),
                    "exchange": metadata["exchange"],
                    "score": round(score, 3),
                    "as_of": tick.source_timestamp.isoformat(),
                    "session": tick.session,
                    "quote_scope": tick.quote_scope,
                    "last": tick.last,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread_pct": round(spread, 4),
                    "bid_size": tick.bid_size,
                    "ask_size": tick.ask_size,
                    "indicators": details,
                    "premarket_guard_template": {
                        "reference_price": tick.last,
                        "max_open_deviation_pct": self.settings.candidate_open_deviation_pct,
                        "max_spread_pct": self.settings.candidate_regular_max_spread_pct,
                        "relative_volume_min": self.settings.candidate_regular_relative_volume_min,
                        "require_above_vwap": True,
                        "require_market_above_vwap": True,
                    },
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return {
            "market": market.value,
            "trade_date": trade_date.isoformat(),
            "phase": selected_phase,
            "status": window.get("status"),
            "as_of": now.isoformat(),
            "window": window,
            "execution_note": (
                "This endpoint is read-only. A separate one-time approval code is required "
                "before any record-only paper plan is armed."
            ),
            "candidates": candidates[:limit],
            "exclusions": exclusions,
        }
