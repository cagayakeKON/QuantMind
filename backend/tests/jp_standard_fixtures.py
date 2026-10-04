"""Temporary JP data and original Qlib service fixtures, with no cash runner."""

from types import SimpleNamespace
import duckdb
import pandas as pd
import pytest
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as runtime,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)

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
        # This fixture verifies full target sizing across a 1:2 split; keep
        # sufficient observed volume for the converted execution-share order.
        conn.execute("UPDATE research.daily_prices SET Vo=10000,Va=C*10000")
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
        commission=0,
        min_commission=0,
        impact_cost_coefficient=0,
        strategy_total_position=0.95,
    )
    return request, model, meta


class RecordingRedis:
    def __init__(self):
        self.values = {}
        self.fail_publish = False

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def eval(self, script, count, *args):
        keys, values = args[:count], args[count:]
        if count == 1:
            if self.get(keys[0]) != values[0]:
                return 0
            self.values.pop(keys[0])
            return 1
        if self.fail_publish:
            raise RuntimeError("controlled Redis publication failure")
        if (self.get(keys[0]) is not None) != (values[1] == "1") or (
            values[1] == "1" and self.get(keys[0]) != values[0]
        ):
            return 0
        if any(self.get(k) != v for k, v in zip(keys[2:], values[4:], strict=True)):
            return 0
        self.values.update(zip(keys[:2], values[2:4], strict=True))
        self.values.update(dict.fromkeys(keys[2:], "completed"))
        return 1


import pytest_asyncio


@pytest_asyncio.fixture
async def standard_report(model_data, runtime_factory, monkeypatch):
    from backend.tests.test_jp_standard_qlib_backtest import (
        ready_service,
        prepare_standard_predictions,
    )
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

    request, model, _ = model_data
    request.end_date = "2026-09-30"
    request.strategy_params.signal = str(model / "pred.parquet")
    request.commission = 0.001
    prepare_standard_predictions(request, model)
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    captured = {}
    original = RiskAnalyzer.analyze

    async def analyze(**kwargs):
        captured.update(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(RiskAnalyzer, "analyze", analyze)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    return result, request, captured["portfolio_dict"]
