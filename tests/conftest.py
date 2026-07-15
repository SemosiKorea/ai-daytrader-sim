from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daytrader.market_clock import force_exit_at, is_session
from daytrader.models import Market, TradePlan


@pytest.fixture
def kr_plan_dict() -> dict:
    timezone = ZoneInfo("Asia/Seoul")
    trade_date = datetime.now(timezone).date() + timedelta(days=1)
    while not is_session(Market.KR, trade_date):
        trade_date += timedelta(days=1)
    expires_at = force_exit_at(Market.KR, trade_date)
    return {
        "plan_id": f"KR_{trade_date:%Y%m%d}_test",
        "market": "KR",
        "trade_date": trade_date.isoformat(),
        "expires_at": expires_at.isoformat(),
        "approval_nonce": "123456",
        "approved_symbols": [
            {
                "symbol": "005930",
                "exchange": "KRX",
                "reason": "A deterministic schema validation test candidate.",
                "entry": {
                    "trigger_price": 100.0,
                    "limit_price": 100.1,
                    "start_time": "09:10:00",
                    "end_time": "11:00:00",
                    "price_only": True,
                    "rules": {"mode": "all", "predicates": [], "groups": []},
                },
                "stop_loss": {"price": 99.0},
                "take_profit": [
                    {"price": 103.0, "quantity_pct": 50},
                    {"price": 104.0, "quantity_pct": 50},
                ],
                "force_exit_time": expires_at.time().isoformat(),
            }
        ],
    }


@pytest.fixture
def kr_plan(kr_plan_dict: dict) -> TradePlan:
    return TradePlan.model_validate(kr_plan_dict)
