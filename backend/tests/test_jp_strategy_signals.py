"""JP explicit features and native signals use the original Qlib interfaces."""

from types import SimpleNamespace
import pandas as pd
import pytest
from qlib.backtest.signal import create_signal_from
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as public_runtime,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.utils import simple_signal
from backend.tests.test_jp_standard_qlib_backtest import (
    ready_service,
    prepare_standard_predictions,
)

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def test_optional_universe_callback_keeps_public_signal_algorithm(monkeypatch):
    dates = pd.date_range("2026-09-24", periods=5)
    frame = pd.DataFrame(
        {"$close": [10, 11, 12, 13, 14]},
        index=pd.MultiIndex.from_product(
            [["jp_216a0"], dates], names=["instrument", "datetime"]
        ),
    )
    monkeypatch.setattr(
        simple_signal,
        "D",
        SimpleNamespace(
            instruments=lambda market: market,
            list_instruments=lambda *args, **kwargs: ["jp_216a0"],
            features=lambda *args, **kwargs: frame,
        ),
    )
    for lag in (0, 1, 2, 3):
        default = simple_signal.SimpleSignal(signal_lag_days=lag)
        injected = simple_signal.SimpleSignal(
            signal_lag_days=lag, instrument_provider=lambda: ["jp_216a0"]
        )
        pd.testing.assert_series_equal(
            default.get_signal(dates[4], dates[4]),
            injected.get_signal(dates[4], dates[4]),
        )


@pytest.mark.asyncio
async def test_feature_interval_matches_actual_public_request_builder(monkeypatch):
    dates = pd.date_range("2026-09-29", periods=2)
    frame = pd.DataFrame(
        {"$close": [50.0, 45.0]},
        index=pd.MultiIndex.from_product(
            [["sh600036"], dates], names=["instrument", "datetime"]
        ),
    )
    calls = []

    def read(instruments, fields, **kwargs):
        calls.append(kwargs)
        times = frame.index.get_level_values("datetime")
        return frame[
            (times >= pd.Timestamp(kwargs["start_time"]))
            & (times <= pd.Timestamp(kwargs["end_time"]))
        ]

    provider = SimpleNamespace(
        instruments=lambda market: market,
        list_instruments=lambda *a, **kw: ["sh600036"],
        features=read,
    )
    monkeypatch.setattr(public_runtime, "D", provider)
    request = QlibBacktestRequest(
        start_date="2026-09-29",
        end_date="2026-09-30",
        strategy_params={"signal": "$close"},
    )
    service = object.__new__(QlibBacktestService)

    async def forbidden(*args):
        pytest.fail("The public explicit-field rule does not resolve a model")

    service._resolve_pred_path_from_model_registry = forbidden
    data, metadata = await service._build_signal_data(request)
    assert metadata["source"] == "feature_field"
    assert calls[0]["start_time"] == request.start_date
    original = create_signal_from(data)
    actual = original.get_signal(
        pd.Timestamp(request.start_date), pd.Timestamp(request.start_date)
    )
    assert actual is not None and not actual.empty
    assert actual.iloc[0, 0] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["field", "object", "dict", "instance"])
async def test_jp_public_native_signal_runs_actual_qlib(
    model_data, runtime_factory, monkeypatch, kind
):
    request, model, _ = model_data
    request.model_id = None
    request.start_date = "2026-09-28"
    request.end_date = "2026-09-30"
    request.strategy_params.signal = "$close"
    request.strategy_params.rebalance_days = 1
    service, store, _ = ready_service(runtime_factory, monkeypatch)

    async def forbidden(*args, **kwargs):
        pytest.fail("Explicit field signal must not resolve a model")

    service._resolve_pred_path_from_model_registry = forbidden
    if kind != "field":
        request.strategy_type = "CustomStrategy"
        signal = (
            "OwnSignal()"
            if kind == "object"
            else "{'class':'SimpleSignal','module_path':'backend.services.engine.qlib_app.utils.simple_signal','kwargs':{'metric':'$close','universe':'all'}}"
        )
        request.strategy_content = (
            """
import pandas as pd
from qlib.backtest.signal import Signal
class OwnSignal(Signal):
    def get_signal(self,start_time=None,end_time=None):
        return pd.Series({'jp_72030':0.9,'jp_216a0':0.8})
def get_strategy_config():
    return {'class':'RedisRecordingStrategy','module_path':'backend.services.engine.qlib_app.utils.recording_strategy','kwargs':{'signal':%s,'topk':5,'n_drop':1}}
"""
            % signal
        )
    if kind == "instance":
        request.strategy_content = """
import pandas as pd
from qlib.backtest.signal import Signal
from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy
class OwnSignal(Signal):
    def get_signal(self,start_time=None,end_time=None):
        return pd.Series({'jp_72030':0.9,'jp_216a0':0.8})
def get_strategy_instance():
    return RedisRecordingStrategy(signal=OwnSignal(),topk=5,n_drop=1)
"""
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert result.trades
    assert {row["symbol"] for row in result.trades}.issubset(
        {"jp_72030", "jp_216a0", "jp_13370"}
    )
    assert result.config["signal_meta"]["source"] == "feature_field"
    assert [row["status"] for row in store.saved] == ["running", "completed"]
