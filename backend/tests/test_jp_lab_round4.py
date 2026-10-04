"""Published JP Lab inputs use one raw cash basis and one pinned run context."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.tests.test_jp_share_basis_review import (
    native as native_fixture,
    snapshot as snapshot_fixture,
    publish,
    DAYS,
)

native = native_fixture
snapshot = snapshot_fixture
from backend.services.engine.strategy_lab.engine.loop import run_backtest
from backend.services.engine.strategy_lab.runner.worker import (
    _resolve_provider,
    _build_user_globals,
)
from backend.services.engine.strategy_lab.sdk.context import Context


@pytest.fixture
def provider(native):
    source, _ = native
    with duckdb.connect(str(source)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=100,C=100,H=105,L=95,AdjFactor=1,ExRT='',Vo=100000"
        )
    publish(native)
    return _resolve_provider({"options": {"market": "JP"}}, None)


@pytest.mark.parametrize("slip", [0, 0.0005])
def test_full_weight_reserves_known_commission_slippage_and_fills(provider, slip):
    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = str(DAYS[0]), str(DAYS[2]), 100000
        ctx.commission, ctx.slippage = 0.0003, slip

    def on_bar(ctx, bar):
        if bar.date.date() == DAYS[0]:
            ctx.set_position(bar.symbol, weight=1)

    result = run_backtest(
        ctx=Context(),
        provider=provider,
        user_globals={"setup": setup, "on_bar": on_bar},
    )
    assert [(trade.qty, trade.direction) for trade in result.trades] == [(900, "BUY")]
    assert result.config["cash"] == 100000
    assert (
        result.config["market"] == "JP"
        and result.config["data_version"] == provider.reader.data_version
    )
    assert not any("insufficient" in row["msg"].lower() for row in result.logs)


def test_position_holding_days_counts_sessions_not_calendar_days(native):
    from backend.services.engine.strategy_lab.engine.dated_broker import DatedLabBroker
    from backend.services.engine.strategy_lab.engine.local_provider import (
        bind_registered_context,
    )

    source, _ = native
    with duckdb.connect(str(source)) as conn:
        conn.execute("DELETE FROM research.calendar WHERE Date='2026-09-29'")
        conn.execute("DELETE FROM research.daily_prices WHERE Date='2026-09-29'")
    publish(native)
    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    ctx = Context()
    bind_registered_context(ctx, provider)
    ctx.commission = ctx.slippage = 0
    broker = DatedLabBroker(ctx, provider, 100000)
    broker.executor.execute_day(
        DAYS[0],
        [
            {
                "symbol": "JP72030",
                "quantity": 100,
                "side": "BUY",
                "signal_date": "2026-09-25",
                "order_id": "first",
            }
        ],
    )
    broker.prepare_day(pd.Timestamp(DAYS[2]))
    assert ctx.position("JP72030").holding_days == 1


def test_raw_event_and_history_match_position_cost_qfq_is_explicit(
    provider, monkeypatch
):
    original = provider.hub.fetch_daily_kline

    def prices(symbol, start, end, *, adjust):
        frame = original(symbol, start, end, adjust="raw")
        if adjust == "qfq":
            for field in ("open", "high", "low", "close"):
                frame[field] /= 5
        return frame

    monkeypatch.setattr(provider.hub, "fetch_daily_kline", prices)
    seen = []

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = str(DAYS[0]), str(DAYS[2]), 100000
        ctx.commission = ctx.slippage = 0

    def on_bar(ctx, bar):
        if bar.date.date() == DAYS[0]:
            ctx.buy(bar.symbol, qty=100)
        elif ctx.position(bar.symbol).qty:
            seen.append(
                (
                    bar.close,
                    ctx.position(bar.symbol).avg_cost,
                    ctx.history(bar.symbol, n=1).iloc[-1],
                    ctx.history(bar.symbol, n=1, adjust="qfq").iloc[-1],
                )
            )
            assert not bar.close < ctx.position(bar.symbol).avg_cost * 0.9

    run_backtest(
        ctx=Context(),
        provider=provider,
        user_globals={"setup": setup, "on_bar": on_bar},
    )
    assert seen and all(row == (100, 100, 100, 20) for row in seen)


def test_registered_all_defaults_and_translator_keep_full_jp_configuration(provider):
    from backend.services.engine.strategy_lab.translator import (
        translate_sdk_to_template,
    )

    code = """
def setup(ctx):
    ctx.universe = 'all'
    ctx.cash = 100000
    ctx.commission = ctx.slippage = 0
def on_universe(ctx, date, snapshot):
    assert isinstance(ctx.universe, list)
    assert set(ctx.universe) == {'JP72030', 'JP216A0', 'JP13370'}
    if ctx.today.date().isoformat() == '2026-09-28':
        ctx.set_target_holdings(['JP72030'])
