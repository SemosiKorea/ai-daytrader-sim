from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from daytrader.config import Settings
from daytrader.models import Market, MarketTick
from daytrader.repository import Repository
from daytrader.scanner import CandidateScanner


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        universe_path=tmp_path / "universe.yaml",
        costs_path=tmp_path / "costs.yaml",
        gpt_action_bearer="gpt-secret-1234567890123456",
        admin_bearer="admin-secret-12345678901234",
        market_data_bearer="feed-secret-123456789012345",
    )


def _tick(at: datetime, session: str = "premarket") -> MarketTick:
    indicators = {
        "previous_close": 100.0,
        "premarket_gap_pct": 2.0,
        "premarket_volume": 250_000.0,
        "premarket_vwap": 101.5,
    }
    return MarketTick(
        market=Market.KR,
        symbol="005930",
        timestamp=at,
        source_timestamp=at,
        received_timestamp=at,
        sequence_id=1,
        connection_id="scanner-test",
        data_source="TEST",
        quote_scope="consolidated",
        session=session,
        market_status="open",
        symbol_status="trading",
        luld_status="normal",
        last=102.0,
        bid=101.95,
        ask=102.05,
        indicators=indicators,
        indicator_ready={name: True for name in indicators},
        indicator_timestamps={name: at for name in indicators},
    )


def test_premarket_scanner_returns_guard_template(tmp_path) -> None:
    settings = _settings(tmp_path)
    repository = Repository(settings.database_path)
    now = datetime(2026, 7, 16, 8, 45, tzinfo=ZoneInfo("Asia/Seoul"))
    tick = _tick(now)
    repository.save_market_snapshot(tick, now.date())
    scanner = CandidateScanner(
        repository,
        {
            "KR": {
                "005930": {
                    "name": "Samsung",
                    "exchange": "KRX",
                    "theme": "AI·반도체",
                }
            },
            "US": {},
        },
        settings,
    )

    result = scanner.scan(Market.KR, "premarket", now=now)

    assert result["status"] == "PREMARKET_SCAN_OPEN"
    assert result["candidates"][0]["symbol"] == "005930"
    assert result["candidates"][0]["theme"] == "AI·반도체"
    assert result["candidates"][0]["premarket_guard_template"]["reference_price"] == 102


def test_kr_candidate_selection_closes_at_0855(tmp_path) -> None:
    settings = _settings(tmp_path)
    scanner = CandidateScanner(
        Repository(settings.database_path),
        {"KR": {}, "US": {}},
        settings,
    )
    now = datetime(2026, 7, 16, 8, 56, tzinfo=ZoneInfo("Asia/Seoul"))

    result = scanner.scan(Market.KR, "auto", now=now)

    assert result["status"] == "PREOPEN_RECHECK"


def test_engine_records_nonregular_tick_without_calling_broker(tmp_path) -> None:
    settings = _settings(tmp_path)
    repository = Repository(settings.database_path)

    class BrokerStub:
        costs = {
            "KR": type(
                "Cost",
                (),
                {"max_tick_age_seconds": 3.0, "halt_resume_cooldown_seconds": 60},
            )()
        }

        def on_tick(self, tick):
            raise AssertionError("premarket tick reached execution adapter")

    from daytrader.engine import TradingEngine

    now = datetime.now(UTC)
    engine = TradingEngine(repository, BrokerStub())
    result = engine.process_tick(_tick(now))

    assert result["action"] == "SNAPSHOT_RECORDED"
    trade_date = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    assert repository.market_snapshots(Market.KR, trade_date, "premarket")
