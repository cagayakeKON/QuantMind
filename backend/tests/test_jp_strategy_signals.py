"""Explicit field and native signals use the common strategy contract in JP."""

from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from qlib.data import D

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.services.market_strategy_context import (
    StrategyContextSpec,
)
from backend.services.engine.qlib_app.services import (
    market_strategy_context as contexts,
)
from backend.services.engine.qlib_app.utils import simple_signal
from backend.services.simulation.jp import backtest, model_signals
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as public_runtime,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from qlib.backtest.signal import create_signal_from
from backend.tests.test_dated_strategy_execution import (
    native_exchange,
    order_rows,
    SESSIONS,
)
from backend.services.engine.qlib_app.services.dated_strategy import (
    DatedStrategyRunner,
    DecisionQuote,
)

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def second_session(request, snapshot, root, monkeypatch):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='',Vo=10000,Va=C*10000 WHERE Date='2026-09-30'"
        )
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request.end_date = "2026-09-30"
    request.strategy_params.rebalance_days = 1


async def field_run(request, monkeypatch, runtime_factory):
    async def no_model(*args):
        pytest.fail("Explicit feature signals must not resolve a registry model")

    monkeypatch.setattr(backtest, "resolve_model", no_model)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs["status"])

    provider = D._provider
    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert D._provider is provider
    assert saved == ["running", result.status]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metric, model", [("$close", None), ("close", "not-used-model")]
)
async def test_explicit_field_needs_no_model_and_uses_resolved_pool(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory, metric, model
):
    request, _, _ = model_data
    request.strategy_type = "TopkDropout"
    request.strategy_params.signal = metric
    request.allow_feature_signal_fallback = model is not None
    request.pool_id = "list:JP216A0"
    request.model_id = model
    second_session(request, snapshot, tmp_path / "field-interval", monkeypatch)
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "completed", result.error_message
    assert result.config["signal_source"] == "feature_field"
    assert result.config["signal_feature"] == "$close"
    assert result.config["training_labels_available_on"] is None
    assert result.config["training_data_version"] is None
    assert result.config["prediction_sha256"] is None
    assert result.config["strategy_data_version"] == result.data_version
    assert [
        (fill["symbol"], fill["quantity"], float(fill["price"]))
        for fill in result.trades
    ] == [("JP216A0", 1900, 45)]
    assert result.trades[0]["trade_date"] == "2026-09-30"


@pytest.mark.asyncio
async def test_unfiltered_field_signal_uses_dated_equity_universe(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory
):
    request, _, _ = model_data
    request.strategy_type = "TopkDropout"
    request.model_id = None
    request.strategy_params.signal = "$close"
    # The shared fixture also contains a retired stock whose execution-day
    # master is deliberately absent. Use a separate complete-universe snapshot
    # for this positive case, and keep the missing-master block below.
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("DELETE FROM research.master WHERE Code='13370'")
        conn.execute("DELETE FROM research.daily_prices WHERE Code='13370'")
    root = tmp_path / "complete-universe"
    second_session(request, snapshot, root, monkeypatch)
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "completed", result.error_message
    assert {fill["symbol"] for fill in result.trades} == {"JP216A0", "JP72030"}
    assert all(fill["quantity"] == 900 for fill in result.trades)


@pytest.mark.asyncio
async def test_native_signal_preserves_missing_execution_master_block(
    model_data, monkeypatch, runtime_factory
):
    request, _, _ = model_data
    request.strategy_type = "CustomStrategy"
    request.model_id = None
    request.strategy_params.signal = "$close"
    request.strategy_content = """
import pandas as pd
from qlib.backtest.signal import Signal
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
class OwnSignal(Signal):
    def get_signal(self, start_time=None, end_time=None):
        return pd.Series({'jp_13370': 1.0})
def get_strategy_instance():
    return TopkDropoutStrategy(signal=OwnSignal(), topk=5, n_drop=1)
"""
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "failed"
    assert "Missing dated JP master/units: JP13370" in result.error_message


