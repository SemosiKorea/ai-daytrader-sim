from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from daytrader.allocation import WholeShareAllocator
from daytrader.broker import PaperBroker
from daytrader.config import CostConfig
from daytrader.experiment import PortfolioExperimentManager
from daytrader.market_clock import session_bounds
from daytrader.models import (
    CandidatePlan,
    ExperimentCohort,
    Market,
    MarketTick,
    PortfolioExperimentRequest,
)
from daytrader.repository import Repository


def _candidate(symbol: str, price: float) -> CandidatePlan:
    return CandidatePlan.model_validate(
        {
            "symbol": symbol,
            "exchange": "NASDAQ",
            "reason": "Whole-share comparison experiment candidate.",
            "entry": {
                "trigger_price": price,
                "limit_price": price,
                "start_time": "09:40:00",
                "end_time": "11:00:00",
                "price_only": True,
                "rules": {"mode": "all", "predicates": [], "groups": []},
            },
            "stop_loss": {"price": price * 0.99},
            "take_profit": [{"price": price * 1.02, "quantity_pct": 100}],
            "force_exit_time": "15:50:00",
        }
    )


def _tick(symbol: str, at: datetime, price: float) -> MarketTick:
    return MarketTick(
        market=Market.US,
        symbol=symbol,
        timestamp=at,
        source_timestamp=at,
        received_timestamp=at,
        sequence_id=int(at.timestamp() * 1000),
        connection_id="experiment-test",
        data_source="TEST",
        quote_scope="consolidated",
        session="regular",
        market_status="open",
        symbol_status="trading",
        luld_status="normal",
        last=price,
        bid=price - 0.01,
        ask=price,
        ask_size=100,
        trade_size=100,
    )


def test_whole_share_allocator_spends_residual_without_fractional_shares() -> None:
    allocator = WholeShareAllocator()
    costs = CostConfig(100, 0, 0, 0, 0)
    candidates = [_candidate("AAA", 60), _candidate("BBB", 40)]

    all_equal = allocator.allocate(
        100,
        candidates,
        {"AAA": 0.5, "BBB": 0.5},
        costs,
        redistribute_residual=True,
    )
    fixed = allocator.allocate(
        100,
        [_candidate("AAA", 60)],
        {"AAA": 0.5},
        costs,
        redistribute_residual=False,
    )

    assert all_equal.quantities == {"AAA": 1, "BBB": 1}
    assert all_equal.estimated_cash == 0
    assert fixed.quantities == {"AAA": 0}
    assert fixed.estimated_cash == 100


def test_experiment_broker_allows_multiple_fixed_quantity_positions(tmp_path) -> None:
    repository = Repository(tmp_path / "cohort.db")
    costs = CostConfig(
        1_000,
        0,
        0,
        0,
        0,
        max_order_submissions_per_day=3,
        max_filled_entries_per_day=3,
    )
    broker = PaperBroker(
        repository,
        {"US": costs},
        max_concurrent_positions=2,
        fixed_quantities={(Market.US, "AAA"): 3, (Market.US, "BBB"): 4},
        enforce_portfolio_gates=False,
    )
    at = datetime.now(UTC)

    first, first_reason = broker.submit_entry(
        "experiment_AAA", _candidate("AAA", 60), _tick("AAA", at, 60)
    )
    second, second_reason = broker.submit_entry(
        "experiment_BBB", _candidate("BBB", 40), _tick("BBB", at, 40)
    )
    third, third_reason = broker.submit_entry(
        "experiment_CCC", _candidate("CCC", 20), _tick("CCC", at, 20)
    )

    assert first and first.desired_quantity == 3 and first_reason is None
    assert second and second.desired_quantity == 4 and second_reason is None
    assert third is None and third_reason == "POSITION_SLOT_OCCUPIED"


def test_manager_builds_three_isolated_allocations_and_restores(tmp_path) -> None:
    repository = Repository(tmp_path / "main.db")
    now = datetime.now(UTC)
    request = PortfolioExperimentRequest(
        experiment_id="US_compare_restore",
        created_at=now,
        market=Market.US,
        trade_date=now.date(),
        expires_at=now + timedelta(days=1),
        approval_nonce="123456",
        candidates=[_candidate("AAA", 60), _candidate("BBB", 40)],
        user_selected_symbols=["AAA"],
    )
    nonce, _ = repository.issue_nonce(Market.US, now.date())
    request.approval_nonce = nonce
    assert repository.approve_portfolio_experiment(request)
    costs = {"US": CostConfig(100, 0, 0, 0, 0)}
    manager = PortfolioExperimentManager(repository, costs, tmp_path / "experiments")

    runtime = manager.runtimes[request.experiment_id]
    assert runtime.cohorts[
        ExperimentCohort.GPT_ALL_EQUAL
    ].allocation.quantities == {"AAA": 1, "BBB": 1}
    assert runtime.cohorts[
        ExperimentCohort.USER_FIXED_SLEEVE
    ].allocation.quantities == {"AAA": 0}
    assert runtime.cohorts[
        ExperimentCohort.USER_REALLOCATED
    ].allocation.quantities == {"AAA": 1}
    assert all(
        not cohort.repository.recent_kis_order_intents()
        for cohort in runtime.cohorts.values()
    )

    changed_costs = {"US": CostConfig(1_000, 10, 10, 0, 5)}
    restored = PortfolioExperimentManager(
        repository, changed_costs, tmp_path / "experiments"
    )
    assert request.experiment_id in restored.runtimes
    view = restored.view(request.experiment_id)
    assert view and set(view["cohorts"]) == {cohort.value for cohort in ExperimentCohort}
    assert "comparison" in view
    assert restored.runtimes[request.experiment_id].cohorts[
        ExperimentCohort.GPT_ALL_EQUAL
    ].allocation.quantities == {"AAA": 1, "BBB": 1}
    assert restored.runtimes[request.experiment_id].cohorts[
        ExperimentCohort.GPT_ALL_EQUAL
    ].broker.portfolios[Market.US].initial_cash == 100


