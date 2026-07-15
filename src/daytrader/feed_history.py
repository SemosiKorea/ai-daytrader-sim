from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

from .market_clock import MARKET_TZ, is_session, session_bounds
from .models import Market


@dataclass(slots=True)
class MinuteBar:
    market: Market
    symbol: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    notional: float

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=1)


class FeedHistoryStore:
    """Durable completed one-minute bars used to warm deterministic indicators."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS minute_bars (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    notional REAL NOT NULL,
                    PRIMARY KEY (market, symbol, start)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def save(self, bar: MinuteBar) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO minute_bars(
                    market, symbol, start, open, high, low, close, volume, notional
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol, start) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    notional=excluded.notional
                """,
                (
                    bar.market.value,
                    bar.symbol.upper(),
                    bar.start.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.notional,
                ),
            )

    def load(self, market: Market, symbol: str, limit: int = 20_000) -> list[MinuteBar]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM (
                    SELECT * FROM minute_bars
                    WHERE market=? AND symbol=? ORDER BY start DESC LIMIT ?
                ) ORDER BY start
                """,
                (market.value, symbol.upper(), limit),
            ).fetchall()
        return [
            MinuteBar(
                market=Market(row["market"]),
                symbol=row["symbol"],
                start=datetime.fromisoformat(row["start"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                notional=float(row["notional"]),
            )
            for row in rows
        ]

    def import_csv(self, path: Path) -> int:
        """Import verified regular-session bars.

        Required columns: market,symbol,timestamp,open,high,low,close,volume.
        An optional notional column preserves exact VWAP; otherwise close*volume is used.
        """
        imported = 0
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {
                "market",
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            }
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                missing = required - set(reader.fieldnames or [])
                raise ValueError(f"history CSV is missing columns: {sorted(missing)}")
            for row in reader:
                market = Market(row["market"].upper())
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    raise ValueError("history CSV timestamps must include a timezone")
                local_date = timestamp.astimezone(MARKET_TZ[market]).date()
                if not is_session(market, local_date):
                    raise ValueError(f"history bar is not on a session: {timestamp.isoformat()}")
                session_open, session_close = session_bounds(market, local_date)
                local = timestamp.astimezone(MARKET_TZ[market])
                if not session_open <= local < session_close:
                    raise ValueError(
                        f"history bar is outside regular hours: {timestamp.isoformat()}"
                    )
                volume = int(row["volume"])
                close = float(row["close"])
                notional = float(row.get("notional") or close * volume)
                self.save(
                    MinuteBar(
                        market=market,
                        symbol=row["symbol"].upper(),
                        start=local.replace(second=0, microsecond=0),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=close,
                        volume=volume,
                        notional=notional,
                    )
                )
                imported += 1
        return imported


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for item in values[period:]:
        value = (item - value) * multiplier + value
    return value


def _wilder_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    strength = average_gain / average_loss
    return 100 - 100 / (1 + strength)


def _wilder_atr(bars: list[MinuteBar], period: int = 14) -> float | None:
    if len(bars) < period:
        return None
    ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        candidates = [bar.high - bar.low]
        if previous_close is not None:
            candidates.extend((abs(bar.high - previous_close), abs(bar.low - previous_close)))
        ranges.append(max(candidates))
        previous_close = bar.close
    value = sum(ranges[:period]) / period
    for true_range in ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


class IndicatorCalculator:
    """Compute the exact indicator contract from regular-session trades and stored bars."""

    def __init__(self, market: Market, symbol: str, store: FeedHistoryStore):
        self.market = market
        self.symbol = symbol.upper()
        self.store = store
        self.completed = store.load(market, symbol)
        self.current: MinuteBar | None = None
        self.session_date: date | None = None
        self.session_notional = 0.0
        self.session_volume = 0
        self.first_trade: float | None = None

    def _bar_date(self, bar: MinuteBar) -> date:
        return bar.start.astimezone(MARKET_TZ[self.market]).date()

    def _begin_session(self, trade_date: date) -> None:
        self.session_date = trade_date
        today = [bar for bar in self.completed if self._bar_date(bar) == trade_date]
        self.session_notional = sum(bar.notional for bar in today)
        self.session_volume = sum(bar.volume for bar in today)
        session_open, _ = session_bounds(self.market, trade_date)
        opening_bar = next((bar for bar in today if bar.start == session_open), None)
        self.first_trade = opening_bar.open if opening_bar else None
        self.current = None

    def _complete_current(self) -> None:
        if self.current is None:
            return
        self.store.save(self.current)
        self.completed.append(self.current)
        self.current = None

    def on_trade(self, price: float, size: int, at: datetime) -> None:
        local = at.astimezone(MARKET_TZ[self.market])
        trade_date = local.date()
        if not is_session(self.market, trade_date):
            return
        session_open, session_close = session_bounds(self.market, trade_date)
        if not session_open <= local < session_close:
            return
        if self.session_date != trade_date:
            self._complete_current()
            self._begin_session(trade_date)
        minute = local.replace(second=0, microsecond=0)
        if self.current is not None and minute > self.current.start:
            self._complete_current()
        if self.current is None:
            self.current = MinuteBar(
                market=self.market,
                symbol=self.symbol,
                start=minute,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0,
                notional=0.0,
            )
        self.current.high = max(self.current.high, price)
        self.current.low = min(self.current.low, price)
        self.current.close = price
        quantity = max(0, size)
        self.current.volume += quantity
        self.current.notional += price * quantity
        self.session_volume += quantity
        self.session_notional += price * quantity
        session_open, _ = session_bounds(self.market, trade_date)
        if minute == session_open and self.first_trade is None:
            self.first_trade = price

    def _sessions(self) -> list[date]:
        return sorted({self._bar_date(bar) for bar in self.completed})

    def _previous_session_bars(self) -> list[MinuteBar]:
        if self.session_date is None:
            return []
        prior = [value for value in self._sessions() if value < self.session_date]
        if not prior:
            return []
        previous_date = prior[-1]
        return [bar for bar in self.completed if self._bar_date(bar) == previous_date]

    def _opening_range(self, minutes: int, at: datetime) -> tuple[float, float, bool, datetime]:
        local = at.astimezone(MARKET_TZ[self.market])
        session_open, _ = session_bounds(self.market, local.date())
        boundary = session_open + timedelta(minutes=minutes)
        bars = [
            bar
            for bar in self.completed
            if self._bar_date(bar) == local.date() and bar.start < boundary
        ]
        if self.current and self.current.start < boundary:
            bars.append(self.current)
        expected_starts = {session_open + timedelta(minutes=offset) for offset in range(minutes)}
        observed_starts = {bar.start for bar in bars}
        ready = local >= boundary and expected_starts.issubset(observed_starts)
        if not bars:
            return 0.0, 0.0, False, boundary
        return (
            max(bar.high for bar in bars),
            min(bar.low for bar in bars),
            ready,
            boundary,
        )

    def _relative_volume(self, at: datetime) -> float | None:
        if self.session_date is None or self.session_volume <= 0:
            return None
        local = at.astimezone(MARKET_TZ[self.market])
        session_open, _ = session_bounds(self.market, self.session_date)
        elapsed = int((local - session_open).total_seconds() // 60)
        prior_dates = [value for value in self._sessions() if value < self.session_date]
        if len(prior_dates) < 20:
            return None
        totals = []
        for prior_date in prior_dates[-20:]:
            prior_open, _ = session_bounds(self.market, prior_date)
            cutoff = prior_open + timedelta(minutes=elapsed + 1)
            bars = [
                bar
                for bar in self.completed
                if self._bar_date(bar) == prior_date and bar.start < cutoff
            ]
            expected_starts = {
                prior_open + timedelta(minutes=offset) for offset in range(elapsed + 1)
            }
            if not expected_starts.issubset({bar.start for bar in bars}):
                return None
            total = sum(bar.volume for bar in bars)
            totals.append(total)
        average = sum(totals) / len(totals)
        return self.session_volume / average if average > 0 else None

    def snapshot(
        self, at: datetime
    ) -> tuple[dict[str, float | bool], dict[str, bool], dict[str, datetime]]:
        indicators: dict[str, float | bool] = {}
        ready: dict[str, bool] = {}
        timestamps: dict[str, datetime] = {}

        def add(name: str, value: Any, is_ready: bool, generated_at: datetime) -> None:
            indicators[name] = value if value is not None else 0.0
            ready[name] = bool(is_ready and value is not None)
            timestamps[name] = generated_at

        local = at.astimezone(MARKET_TZ[self.market])
        completed = self.completed
        closes = [bar.close for bar in completed]
        last_bar_at = completed[-1].end if completed else local
        add(
            "vwap_regular",
            self.session_notional / self.session_volume if self.session_volume else None,
            self.session_volume > 0,
            local,
        )
        add("ema_9_1m_regular", _ema(closes, 9), len(closes) >= 9, last_bar_at)
        add("ema_20_1m_regular", _ema(closes, 20), len(closes) >= 20, last_bar_at)
        add("ema_50_1m_regular", _ema(closes, 50), len(closes) >= 50, last_bar_at)
        add("rsi_14_1m_regular", _wilder_rsi(closes), len(closes) >= 15, last_bar_at)
        add("atr_14_1m_regular", _wilder_atr(completed), len(completed) >= 14, last_bar_at)

        recent = completed[-5:]
        add(
            "recent_high_5_1m_regular",
            max((bar.high for bar in recent), default=None),
            len(recent) == 5,
            last_bar_at,
        )
        add(
            "recent_low_5_1m_regular",
            min((bar.low for bar in recent), default=None),
            len(recent) == 5,
            last_bar_at,
        )
        add(
            "bar_volume_1m_regular",
            float(completed[-1].volume) if completed else None,
            bool(completed),
            last_bar_at,
        )
        for minutes in (5, 10, 15):
            high, low, range_ready, generated_at = self._opening_range(minutes, local)
            add(f"opening_range_{minutes}_high", high, range_ready, generated_at)
            add(f"opening_range_{minutes}_low", low, range_ready, generated_at)

        previous = self._previous_session_bars()
        previous_at = previous[-1].end if previous else local
        add("previous_open", previous[0].open if previous else None, bool(previous), previous_at)
        add(
            "previous_high",
            max((bar.high for bar in previous), default=None),
            bool(previous),
            previous_at,
        )
        add(
            "previous_low",
            min((bar.low for bar in previous), default=None),
            bool(previous),
            previous_at,
        )
        previous_close = previous[-1].close if previous else None
        add("previous_close", previous_close, bool(previous), previous_at)
        gap = None
        if previous_close and self.first_trade is not None:
            gap = (self.first_trade - previous_close) / previous_close * 100
        add("gap_pct", gap, gap is not None, local)
        relative_volume = self._relative_volume(local)
        add(
            "relative_volume_cumulative_20d_same_time_regular",
            relative_volume,
            relative_volume is not None,
            local,
        )
        return indicators, ready, timestamps