CODE_HEADER = """
import pandas as pd
from qlib.data import D
from qlib.backtest.signal import Signal
from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy
class OwnSignal(Signal):
    def __deepcopy__(self, memo):
        raise AssertionError('Public native Signal identity must be preserved')
    def get_signal(self, start_time=None, end_time=None):
        frame = D.features(['jp_216a0'], ['$close'], start_time, end_time)
        assert frame.index.get_level_values('datetime').max() == pd.Timestamp('2026-09-28')
        assert frame['$close'].iloc[-1] == 100
        return pd.Series({'jp_216a0': frame['$close'].iloc[-1]})
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["object", "dataframe", "dict", "instance"])
@pytest.mark.parametrize("pool", ["list:JP216A0", "list:JP72030"])
async def test_native_signal_configs_and_instances_remain_native(
    model_data, monkeypatch, runtime_factory, kind, pool
):
    request, _, _ = model_data
    request.strategy_type = "CustomStrategy"
    request.model_id = None
    request.strategy_params.signal = "$close"
    request.pool_id = pool
    if kind == "instance":
        body = """
def get_strategy_instance():
    return RedisRecordingStrategy(signal=OwnSignal(), topk=5, n_drop=1)
"""
    else:
        signal = {
            "object": "OwnSignal()",
            "dataframe": "pd.DataFrame({'score': [2]}, index=pd.MultiIndex.from_tuples([(pd.Timestamp('2026-09-28'), 'jp_216a0')], names=['datetime', 'instrument']))",
            "dict": "{'class': 'SimpleSignal', 'module_path': 'backend.services.engine.qlib_app.utils.simple_signal', 'kwargs': {'metric': '$close', 'universe': 'all'}}",
        }[kind]
        body = f"""
def get_strategy_config():
    return {{'class': 'RedisRecordingStrategy', 'kwargs': {{'signal': {signal}, 'topk': 5, 'n_drop': 1}}}}
"""
    request.strategy_content = CODE_HEADER + body
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "completed", result.error_message
    expected = "JP216A0" if kind != "dict" else pool.removeprefix("list:")
    # The dictionary signal intentionally reads all stocks. The exchange still
    # quotes the resolved pool, so it cannot buy a stock outside that pool.
    if pool == "list:JP216A0" or kind == "dict":
        # Preserve native allocation across the explicit all-stock Signal's
        # candidate list; unquoted candidates do not become actual fills.
        # The prior-close quantities (300/900) execute across a 1:2 split.
        quantity = 600 if kind == "dict" else 1800
        assert [(fill["symbol"], fill["quantity"]) for fill in result.trades] == [
            (expected, quantity)
        ]
    else:
        assert result.total_trades == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["$missing_metric", "$net_profit_ttm"])
async def test_missing_field_is_not_silently_an_empty_success(
    model_data, monkeypatch, runtime_factory, metric
):
    request, _, _ = model_data
    request.strategy_type = "TopkDropout"
    request.model_id = None
    request.strategy_params.signal = metric
    request.pool_id = "list:JP216A0"
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "failed"
    assert metric.removeprefix("$") in result.error_message


@pytest.mark.asyncio
async def test_default_prediction_request_still_requires_registered_model(
    model_data, runtime_factory, monkeypatch
):
    request, _, _ = model_data
    request.strategy_type = "CustomStrategy"
    request.model_id = None
    request.strategy_content = "raise AssertionError('Factory must not run')"

    async def missing(**kwargs):
        return SimpleNamespace(
            fallback_used=False, effective_model_id=None, storage_path=""
        )

    monkeypatch.setattr(
        model_signals.model_registry_service, "resolve_effective_model", missing
    )

    async def save(**kwargs):
        pass

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "failed"
    assert "Select a registered JP model" in result.error_message


@pytest.mark.asyncio
async def test_unknown_field_swallowed_by_native_signal_still_fails(
    model_data, monkeypatch, runtime_factory
):
    request, _, _ = model_data
    request.strategy_type = "CustomStrategy"
    request.model_id = None
    request.strategy_params.signal = "$close"
    request.pool_id = "list:JP216A0"
    request.strategy_content = """
STRATEGY_CONFIG = {'class': 'RedisRecordingStrategy', 'kwargs': {
    'signal': {'class': 'SimpleSignal',
               'module_path': 'backend.services.engine.qlib_app.utils.simple_signal',
               'kwargs': {'metric': '$unavailable', 'universe': 'all'}},
    'topk': 5, 'n_drop': 1}}