def test_identical_ticks_fill_all_and_selected_cohorts_independently(
    tmp_path, monkeypatch
) -> None:
    repository = Repository(tmp_path / "main.db")
    now = datetime.now(UTC)
    local_date = now.astimezone(ZoneInfo("America/New_York")).date()
    candidates = [_candidate("AAA", 60), _candidate("BBB", 40)]
    for candidate in candidates:
        candidate.entry.start_time = datetime.min.time()
        candidate.entry.end_time = datetime.max.time().replace(microsecond=0)
    request = PortfolioExperimentRequest(
        experiment_id="US_compare_ticks",
        created_at=now,
        market=Market.US,
        trade_date=local_date,
        expires_at=now + timedelta(days=1),
        approval_nonce="123456",
        candidates=candidates,
        user_selected_symbols=["AAA"],
    )
    nonce, _ = repository.issue_nonce(Market.US, local_date)
    request.approval_nonce = nonce
    assert repository.approve_portfolio_experiment(request)
    costs = {
        "US": CostConfig(
            1_000,
            0,
            0,
            0,
            0,
            fill_latency_ms=300,
            max_fill_quantity_per_tick=100,
        )
    }
    manager = PortfolioExperimentManager(repository, costs, tmp_path / "experiments")
    monkeypatch.setattr(
        "daytrader.engine.force_exit_at",
        lambda market, trade_date: now.astimezone(ZoneInfo("America/New_York"))
        + timedelta(hours=1),
    )

    manager.process_tick(_tick("AAA", now, 60))
    manager.process_tick(_tick("BBB", now + timedelta(milliseconds=1), 40))
    manager.process_tick(_tick("AAA", now + timedelta(milliseconds=400), 60))
    manager.process_tick(_tick("BBB", now + timedelta(milliseconds=401), 40))

    runtime = manager.runtimes[request.experiment_id]
    all_positions = runtime.cohorts[
        ExperimentCohort.GPT_ALL_EQUAL
    ].broker.portfolios[Market.US].positions
    fixed_positions = runtime.cohorts[
        ExperimentCohort.USER_FIXED_SLEEVE
    ].broker.portfolios[Market.US].positions
    reallocated_positions = runtime.cohorts[
        ExperimentCohort.USER_REALLOCATED
    ].broker.portfolios[Market.US].positions
    assert set(all_positions) == {"AAA", "BBB"}
    assert set(fixed_positions) == {"AAA"}
    assert set(reallocated_positions) == {"AAA"}
    assert fixed_positions["AAA"].quantity == 8
    assert reallocated_positions["AAA"].quantity == 16


def test_expired_experiment_settles_with_persisted_last_quote(
    tmp_path, monkeypatch
) -> None:
    repository = Repository(tmp_path / "main.db")
    now = datetime.now(UTC)
    local_date = now.astimezone(ZoneInfo("America/New_York")).date()
    candidate = _candidate("AAA", 60)
    candidate.entry.start_time = datetime.min.time()
    candidate.entry.end_time = datetime.max.time().replace(microsecond=0)
    request = PortfolioExperimentRequest(
        experiment_id="US_expiry_recovery",
        created_at=now,
        market=Market.US,
        trade_date=local_date,
        expires_at=now + timedelta(days=1),
        approval_nonce="123456",
        candidates=[candidate],
        user_selected_symbols=["AAA"],
    )
    nonce, _ = repository.issue_nonce(Market.US, local_date)
    request.approval_nonce = nonce
    assert repository.approve_portfolio_experiment(request)
    manager = PortfolioExperimentManager(
        repository,
        {"US": CostConfig(1_000, 0, 0, 0, 0)},
        tmp_path / "experiments",
    )
    monkeypatch.setattr(
        "daytrader.engine.force_exit_at",
        lambda market, trade_date: now.astimezone(ZoneInfo("America/New_York"))
        + timedelta(hours=1),
    )
    trigger = _tick("AAA", now, 60)
    fill = _tick("AAA", now + timedelta(milliseconds=400), 60)
    repository.save_market_snapshot(trigger, local_date)
    repository.save_market_snapshot(fill, local_date)
    manager.process_tick(trigger)
    manager.process_tick(fill)
    runtime = manager.runtimes[request.experiment_id]
    assert all(
        cohort.broker.portfolios[Market.US].positions
        for cohort in runtime.cohorts.values()
    )

    _, close_at = session_bounds(Market.US, local_date)
    runtime.request.expires_at = close_at - timedelta(seconds=1)

    class AfterCloseDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = close_at + timedelta(seconds=1)
            return value.astimezone(tz) if tz else value

    monkeypatch.setattr("daytrader.experiment.datetime", AfterCloseDateTime)
    manager.maintenance()

    assert repository.get_portfolio_experiment(request.experiment_id)["status"] == "COMPLETED"
    assert request.experiment_id not in manager.runtimes
    assert all(
        not cohort.broker.portfolios[Market.US].positions
        for cohort in runtime.cohorts.values()
    )
