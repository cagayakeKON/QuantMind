"""Pinned market data in a real subprocess, without changing the parent provider."""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import duckdb
from qlib.data import D

from backend.services.engine.qlib_app.services.market_strategy_context import (
    MarketStrategyContext,
)
from backend.services.simulation.jp import backtest
from backend.services.simulation.jp.strategy_context import to_provider_instrument
from backend.services.simulation.jp.strategy_context import prepare_context
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.tests.test_dated_strategy_execution import make_runner, SESSIONS
from backend.services.engine.qlib_app.services.dated_strategy import DecisionQuote
from backend.services.engine.qlib_app.utils.recording_strategy import RedisLoggerMixin
from backend.services.simulation.jp.strategy_snapshot import executed_account_snapshot

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def context_with_reader(reader):
    context = object.__new__(MarketStrategyContext)
    context.provider = SimpleNamespace(features=reader)
    context.mapper = to_provider_instrument
    context.errors = []
    context.advance(date(2026, 9, 28), date(2026, 9, 29))
    return context


def test_completed_bar_scope_and_index_alias_remain_explicit():
    calls = []

    def read(instruments, fields, **kwargs):
        calls.append((instruments, fields, kwargs))
        return pd.DataFrame(
            {"$close": [2500]},
            index=pd.MultiIndex.from_tuples(
                [("jp_topix", pd.Timestamp("2026-09-28"))],
                names=["instrument", "datetime"],
            ),
        )

    context = context_with_reader(read)
    result = context.features(["TOPIX"], ["$close"], "2026-09-29", "2026-09-29")
    assert result.index.tolist() == [("TOPIX", pd.Timestamp("2026-09-28"))]
    assert calls[0][0] == ["jp_topix"]
    assert calls[0][2]["end_time"] == pd.Timestamp("2026-09-28")
    assert calls[0][2]["start_time"] == pd.Timestamp("2026-09-28")


@pytest.mark.parametrize("code", ["SH600036", "00700.HK", "us_AAPL"])
def test_foreign_market_reads_never_reach_provider(code):
    context = context_with_reader(lambda *a, **kw: pytest.fail("foreign read"))
    with pytest.raises(ValueError):
        context.features([code], ["$close"], "2026-09-28", "2026-09-28")
    with pytest.raises(ValueError, match="unavailable"):
        context.assert_reads_succeeded()


def test_future_session_query_cannot_return_future_bars():
    context = context_with_reader(lambda *a, **kw: pytest.fail("future read"))
    with pytest.raises(ValueError, match="known snapshot"):
        context.features(["jp_72030"], ["$close"], "2026-09-30", "2026-09-30")


def test_actual_fill_callback_uses_ledger_cash_and_stale_valuation(monkeypatch):
    def init(self, kwargs):
        self.backtest_id = kwargs.pop("backtest_id", None)
        self.redis_client = None

    monkeypatch.setattr(RedisLoggerMixin, "init_redis", init)
    runner = make_runner("standard_topk", topk=5)
    runner.strategy_context = context_with_reader(lambda *args, **kwargs: None)
    orders = runner.decide(
        step=0,
        signal_day=SESSIONS[0],
        scores={"jp_72030": 1},
        quotes={"jp_72030": DecisionQuote(100, 100)},
        cash=100000,
        positions={},
    )
    calls = []

    def callback(results):
        calls.append((results, runner.strategy.trade_position))

    monkeypatch.setattr(runner.strategy, "post_exe_step", callback)
    snapshot = executed_account_snapshot(
        {
            "cash_funds": [{"amount": "54900"}],
            "positions": {"JP72030": {"lots": [{"quantity": 900}], "last_price": "49"}},
        }
    )
    runner.record_fills(
        {id(orders[0]): {"quantity": 900, "price": "50", "fee": "100"}},
        post_snapshot=snapshot,
    )
    assert calls[0][0] == [(orders[0], 45000, 100, 50)]
    assert calls[0][1].get_cash() == 54900
    assert calls[0][1].get_stock_price("jp_72030") == 49
    assert calls[0][1].get_stock_count("jp_72030", "day") == 1


@pytest.mark.asyncio
async def test_public_expert_code_uses_real_isolated_raw_provider(
    model_data, monkeypatch, runtime_factory
):
    request, directory, meta = model_data
    request.strategy_type = "CustomStrategy"
    request.strategy_content = """
from qlib.data import D
def get_strategy_config():
    prices = D.features(['jp_216a0'], ['$close'], '2026-09-28', '2026-09-28')
    assert prices['$close'].iloc[-1] == 100, 'wrong market or adjusted price'
    expression = D.features(['jp_216a0'], ['Ref($close, 0)'], '2026-09-28', '2026-09-28')
    assert expression.iloc[-1, 0] == 100, 'known historical expression is unavailable'
    return {'class': 'RedisRecordingStrategy', 'kwargs': {
        'signal': '<PRED>', 'topk': 1, 'n_drop': 1, 'rebalance_days': 1}}
"""
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    service = runtime_factory(SimpleNamespace(save_run=save))
    result = await service.run_backtest(request)
    assert result.status == "completed", result.error_message
    assert result.config["strategy_decision_class"] == "RedisRecordingStrategy"
    assert result.config["strategy_data_version"] == meta["jp_data_version"]
    assert result.config["strategy_price_basis"] == "raw"
    assert result.trades[0]["quantity"] == 900
    assert [record["status"] for record in saved] == ["running", "completed"]
    assert D._provider is original_provider


