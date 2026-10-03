"""Model portfolios and reports share the strict JP execution ledger."""

from datetime import date
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.backtest_service_runtime import (
    QlibBacktestServiceRuntimeMixin,
)
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as runtime,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.simulation.jp import backtest
from backend.services.simulation.jp.model_signals import model_registry_service
from backend.services.simulation.jp.model_portfolio import portfolio_orders
from backend.services.simulation.jp.rules import RuleDataMissing

pytest_plugins = ["backend.tests.test_jp_data_platform"]


@pytest.fixture
def runtime_factory(monkeypatch):
    notifications = []

    async def notify(**payload):
        notifications.append(payload)

    monkeypatch.setattr(runtime, "publish_notification_async", notify)

    def create(persistence):
        service = object.__new__(QlibBacktestService)
        service._runs = {}
        service._persistence = persistence
        service._initialized = True
        service._cache = None
        service.events = []
        service.notifications = notifications

        async def progress(*args, **kwargs):
            service.events.append(kwargs)

        service._notify_progress = progress
        return service

    return create


@pytest.fixture
def model_data(snapshot, tmp_path, monkeypatch):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-24','1'), ('2026-09-25','1'), ('2026-10-01','1'), ('2026-10-02','1')"
        )
    root = tmp_path / "quantjp"
    version = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    model = tmp_path / "model"
    model.mkdir()
    pd.DataFrame(
        {
            "symbol": ["JP72030"],
            "trade_date": pd.to_datetime(["2026-09-28"]),
            "pred": [0.8],
            "split": ["test"],
        }
    ).to_parquet(model / "pred.parquet")
    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "jp_data_version": version["version"],
        "context": {"market": "JP"},
        "train_end": "2026-09-24",
        "target_horizon_days": 1,
    }
    request = QlibBacktestRequest(
        market="JP",
        strategy_type="TopkDropout",
        model_id="jp-test",
        start_date="2026-09-29",
        end_date="2026-09-29",
        initial_capital=100000,
        strategy_params={"topk": 5},
        benchmark="TOPIX",
        risk_free_rate=0,
        jp_slippage_bps=0,
        strategy_total_position=0.95,
    )
    return request, model, meta


def test_real_raw_fills_dated_settlement_and_topix_report(model_data):
    request, model, meta = model_data
    result = backtest.run_cash_backtest(request, model, meta)
    assert result.market == "JP" and result.currency == "JPY"
    assert result.total_trades == 1
    fill = result.trades[0]
    assert fill["symbol"] == "JP72030" and fill["quantity"] == 900
    assert float(fill["price"]) == 50 and float(fill["fee"]) == 0
    assert fill["settlement_date"] == "2026-10-01"
    assert result.total_return == 0 and result.benchmark_return == 0
    assert result.config["execution_engine"] == "jp_cash_ledger"
    assert len(result.config["prediction_sha256"]) == 64
    assert [row["date"] for row in result.equity_curve] == ["2026-09-28", "2026-09-29"]


def test_standard_topk_strategy_uses_actual_public_strategy(model_data):
    request, model, meta = model_data
    request.strategy_type = "TopkDropout"
    result = backtest.run_cash_backtest(request, model, meta)
    assert result.config["strategy_type"] == "TopkDropout"
    assert result.total_trades == 1
    # The actual public strategy allocates available cash across its buy list.
    assert result.trades[0]["quantity"] == 900
    assert result.config["strategy_decision_class"] == "RedisRecordingStrategy"


def test_standard_topk_keeps_public_builder_parameter_rules(model_data):
    request, model, meta = model_data
    request.strategy_type = "TopkDropout"
    request.strategy_params.max_weight = 0.05
    result = backtest.run_cash_backtest(request, model, meta)
    # The public TopK builder does not consume the weight strategy's max_weight.
    assert result.total_trades == 1
    assert result.trades[0]["quantity"] == 900
    assert result.equity_curve[-1]["value"] == request.initial_capital


@pytest.mark.asyncio
async def test_research_jp_reads_dated_publication_without_cn_table(model_data):
    from backend.services.api.routers import research_service as research

    records = await research._load_sdl_day_map(None, date(2026, 9, 29), market="JP")
    assert records["JP72030"]["close"] == 50
    assert records["JP72030"]["currency"] == "JPY"
    assert all(code.startswith("JP") for code in records)
    with pytest.raises(ValueError, match="dated publication"):
        research._get_sdl_table("JP")
    assert research._get_sdl_table("CN") == "stock_daily_latest"
    assert research._get_sdl_table("HK") == "stock_daily_latest_hk"


def test_no_training_split_signals_or_cn_strategy_fallback(model_data):
    request, model, meta = model_data
    request.strategy_type = "CustomStrategy"
    with pytest.raises(ValueError, match="isolated market data-provider"):
        backtest.run_cash_backtest(request, model, meta)
    request.strategy_type = "TopkDropout"
    with pytest.raises(ValueError, match="availability"):
        backtest.run_cash_backtest(request, model, {**meta, "train_end": "2026-09-28"})
    frame = pd.read_parquet(model / "pred.parquet")
    frame["split"] = "train"
    frame.to_parquet(model / "pred.parquet")
    with pytest.raises(RuleDataMissing, match="signals are missing"):
        backtest.run_cash_backtest(request, model, meta)