"""
    g = _build_user_globals()
    exec(code, g, g)
    result = run_backtest(ctx=Context(), provider=provider, user_globals=g)
    assert result.status == "success" and result.config["universe"] == "all"
    assert result.config["end"] == "2026-09-30"
    template = translate_sdk_to_template(code, run_id="native", provider=provider)
    assert template.config["market"] == "JP" and template.config["benchmark"] == "TOPIX"
    assert template.config["universe"] == "all" and template.config["cash"] == 100000
    assert template.config["start"] and template.config["end"] == result.config["end"]
    assert not template.needs_review


def test_auxiliary_context_keeps_main_publication_when_current_changes(
    provider, native, monkeypatch
):
    from backend.services.engine.strategy_lab import runtime_context
    from backend.services.engine.strategy_lab.runner.result_collector import RunResult

    saved = provider.reader.data_version
    source, _ = native
    with duckdb.connect(str(source)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET C=102 WHERE Date='2026-09-30' AND Code='72030'"
        )
    newer = publish(native)
    assert newer.data_version != saved
    from backend.services.engine.data_platform.jp_publication import publish_pointer

    publish_pointer(native[1], newer.data_version)
    assert (
        _resolve_provider({"options": {"market": "JP"}}, None).reader.data_version
        == newer.data_version
    )
    monkeypatch.setattr(
        runtime_context,
        "fetch_result",
        lambda rid: RunResult(
            run_id=rid,
            status="success",
            config={
                "market": "JP",
                "data_version": saved,
                "execution_model": "dated_cash",
                "run_params": {"period": 5},
                "stock_pool": "list:JP72030",
            },
        ),
    )
    options, params, pool, resolved = runtime_context.auxiliary_context(run_id="native")
    assert options == {"market": "JP", "data_version": saved}
    assert params == {"period": 5} and pool == "list:JP72030"
    assert resolved.reader.data_version == saved
    with pytest.raises(ValueError, match="publication"):
        runtime_context.auxiliary_context(
            run_id="native", options={"data_version": "different"}
        )


def test_auxiliary_real_subruns_use_registered_cash_and_date_overrides(
    provider, monkeypatch
):
    from backend.services.engine.strategy_lab.overfit import runner
    from backend.services.engine.strategy_lab.overfit.runner import (
        _run_one,
        run_overfit_check,
    )

    monkeypatch.setattr(
        runner,
        "ProgressPublisher",
        lambda **kwargs: SimpleNamespace(publish=lambda *a, **k: None),
    )
    code = """
def setup(ctx):
    ctx.universe = ['JP72030']
    ctx.cash = 100000
    ctx.commission = ctx.slippage = 0
def on_bar(ctx, bar):
    if ctx.position(bar.symbol).qty == 0:
        ctx.buy(bar.symbol, weight=0.5)