def test_context_keeps_derived_cache_outside_immutable_publication(
    model_data, monkeypatch, tmp_path
):
    request, _, meta = model_data
    units = tmp_path / "dated_units.parquet"
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
    spec = prepare_context(request)
    root = tmp_path / "quantjp"
    assert spec.environment["QM_QUANTJP_DATA_DIR"] == str(root)
    assert spec.environment["QM_JP_TRADING_UNITS_FILE"] == str(units)
    assert spec.provider_uri == str(
        root / ".rd_cache" / meta["jp_data_version"] / "qlib_raw"
    )
    assert not (root / "versions" / meta["jp_data_version"] / ".rd_cache").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["StopLoss", "CustomStrategy"])
async def test_public_stop_loss_reads_completed_raw_bars_in_its_worker(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory, strategy
):
    request, directory, meta = model_data
    # Keep the real split on Sep 29; Sep 30 is an ordinary 10% decline for this
    # test, rather than the independent unresolved-rights fixture.
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
    root = tmp_path / "stop-loss-publication"
    version = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    meta = {**meta, "jp_data_version": version["version"]}
    request.strategy_type = strategy
    request.strategy_params.n_drop = 0
    if strategy == "CustomStrategy":
        request.strategy_content = """
STRATEGY_CONFIG = {
    'class': 'RedisStopLossStrategy',
    'module_path': 'backend.services.engine.qlib_app.utils.extended_strategies',
    'kwargs': {'signal': '<PRED>', 'topk': 5, 'n_drop': 1, 'hold_thresh': 10,
               'stop_loss': -0.08, 'take_profit': 0.15}}
"""
    request.end_date = "2026-09-30"
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP72030"],
            "trade_date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
            "pred": [0.8, 0.8],
            "split": ["test", "test"],
        }
    ).to_parquet(directory / "pred.parquet")
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    service = runtime_factory(SimpleNamespace(save_run=save))
    result = await service.run_backtest(request)
    assert result.status == "completed", result.error_message
    assert result.config["strategy_decision_class"] == "RedisStopLossStrategy"
    # The built-in preserves native TopK rotation. The custom hold threshold
    # prevents normal sales, so a leaked Sep 30 close (45) would uniquely trigger
    # the 8% stop; the completed Sep 29 close (50) equals the real purchase price.
    expected = ["BUY", "SELL"] if strategy == "StopLoss" else ["BUY"]
    assert [trade["side"] for trade in result.trades] == expected
    assert result.trades[0]["quantity"] == 900
    assert [record["status"] for record in saved] == ["running", "completed"]
    assert D._provider is original_provider


@pytest.mark.asyncio
async def test_expert_cannot_hide_a_forward_expression_read_failure(
    model_data, monkeypatch, runtime_factory
):
    request, directory, meta = model_data
    request.strategy_type = "CustomStrategy"
    request.strategy_content = """
from qlib.data import D
def get_strategy_config():
    try:
        D.features(['jp_72030'], ['Ref($close, -1)'], '2026-09-28', '2026-09-28')
    except Exception:
        pass
    return {'class': 'RedisRecordingStrategy', 'kwargs': {
        'signal': '<PRED>', 'topk': 5, 'n_drop': 1}}
"""
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    service = runtime_factory(SimpleNamespace(save_run=save))
    result = await service.run_backtest(request)
    assert result.status == "failed"
    assert "beyond the known snapshot" in result.error_message
    assert [record["status"] for record in saved] == ["running", "failed"]
    assert D._provider is original_provider


@pytest.mark.asyncio
async def test_worker_retains_common_failure_and_existing_code_permission(
    model_data, monkeypatch, runtime_factory
):
    request, directory, meta = model_data
    request.strategy_type = "CustomStrategy"
    request.strategy_content = "raise AssertionError('permission bypassed')"
    monkeypatch.setenv("ALLOW_CUSTOM_STRATEGY", "false")

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    service = runtime_factory(SimpleNamespace(save_run=save))
    result = await service.run_backtest(request)
    assert result.status == "failed"
    assert "Custom strategy execution is disabled" in result.error_message
    assert [record["status"] for record in saved] == ["running", "failed"]
