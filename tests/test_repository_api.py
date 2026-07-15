from __future__ import annotations

from fastapi.testclient import TestClient

from daytrader.api import create_app
from daytrader.config import Settings


def _settings(tmp_path) -> Settings:
    universe = tmp_path / "universe.yaml"
    universe.write_text(
        'KR:\n  "005930": {name: Samsung, exchange: KRX}\nUS: {}\n', encoding="utf-8"
    )
    costs = tmp_path / "costs.yaml"
    costs.write_text(
        "KR:\n  initial_cash: 3000000\n  commission_bps_each_side: 0\n"
        "  slippage_bps_each_side: 0\n  sell_tax_bps: 0\n  fx_bps_each_side: 0\n"
        "US:\n  initial_cash: 1500\n  commission_bps_each_side: 0\n"
        "  slippage_bps_each_side: 0\n  sell_tax_bps: 0\n  fx_bps_each_side: 0\n",
        encoding="utf-8",
    )
    return Settings(
        database_path=tmp_path / "test.db",
        universe_path=universe,
        costs_path=costs,
        gpt_action_bearer="gpt-secret-1234567890123456",
        admin_bearer="admin-secret-12345678901234",
        market_data_bearer="feed-secret-123456789012345",
    )


def test_approval_code_is_one_time_and_dashboard_is_private(tmp_path, kr_plan_dict: dict) -> None:
    app = create_app(_settings(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        nonce_response = client.post(
            "/v1/admin/nonces",
            headers={"Authorization": "Bearer admin-secret-12345678901234"},
            json={"market": "KR", "trade_date": kr_plan_dict["trade_date"]},
        )
        assert nonce_response.status_code == 200
        kr_plan_dict["approval_nonce"] = nonce_response.json()["nonce"]
        response = client.post(
            "/v1/gpt-actions/plans",
            headers={"Authorization": "Bearer gpt-secret-1234567890123456"},
            json=kr_plan_dict,
        )
        assert response.status_code == 201
        assert response.json()["status"] == "ARMED"

        reused = client.post(
            "/v1/gpt-actions/plans",
            headers={"Authorization": "Bearer gpt-secret-1234567890123456"},
            json={**kr_plan_dict, "plan_id": f"{kr_plan_dict['plan_id']}_new"},
        )
        assert reused.status_code == 409

        stored = app.state.repository.get_plan(kr_plan_dict["plan_id"])
        assert "123456" not in stored["payload"]
        assert kr_plan_dict["approval_nonce"] not in stored["payload"]

        next_nonce = client.post(
            "/v1/admin/nonces",
            headers={"Authorization": "Bearer admin-secret-12345678901234"},
            json={"market": "KR", "trade_date": kr_plan_dict["trade_date"]},
        ).json()["nonce"]
        revised = {
            **kr_plan_dict,
            "plan_id": f"{kr_plan_dict['plan_id']}_v2",
            "plan_version": 2,
            "approval_nonce": next_nonce,
        }
        replaced = client.post(
            "/v1/gpt-actions/plans",
            headers={"Authorization": "Bearer gpt-secret-1234567890123456"},
            json=revised,
        )
        assert replaced.status_code == 201
        assert app.state.repository.get_plan(kr_plan_dict["plan_id"])["status"] == "CANCELLED"


def test_payload_and_auth_guards(tmp_path) -> None:
    app = create_app(_settings(tmp_path), start_scheduler=False)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.json()["kis_order_mode"] == "record_only"
        assert health.json()["live_orders"] is False
        assert client.get("/v1/performance/KR").status_code == 401
        assert client.get("/v1/admin/kis-order-intents").status_code == 401
        intents = client.get(
            "/v1/admin/kis-order-intents",
            headers={"Authorization": "Bearer admin-secret-12345678901234"},
        )
        assert intents.status_code == 200
        assert intents.json() == []
        response = client.post(
            "/v1/market-data/ticks",
            headers={"Content-Length": "65537"},
            content=b"{}",
        )
        assert response.status_code == 413