"""
    result = await field_run(request, monkeypatch, runtime_factory)
    assert result.status == "failed"
    assert "unavailable" in result.error_message


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
    context = object.__new__(contexts.MarketStrategyContext)
    context.provider, context.mapper, context.errors = provider, lambda code: code, []
    context.spec = StrategyContextSpec(
        "unused", "cn", "fixture", "unused", feature_fields=("close",)
    )
    monkeypatch.setattr(simple_signal, "D", context)
    adapted = context.request_feature_signal("$close", request)
    context.advance("2026-09-28", "2026-09-29")
    prior = original.get_signal(pd.Timestamp("2026-09-28"), pd.Timestamp("2026-09-28"))
    assert prior is None or prior.empty
    assert adapted.get_signal("2026-09-28", "2026-09-28") is None
    context.advance("2026-09-29", "2026-09-30")
    public_daily = original.get_signal(
        pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-29")
    )
    # The public TopK accepts a one-column DataFrame or its first Series.
    pd.testing.assert_series_equal(
        adapted.get_signal("2026-09-29", "2026-09-29"), public_daily.iloc[:, 0]
    )


def test_all_stock_signal_sizing_matches_independent_native_exchange(monkeypatch):
    def runner():
        return DatedStrategyRunner(
            {
                "class": "TopkDropoutStrategy",
                "module_path": "qlib.contrib.strategy.signal_strategy",
                "kwargs": {"signal": "<PRED>", "topk": 5, "n_drop": 1},
            },
            SESSIONS,
            SESSIONS[1],
            SESSIONS[-1],
        )

    inputs = {
        "step": 0,
        "signal_day": SESSIONS[0],
        "scores": {
            "jp_216a0": 100,
            "jp_72030": 100,
            "jp_13370": 10,
        },
        "quotes": {"jp_216a0": DecisionQuote(100, 100)},
        "cash": 100000,
        "positions": {},
    }
    adapted, reference = runner(), runner()
    reference.exchange = native_exchange(monkeypatch, inputs["quotes"])
    reference.strategy.common_infra.reset_infra(trade_exchange=reference.exchange)
    actual = order_rows(adapted.decide(**inputs))
    assert actual == order_rows(reference.decide(**inputs))
    assert len(actual) == 1 and actual[0][0] == "jp_216a0" and actual[0][2] == 300


@pytest.mark.parametrize(
    "expression", ["$unknown", "Mean($unknown, 5)", "Ref($close, -1)"]
)
def test_declared_field_and_forward_expression_checks_precede_native_reads(
    expression, monkeypatch
):
    if expression == "Ref($close, -1)":
        # This unit test checks the window gate without registering a provider
        # in the parent. Existing real-worker tests cover Qlib expression parsing.
        from qlib.data import data

        monkeypatch.setattr(
            data,
            "ExpressionD",
            SimpleNamespace(
                get_expression_instance=lambda field: SimpleNamespace(
                    get_extended_window_size=lambda: (0, 1)
                )
            ),
        )
    context = object.__new__(contexts.MarketStrategyContext)
    context.spec = StrategyContextSpec(
        "unused", "cn", "fixture", "unused", feature_fields=("close",)
    )
    context.provider = SimpleNamespace(
        features=lambda *a, **kw: pytest.fail("Unregistered or future data read")
    )
    context.mapper = lambda code: code
    context.errors = []
    context.advance("2026-09-28", "2026-09-29")
    with pytest.raises(ValueError):
        context.features(["jp_216a0"], [expression], "2026-09-28", "2026-09-28")
    with pytest.raises(ValueError, match="unavailable"):
        context.assert_reads_succeeded()


@pytest.mark.asyncio
async def test_future_raw_changes_do_not_change_first_field_decision(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory
):
    request, _, _ = model_data
    request.strategy_type = "TopkDropout"
    request.model_id = None
    request.strategy_params.signal = "$close"
    request.pool_id = "list:JP216A0"
    second_session(
        request, snapshot, tmp_path / "initial-field-publication", monkeypatch
    )
    first = await field_run(request, monkeypatch, runtime_factory)
    assert first.status == "completed", first.error_message
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET H=47,C=46,Va=460000 WHERE Date>'2026-09-29'"
        )
    root = tmp_path / "future-field-publication"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request.jp_data_version = None
    changed = await field_run(request, monkeypatch, runtime_factory)
    assert changed.status == "completed", changed.error_message
    assert first.data_version != changed.data_version
    assert [(row["symbol"], row["quantity"], row["price"]) for row in first.trades] == [
        (row["symbol"], row["quantity"], row["price"]) for row in changed.trades
    ]
