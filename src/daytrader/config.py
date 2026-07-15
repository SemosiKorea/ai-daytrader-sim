from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8787
    database_path: Path = Path("data/daytrader.db")
    gpt_action_bearer: str = "change-me"
    admin_bearer: str = "change-admin"
    market_data_bearer: str = "change-feed"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_account_number: str | None = None
    kis_product_code: str = "01"
    kis_env: str = "paper"
    kis_poll_enabled: bool = False
    kis_poll_seconds: float = 1.0
    universe_path: Path = Path("config/universe.yaml")
    costs_path: Path = Path("config/costs.yaml")

    @model_validator(mode="after")
    def validate_bearer_secrets(self) -> "Settings":
        secrets = [self.gpt_action_bearer, self.admin_bearer, self.market_data_bearer]
        if any(
            len(value) < 24 or value.startswith(("change-", "replace-")) for value in secrets
        ):
            raise ValueError("all bearer secrets must be random values of at least 24 characters")
        if len(set(secrets)) != len(secrets):
            raise ValueError("bearer secrets must be different")
        return self


@dataclass(frozen=True)
class CostConfig:
    initial_cash: float
    commission_bps_each_side: float
    slippage_bps_each_side: float
    sell_tax_bps: float | None
    fx_bps_each_side: float
    trade_risk_pct: float = 0.4
    daily_loss_limit_pct: float = 2.0
    max_position_quantity: int = 1000
    fill_latency_ms: int = 300
    fill_timeout_sec: float = 5.0
    allow_partial_fill: bool = True
    max_fill_quantity_per_tick: int = 100
    volume_participation_pct: float = 10.0
    max_order_submissions_per_day: int = 4
    max_filled_entries_per_day: int = 2
    consecutive_stop_limit: int = 2
    stop_cooldown_minutes: int = 20
    halt_resume_cooldown_seconds: int = 60
    max_spread_pct: float = 1.0
    max_tick_age_seconds: float = 3.0
    indicator_max_age_seconds: float = 3.0

    def __post_init__(self) -> None:
        nonnegative = (
            self.commission_bps_each_side,
            self.slippage_bps_each_side,
            self.fx_bps_each_side,
            self.fill_latency_ms,
            self.fill_timeout_sec,
            self.volume_participation_pct,
            self.stop_cooldown_minutes,
            self.halt_resume_cooldown_seconds,
            self.max_spread_pct,
            self.max_tick_age_seconds,
            self.indicator_max_age_seconds,
        )
        if self.initial_cash <= 0 or any(value < 0 for value in nonnegative):
            raise ValueError("execution cost and timing configuration cannot be negative")
        if self.sell_tax_bps is not None and self.sell_tax_bps < 0:
            raise ValueError("sell_tax_bps cannot be negative")
        if not 0 < self.trade_risk_pct <= self.daily_loss_limit_pct <= 10:
            raise ValueError("risk percentages must satisfy 0 < trade <= daily <= 10")
        positive_integers = (
            self.max_position_quantity,
            self.max_fill_quantity_per_tick,
            self.max_order_submissions_per_day,
            self.max_filled_entries_per_day,
            self.consecutive_stop_limit,
        )
        if any(value <= 0 for value in positive_integers):
            raise ValueError("execution quantity and count limits must be positive")
        if not 0 < self.volume_participation_pct <= 100:
            raise ValueError("volume_participation_pct must be within (0, 100]")
        if (
            self.fill_timeout_sec <= 0
            or self.max_spread_pct <= 0
            or self.max_tick_age_seconds <= 0
            or self.indicator_max_age_seconds <= 0
        ):
            raise ValueError("timeouts, spread, and data-age limits must be positive")

    @property
    def session_ready(self) -> bool:
        return self.sell_tax_bps is not None


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_universe(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    raw = _read_yaml(path)
    return {market: {str(k): v for k, v in values.items()} for market, values in raw.items()}


def load_costs(path: Path) -> dict[str, CostConfig]:
    raw = _read_yaml(path)
    return {market: CostConfig(**values) for market, values in raw.items()}