def test_retired_private_strategy_cannot_execute_new_backtests(model_data):
    request, model, meta = model_data
    request.strategy_type = "jp_cash_topk"
    with pytest.raises(ValueError, match="shared strategy template"):
        backtest.run_cash_backtest(request, model, meta)


def test_prior_close_sizing_sells_first_without_using_future_open():
    state = {
        "cash_funds": [{"amount": "100000"}],
        "positions": {"JP67580": {"lots": [{"quantity": 100}]}},
    }
    bars = {
        "JP67580": {"close": 1000, "volume": 10000},
        "JP72030": {"close": 500, "volume": 10000, "open": 1},
    }
    master = {"JP72030": {}}
    orders = portfolio_orders(
        state,
        [{"symbol": "JP72030", "score": 1}],
        bars,
        master,
        date(2026, 9, 28),
        date(2026, 9, 29),
        topk=2,
        exposure=backtest.money("0.95"),
    )
    assert [(row["side"], row["symbol"], row["quantity"]) for row in orders] == [
        ("SELL", "JP67580", 100),
        ("BUY", "JP72030", 100),
    ]
    assert all(row["signal_date"] == "2026-09-28" for row in orders)


@pytest.mark.asyncio
async def test_dispatch_retains_existing_market_runtime(runtime_factory):
    def original_cleanup():
        raise RuntimeError("original runtime reached")

    service = runtime_factory("store")
    service._cleanup_stale_runs = original_cleanup
    for market in ("JP", "CN"):
        with pytest.raises(RuntimeError, match="original runtime reached"):
            await QlibBacktestServiceRuntimeMixin.run_backtest(
                service, QlibBacktestRequest(market=market)
            )


@pytest.mark.asyncio
async def test_failure_is_persisted_and_never_falls_back(monkeypatch, runtime_factory):
    async def resolve(**kwargs):
        return SimpleNamespace(fallback_used=True, effective_model_id="cn-default")

    monkeypatch.setattr(model_registry_service, "resolve_effective_model", resolve)
    saved = []

    async def save(*args, **kwargs):
        saved.append(kwargs["status"])

    service = runtime_factory(SimpleNamespace(save_run=save))
    result = await service.run_backtest(
        QlibBacktestRequest(market="JP", model_id="missing")
    )
    assert "unavailable" in result.error_message
    assert saved == ["running", "failed"]
    assert result.status == "failed" and result.currency == "JPY"


@pytest.mark.asyncio
async def test_jp_pending_saved_before_fast_worker_and_not_cached(monkeypatch):
    from backend.services.engine.qlib_app.api import backtest as api
    from backend.services.engine.qlib_app.services import (
        backtest_persistence as storage,
    )
    from backend.services.engine.qlib_app.services import (
        backtest_service_query as query,
    )
    from backend.services.engine.qlib_app import tasks
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult

    saved = []

    class Store:
        async def save_run(self, **kwargs):
            saved.append(kwargs["status"])

    def enqueue(**kwargs):
        assert saved == ["pending"]
        payload = kwargs["args"][0]
        assert "stamp_duty" not in payload and "min_commission" not in payload
        worker_request = QlibBacktestRequest(**payload)
        assert "commission" not in worker_request.model_fields_set
        saved.append("completed-by-worker")
        return SimpleNamespace(id="task-jp")

    monkeypatch.setattr(storage, "BacktestPersistence", Store)
    monkeypatch.setattr(tasks.run_backtest_async, "apply_async", enqueue)
    monkeypatch.setattr(
        api, "_identity_from_request", lambda *args, **kwargs: ("u", "default")
    )
    pending = await api.run_backtest(None, QlibBacktestRequest(market="JP"), None, True)
    assert pending.task_id == "task-jp" and saved == ["pending", "completed-by-worker"]

    complete = QlibBacktestResult(
        backtest_id=pending.backtest_id,
        market="JP",
        config={"market": "JP"},
        trades=[{"symbol": "JP216A0", "price": "100.5"}],
    )

    async def get_result(*args, **kwargs):
        return complete

    cache = SimpleNamespace(
        get_backtest_result=lambda _: {"config": {"market": "JP"}, "status": "pending"},
        set_backtest_result=lambda *args: None,
    )
    service = SimpleNamespace(
        _initialized=True,
        _cache=cache,
        _runs={},
        _persistence=SimpleNamespace(get_result=get_result),
        _normalize_result_trades=lambda value: value,
    )
    got = await query.QlibBacktestServiceQueryMixin.get_result(
        service, pending.backtest_id, "default", "u"
    )
    assert got.status == "completed" and got.trades[0]["price"] == "100.5"