"""
    result = _run_one(
        code,
        start="2026-09-29",
        end="2026-09-30",
        provider=provider,
        publisher=SimpleNamespace(publish=lambda *a, **k: None),
    )
    assert result.status == "success" and result.config["start"] == "2026-09-29"
    assert result.config["data_version"] == provider.reader.data_version
    observed = []
    original = runner._run_one

    def checked(*args, **kwargs):
        assert kwargs.get("provider") is provider
        result = original(*args, **kwargs)
        if result is not None:
            observed.append(result.config)
        return result

    monkeypatch.setattr(runner, "_run_one", checked)
    report = run_overfit_check(code, provider=provider)
    assert len(observed) >= 4
    assert all(
        config["market"] == "JP"
        and config["data_version"] == provider.reader.data_version
        for config in observed
    )
    assert not any(
        "CN" in warning or "provider" in warning for warning in report.warnings
    )


def test_translate_preserves_selected_setup_parameters(provider):
    from backend.services.engine.strategy_lab.translator import (
        translate_sdk_to_template,
    )

    provider.run_params = {"period": 5, "capital": 120000}
    template = translate_sdk_to_template(
        "def setup(ctx):\n    ctx.universe='all'\n    ctx.cash=ctx.param('capital',default=100000)\n    ctx.param('period',default=20)\n",
        provider=provider,
    )
    assert template.params["period"] == 5
    assert template.config["cash"] == 120000


def test_missing_source_is_strict_for_declared_jp_only(monkeypatch):
    from backend.services.engine.strategy_lab import runtime_context
    from backend.services.engine.strategy_lab.runner.result_collector import RunResult

    monkeypatch.setattr(runtime_context, "fetch_result", lambda rid: None)
    assert runtime_context.auxiliary_context(run_id="expired") == ({}, {}, None, None)
    assert runtime_context.auxiliary_context(
        run_id="expired", options={"market": "CN"}
    ) == ({"market": "CN"}, {}, None, None)
    with pytest.raises(ValueError, match="missing or expired"):
        runtime_context.auxiliary_context(run_id="expired", options={"market": "JP"})
    monkeypatch.setattr(
        runtime_context,
        "fetch_result",
        lambda rid: RunResult(
            run_id=rid, status="success", config={"benchmark": "SH000300"}
        ),
    )
    with pytest.raises(ValueError, match="registered Japanese"):
        runtime_context.auxiliary_context(run_id="cn-source", options={"market": "JP"})


def test_real_worker_uses_selected_params_before_setup(provider, monkeypatch):
    from backend.services.engine.strategy_lab.runner import worker

    captured = []
    pub = SimpleNamespace(publish=lambda *a, **k: None, set_status=lambda *a, **k: None)
    monkeypatch.setattr(worker, "ProgressPublisher", lambda **kw: pub)
    monkeypatch.setattr(worker, "store_result", lambda result: captured.append(result))
    code = "def setup(ctx):\n    ctx.universe='all'\n    ctx.cash=ctx.param('capital',default=100000)\n    assert ctx.param('period',default=20)==5\n"
    status = worker.run_request(
        {
            "run_id": "isolated",
            "code": code,
            "params": {"capital": 120000, "period": 5},
            "options": {"market": "JP", "data_version": provider.reader.data_version},
        }
    )
    assert status == 0 and captured[0].status == "success"
    assert captured[0].config["cash"] == 120000
    assert captured[0].config["run_params"] == {"capital": 120000, "period": 5}


def test_watch_and_daily_scan_keep_main_publication_and_params(provider, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.services.engine.strategy_lab import routers, runtime_context
    from backend.services.engine.strategy_lab.cron import daily_scan
    from backend.services.engine.strategy_lab.runner.result_collector import RunResult
    from backend.services.engine.strategy_lab.overfit import runner

    config = {
        "market": "JP",
        "execution_model": "dated_cash",
        "data_version": provider.reader.data_version,
        "run_params": {"period": 5},
        "stock_pool": "list:JP72030",
    }
    monkeypatch.setattr(
        runtime_context,
        "fetch_result",
        lambda rid: RunResult(run_id=rid, status="success", config=config),
    )
    stored = []
    monkeypatch.setattr(routers, "add_watch", lambda **kwargs: stored.append(kwargs))
    code = "def setup(ctx):\n    ctx.universe='all'\n    ctx.cash=100000\n    assert ctx.param('period',default=20)==5\ndef on_bar(ctx,bar):\n    if bar.date.date().isoformat()=='2026-09-29':\n        ctx.buy(bar.symbol,qty=100)\n"
    app = FastAPI()
    app.include_router(routers.router)
    response = TestClient(app).post(
        "/strategy-lab/watch",
        json={
            "code": code,
            "name": "native",
            "run_id": "original",
            "options": {"market": "JP"},
        },
    )
    assert response.status_code == 200 and len(stored) == 1
    assert stored[0]["options"] == {
        "market": "JP",
        "data_version": provider.reader.data_version,
    }
    assert stored[0]["params"] == {"period": 5}
    monkeypatch.setattr(daily_scan, "list_watch", lambda: stored)
    from backend.tests.test_jp_sync_watch_review import RecordingRedis
    redis = RecordingRedis()
    monkeypatch.setattr(
        daily_scan,
        "get_redis_sentinel_client",
        lambda: redis,
    )
    monkeypatch.setattr(
        runner,
        "ProgressPublisher",
        lambda **kwargs: SimpleNamespace(publish=lambda *a, **k: None),
    )
    observed = []
    original = daily_scan._run_one

    def checked(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append((kwargs, result))
        return result

    monkeypatch.setattr(daily_scan, "_run_one", checked)
    scan = daily_scan.run_daily_scan()
    assert scan["summary"]["ok"] == 1 and scan["summary"]["failed"] == 0
    kwargs, result = observed[0]
    assert (
        kwargs["end"] == "2026-09-30"
        and kwargs["provider"].reader.data_version == provider.reader.data_version
    )
    assert (
        result.config["stock_pool"] == "list:JP72030"
        and result.config["market"] == "JP"
    )
    assert redis.get(daily_scan.SIGNALS_KEY)
    assert len(scan["signals"]) == 1
    assert scan["signals"][0]["market"] == "JP"
    assert scan["signals"][0]["data_version"] == provider.reader.data_version
    assert scan["signals"][0]["date"] == "2026-09-30"
    assert scan["signals"][0]["execution_date_mode"] == "published_daily_delayed"


def test_original_provider_context_and_translator_defaults_stay_cn():
    from backend.services.engine.strategy_lab.engine.local_provider import (
        bind_registered_context,
    )
    from backend.services.engine.strategy_lab.translator import (
        translate_sdk_to_template,
    )

    ctx = Context()
    before = deepcopy(ctx.to_config_dict())
    bind_registered_context(ctx, SimpleNamespace())
    assert ctx.to_config_dict() == before
    with pytest.raises(ValueError):
        ctx.universe = "all"
    template = translate_sdk_to_template(
        "def setup(ctx):\n    ctx.universe=['SH600036']\n    ctx.start='2026-01-05'\n    ctx.end='2026-06-12'\n    ctx.cash=100000\n"
    )
    assert (
        template.config["benchmark"] == "SH000300" and "market" not in template.config
    )
