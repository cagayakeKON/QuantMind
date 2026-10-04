"""Public Japan custom and StopLoss strategies execute on the original Qlib path."""

import duckdb
import pandas as pd
import pytest
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import _resolve_quantjp_data_dir
from backend.tests.test_jp_standard_qlib_backtest import (
    ready_service,
    prepare_standard_predictions,
)

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


@pytest.mark.asyncio
async def test_public_expert_factory_reads_pinned_standard_qlib_features(
    model_data, runtime_factory, monkeypatch
):
    request, model, _ = model_data
    request.strategy_params.signal = str(model / "pred.parquet")
    prepare_standard_predictions(request, model)
    request.strategy_type = "CustomStrategy"
    request.strategy_content = """
from qlib.data import D
def get_strategy_config():
    prices=D.features(['jp_216a0'],['$close','$factor','Ref($close,0)'],'2026-09-28','2026-09-28')
    assert not prices.empty
    assert prices['$close'].iloc[-1] == prices['Ref($close,0)'].iloc[-1]
    assert prices['$factor'].iloc[-1] > 0
    return {'class':'RedisRecordingStrategy','module_path':'backend.services.engine.qlib_app.utils.recording_strategy','kwargs':{'signal':'<PRED>','topk':5,'n_drop':1}}
"""
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert result.trades and result.config["execution_engine"] == "qlib"
    assert [entry["status"] for entry in store.saved] == ["running", "completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["StopLoss", "CustomStrategy"])
async def test_public_stop_loss_keeps_prior_session_rule_on_standard_provider(
    model_data, snapshot, runtime_factory, monkeypatch, strategy
):
    request, model, _ = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
    request.jp_data_version = import_jquants_snapshot(
        snapshot, _resolve_quantjp_data_dir()
    )["version"]
    request.end_date = "2026-09-30"
    request.strategy_type = strategy
    request.strategy_params.signal = str(model / "pred.parquet")
    prepare_standard_predictions(request, model)
    if strategy == "CustomStrategy":
        request.strategy_content = "STRATEGY_CONFIG={'class':'RedisStopLossStrategy','module_path':'backend.services.engine.qlib_app.utils.extended_strategies','kwargs':{'signal':'<PRED>','topk':5,'n_drop':1,'hold_thresh':10,'stop_loss':-0.08,'take_profit':0.15}}"
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert result.trades
    assert [entry["status"] for entry in store.saved] == ["running", "completed"]
    if strategy == "CustomStrategy":
        assert [row["action"] for row in result.trades] == ["buy"]


@pytest.mark.asyncio
async def test_original_custom_code_permission_remains_enforced_for_jp(
    model_data, runtime_factory, monkeypatch
):
    request, model, _ = model_data
    request.strategy_params.signal = str(model / "pred.parquet")
    prepare_standard_predictions(request, model)
    request.strategy_type = "CustomStrategy"
    request.strategy_content = "raise AssertionError('permission bypassed')"
    monkeypatch.setenv("ALLOW_CUSTOM_STRATEGY", "false")
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert (
        result.status == "failed"
        and "Custom strategy execution is disabled" in result.error_message
    )
    assert [entry["status"] for entry in store.saved] == ["running", "failed"]
