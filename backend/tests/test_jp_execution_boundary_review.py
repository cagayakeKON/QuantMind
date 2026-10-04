"""Independent execution-boundary regressions from the merged JP reviews."""

from datetime import date
import json

import pandas as pd
import pytest

from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.strategy_lab.test_worker import fake_redis as fake_redis_fixture

snapshot = source_fixture
fake_redis = fake_redis_fixture


@pytest.fixture
def lab_data(snapshot, tmp_path, monkeypatch):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.jp_features import build_jp_features
    from backend.tests.test_jp_features import fake_evaluator

    root = tmp_path / "standard-jp-lab"
    import_jquants_snapshot(snapshot, root)
    build_jp_features(root, evaluator=fake_evaluator)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root


def _setup(ctx):
    ctx.universe = "all"
    ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-30", 1000000
    ctx.benchmark, ctx.commission, ctx.slippage = "TOPIX", 0, 0
    ctx.tax_sell = ctx.transfer_fee = 0


@pytest.mark.asyncio
async def test_legacy_empty_signal_cycle_still_returns_without_financial_work(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from backend.services.simulation import engine as original

    @asynccontextmanager
    async def sessions():
        yield object()

    monkeypatch.setattr(original, "get_session", sessions)
    loader = SimpleNamespace(load_latest_signals=AsyncMock(return_value=[]))
    shared = original.SimulationEngine(
        redis=SimpleNamespace(client=object()),
        loader=loader,
        market_data=object(),
    )
    shared._ensure_redis = Mock(side_effect=AssertionError("account work"))
    shared._sync_snapshot = AsyncMock(side_effect=AssertionError("financial snapshot"))
    report = await shared.run_cycle("test", "00000007", "2")
    assert report.error == "无可用信号"
    assert report.signal_count == report.order_count == report.filled_count == 0
    assert report.account_snapshot == {}
    loader.load_latest_signals.assert_awaited_once()
    shared._ensure_redis.assert_not_called()
    shared._sync_snapshot.assert_not_awaited()


def test_standard_daily_callbacks_skip_retired_and_missing_day_but_history_is_asof(
    lab_data,
):
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    assert "JP13370" in provider.resolve_universe("all")
    history = provider.history(
        "JP13370", n=1, fields=["close"], today=pd.Timestamp("2026-09-29")
    )
    assert history.index[-1].date() == date(2026, 9, 28)
    callbacks = []
    ctx = Context()
    ctx.market = "JP"
    run_backtest(
        ctx=ctx,
        provider=provider,
        user_globals={
            "setup": _setup,
            "on_bar": lambda ctx, item: callbacks.append((item.symbol, item.date)),
        },
    )
    assert [(s, t.date()) for s, t in callbacks if s == "JP13370"] == [
        ("JP13370", date(2026, 9, 28))
    ]
    assert len([s for s, t in callbacks if s == "JP72030"]) == 3


def test_standard_callbacks_require_active_master_even_if_exact_price_exists(
    lab_data, monkeypatch
):
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    # A malformed/outdated master must not turn even an exact dated price into
    # an active instrument. Isolate this view without mutating a publication.
    stock_list = provider.hub.fetch_stock_list

    def dated_master(day):
        frame = stock_list(day)
        return frame[frame.symbol.ne("72030.JP")] if day == date(2026, 9, 29) else frame

    monkeypatch.setattr(provider.hub, "fetch_stock_list", dated_master)
    callbacks, snapshots = [], []
    ctx = Context()
    ctx.market = "JP"
    run_backtest(
        ctx=ctx,
        provider=provider,
        user_globals={
            "setup": _setup,
            "on_bar": lambda ctx, item: callbacks.append((item.symbol, item.date)),
            "on_universe": lambda ctx, today, frame: snapshots.append(
                (today.date(), set(frame.index))
            ),
        },
    )
    assert [(s, t.date()) for s, t in callbacks if s == "JP72030"] == [
        ("JP72030", date(2026, 9, 28)),
        ("JP72030", date(2026, 9, 30)),
    ]
    assert "JP72030" not in dict(snapshots)[date(2026, 9, 29)]


def test_legacy_lab_provider_retains_existing_asof_daily_callback_behavior():
    from backend.services.engine.strategy_lab.engine.data_provider import (
        InMemoryProvider,
    )
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    frame = pd.DataFrame(
        {"open": [10], "high": [10], "low": [10], "close": [10], "volume": [1000]},
        index=pd.to_datetime(["2026-09-28"]),
    )
    provider = InMemoryProvider(
        {
            "A": frame,
            "B": frame.reindex(
                pd.to_datetime(["2026-09-28", "2026-09-29"]), method="ffill"
            ),
        }
    )
    ctx, callbacks = Context(), []
    with pytest.raises(ValueError, match="not supported"):
        ctx.universe = "all"

    def setup(ctx):
        ctx.universe = ["A"]
        ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-29", 100000

    run_backtest(
        ctx=ctx,
        provider=provider,
        user_globals={
            "setup": setup,
            "on_bar": lambda ctx, item: callbacks.append(item.date.date()),
        },
    )
    assert callbacks == [date(2026, 9, 28), date(2026, 9, 29)]


def test_public_worker_supports_registered_jp_all_pool(lab_data, fake_redis):
    from backend.services.engine.strategy_lab.runner.worker import run_request

    code = """
def setup(ctx):
    ctx.universe = "all"
    ctx.start = "2026-09-28"
    ctx.end = "2026-09-30"
    ctx.cash = 1000000
    ctx.commission = 0
    ctx.slippage = 0

def on_bar(ctx, bar):
    ctx.log(bar.symbol)
"""
    assert (
        run_request(
            {
                "run_id": "native-pool-review",
                "code": code,
                "options": {"market": "JP"},
                "params": {},
            }
        )
        == 0
    )
    result = json.loads(fake_redis.kv["qm:lab:result:native-pool-review"])
    assert result["status"] == "success"
    assert result["config"]["universe"] == "all"
    assert result["config"]["market"] == "JP"
    assert result["config"]["currency"] == "JPY"
    assert len(result["equity"]) == 3


@pytest.mark.parametrize("freq", ["day", "5min", "30min"])
def test_standard_sdk_accepts_original_frequency_values(freq):
    from backend.services.engine.strategy_lab.sdk.context import Context

    ctx = Context()
    ctx.freq = freq
    assert ctx.freq == freq
    with pytest.raises(ValueError, match="freq must"):
        ctx.freq = "unsupported"
