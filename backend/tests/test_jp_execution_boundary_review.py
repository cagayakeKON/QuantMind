"""Independent execution-boundary regressions from the merged JP reviews."""

from copy import deepcopy
from datetime import date
import os
import json

import pandas as pd
import pytest

from backend.tests.test_market_simulation_checkpoint import (
    ROOT,
    KEY,
    DAY,
    pg as pg_fixture,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
    initialize,
    manager,
    engine,
    order,
    bar,
    financial_rows,
    MODELS,
)
from backend.tests.test_jp_second_review import lab_native as lab_native_fixture
from backend.tests.strategy_lab.test_worker import fake_redis as fake_redis_fixture

snapshot = snapshot_fixture
published = published_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
lab_native = lab_native_fixture
fake_redis = fake_redis_fixture


@pytest.fixture(autouse=True)
def no_external_unit_file(monkeypatch):
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in")
@pytest.mark.asyncio
async def test_public_bar_execution_rejects_completed_day_without_financial_mutation(
    pg,
):
    from backend.services.simulation.models.account import SimulationAccount

    await initialize(pg)
    async with pg.sessions() as db:
        account = manager(pg, db)
        await account.prepare_dated_day(DAY)
        projection = await account.get_account(7, tenant_id="test", market="JP")
        await account.stage_day_checkpoint(projection)
        await db.commit()
        checkpoint = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)

    async with pg.sessions() as db:
        pending = await order(db)
        before_rows = await financial_rows(pg)
        before_cache = deepcopy(pg.setup.redis.client.values)
        shared = engine(pg, db)
        result = await shared.execute_from_bar(pending, bar(pg), market="JP")
        assert not result.success and "already completed" in result.message
        # Even a caller that commits a rejection cannot erase the closing checkpoint.
        await db.commit()
        root = await db.get(SimulationAccount, ROOT)
        assert root.market_state == checkpoint
        assert root.cash == 250000 and root.base_currency == "CNY"
        assert shared.manager._pending_order is None
        assert pg.setup.redis.client.values == before_cache
        assert KEY in before_cache
    assert await financial_rows(pg) == before_rows


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in")
@pytest.mark.asyncio
async def test_completed_day_stays_closed_after_compatible_raw_publication_advance(
    pg, snapshot, published
):
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.simulation.jp.data import open_execution_data
    from backend.services.simulation.jp.replay_cash_rules import JapanReplayCashRules
    from backend.services.simulation.models.account import SimulationAccount

    async def financial_snapshot(db):
        # Compare every financial field, not only row counts: an in-place change
        # to a lot, balance or existing fill must also be detected.
        values = {}
        for model in MODELS:
            rows = (await db.execute(select(model.__table__))).mappings().all()
            values[model.__tablename__] = sorted(
                [deepcopy(dict(row)) for row in rows], key=repr
            )
        return values

    old = import_jquants_snapshot(snapshot, published, end=DAY)["version"]
    pg.setup.source = open_execution_data(old)
    pg.setup.rules = JapanReplayCashRules(pg.setup.source, slippage_bps="0")
    pg.setup.params["data_version"] = old
    await initialize(pg)
    async with pg.sessions() as db:
        first = await order(db)
        shared = engine(pg, db)
        fill = await shared.execute_from_bar(first, bar(pg), market="JP")
        assert fill.success
        await shared.apply_filled(first, fill)
        account = manager(pg, db)
        await account.prepare_dated_day(DAY)
        projection = await account.get_account(7, tenant_id="test", market="JP")
        assert projection["cash"] == 20000
        assert projection["positions"]["72030.JP"]["volume"] == 100
        await account.stage_day_checkpoint(projection)
        await db.commit()

    latest = import_jquants_snapshot(snapshot, published)["version"]
    assert latest != old
    pg.setup.source = open_execution_data(latest)
    # Establish that the new raw publication is compatible, so rejection cannot
    # be attributed to a missing publication or a consumed-history mismatch.
    proof = pg.setup.source.prove_history_extension(old, DAY)
    assert proof["from_version"] == old and proof["to_version"] == latest
    pg.setup.rules = JapanReplayCashRules(pg.setup.source, slippage_bps="0")
    pg.setup.params["data_version"] = latest

    async with pg.sessions() as db:
        pending = await order(db)
        before = await financial_snapshot(db)
        cache = deepcopy(pg.setup.redis.client.values)
        root = await db.get(SimulationAccount, ROOT)
        completed = deepcopy(root.market_state)
        assert completed["JP"]["cycle_completed"] is True
        assert completed["JP"]["data_version"] == old
        shared = engine(pg, db)
        assert shared.execution_context.data_version == latest
        assert shared.execution_context.trade_date == DAY
        result = await shared.execute_from_bar(pending, bar(pg), market="JP")
        assert not result.success and "already completed" in result.message
        # The shared rejection guard actually holds the root PG row lock until
        # the original caller completes its transaction.
        async with pg.sessions() as competing:
            with pytest.raises(DBAPIError) as locked:
                await competing.execute(
                    select(SimulationAccount)
                    .where(SimulationAccount.account_id == ROOT)
                    .with_for_update(nowait=True)
                )
            assert getattr(locked.value.orig, "sqlstate", None) == "55P03"
            await competing.rollback()
        await db.commit()
        assert await financial_snapshot(db) == before
        assert (await db.get(SimulationAccount, ROOT)).market_state == completed
        assert pg.setup.redis.client.values == cache
        assert shared.manager._pending_order is None


def _setup(ctx):
    ctx.universe = "all"
    ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-30", 1000000
    ctx.benchmark, ctx.commission, ctx.slippage = "TOPIX", 0, 0
    ctx.tax_sell = ctx.transfer_fee = 0


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in")
@pytest.mark.asyncio
@pytest.mark.parametrize("first_has_signals", [False, True])
async def test_completed_cycle_rejects_empty_changed_inputs_and_retry_is_read_only(
    pg, monkeypatch, first_has_signals
):
    from dataclasses import replace
    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.services.hosted_cycle_context import (
        HostedCycleSignals,
    )
    from backend.tests.test_market_simulation_cycle import (
        controlled_context,
        original_engine,
    )

    async def financial_snapshot():
        async with pg.sessions() as db:
            return {
                model.__tablename__: sorted(
                    [
                        deepcopy(dict(row))
                        for row in (
                            await db.execute(select(model.__table__))
                        ).mappings()
                    ],
                    key=repr,
                )
                for model in MODELS
            }

    await initialize(pg)
    context = controlled_context(pg)
    empty = replace(context, hosted_signals=HostedCycleSignals("native-run", ()))
    first_context = context if first_has_signals else empty
    shared = original_engine(pg, monkeypatch)
    first = await shared.run_cycle("test", "00000007", "2", cycle_context=first_context)
    assert first.error is None and first.filled_count == int(first_has_signals)
    before = await financial_snapshot()
    cache = deepcopy(pg.setup.redis.client.values)
    cache_versions = deepcopy(pg.setup.redis.client.versions)
    root = next(
        row for row in before["simulation_accounts"] if row["account_id"] == ROOT
    )
    checkpoint = root["market_state"]["JP"]
    assert checkpoint["cycle_completed"] is True
    assert checkpoint["cycle_inputs"] == first_context.provenance()
    assert checkpoint["metadata"]["state"]["cursor"] == str(DAY)
    assert root["cash"] == 250000 and root["base_currency"] == "CNY"

    changed = replace(
        empty,
        signal_input=replace(empty.signal_input, prediction_sha256="b" * 64),
    )
    assert changed.signals() == []
    rejected = await shared.run_cycle("test", "00000007", "2", cycle_context=changed)
    assert "completed with different model inputs" in (rejected.error or "")
    assert rejected.order_count == rejected.filled_count == 0
    assert await financial_snapshot() == before
    assert pg.setup.redis.client.values == cache
    assert pg.setup.redis.client.versions == cache_versions

    # The shared finish boundary must check inputs while holding the root lock,
    # even for callers outside SimulationEngine's early-return branch.
    async with pg.sessions() as db:
        with pytest.raises(ValueError, match="completed with different model inputs"):
            await changed.finish_day(changed.accounts(db, pg.setup.redis))
        async with pg.sessions() as competing:
            with pytest.raises(DBAPIError) as locked:
                await competing.execute(
                    select(SimulationAccount)
                    .where(SimulationAccount.account_id == ROOT)
                    .with_for_update(nowait=True)
                )
            assert getattr(locked.value.orig, "sqlstate", None) == "55P03"
            await competing.rollback()
        await db.commit()
    assert await financial_snapshot() == before
    assert pg.setup.redis.client.values == cache
    assert pg.setup.redis.client.versions == cache_versions

    again = await shared.run_cycle("test", "00000007", "2", cycle_context=first_context)
    assert again.error is None and again.order_count == again.filled_count == 0
    assert again.account_snapshot == first.account_snapshot
    assert await financial_snapshot() == before
    assert pg.setup.redis.client.values == cache
    assert pg.setup.redis.client.versions == cache_versions


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


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in")
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_inputs", [None, {"trade_date": "2026-09-25"}])
async def test_completed_checkpoint_without_matching_cycle_inputs_cannot_be_rewritten(
    pg, monkeypatch, bad_inputs
):
    from dataclasses import replace
    from sqlalchemy import select
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.services.hosted_cycle_context import (
        HostedCycleSignals,
    )
    from backend.tests.test_market_simulation_cycle import (
        controlled_context,
        original_engine,
    )

    async def financial_snapshot():
        async with pg.sessions() as db:
            return {
                model.__tablename__: sorted(
                    [
                        deepcopy(dict(row))
                        for row in (
                            await db.execute(select(model.__table__))
                        ).mappings()
                    ],
                    key=repr,
                )
                for model in MODELS
            }

    await initialize(pg)
    async with pg.sessions() as db:
        # The shared account interface allows marking a day without model inputs.
        # Such a checkpoint is restorable, but its model identity is unknown.
        account = manager(pg, db)
        await account.prepare_dated_day(DAY)
        projection = await account.get_account(7, tenant_id="test", market="JP")
        await account.stage_day_checkpoint(projection)
        if bad_inputs is not None:
            root = await db.get(SimulationAccount, ROOT)
            states = deepcopy(root.market_state)
            states["JP"]["cycle_inputs"] = deepcopy(bad_inputs)
            root.market_state = states
        await db.commit()
    before = await financial_snapshot()
    cache = deepcopy(pg.setup.redis.client.values)
    cache_versions = deepcopy(pg.setup.redis.client.versions)
    context = controlled_context(pg)
    empty = replace(context, hosted_signals=HostedCycleSignals("native-run", ()))
    shared = original_engine(pg, monkeypatch)
    for candidate in (context, empty):
        rejected = await shared.run_cycle(
            "test", "00000007", "2", cycle_context=candidate
        )
        assert "Completed dated cycle has no matching model inputs" in (
            rejected.error or ""
        )
        assert rejected.order_count == rejected.filled_count == 0
        assert await financial_snapshot() == before
        assert pg.setup.redis.client.values == cache
        assert pg.setup.redis.client.versions == cache_versions


def test_native_daily_callbacks_skip_retired_and_missing_day_but_history_is_asof(
    lab_native,
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


def test_native_callbacks_require_active_master_even_if_exact_price_exists(
    lab_native, monkeypatch
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


def test_public_worker_supports_registered_jp_all_pool(lab_native, fake_redis):
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


@pytest.mark.parametrize("freq", ["5min", "30min"])
def test_native_sdk_rejects_intraday_request_instead_of_running_daily(
    lab_native, fake_redis, freq
):
    from backend.services.engine.strategy_lab.runner.worker import (
        _resolve_provider,
        run_request,
    )
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    ctx = Context()

    def setup(ctx):
        _setup(ctx)
        ctx.freq = freq

    with pytest.raises(NotImplementedError, match="daily SDK bars only"):
        run_backtest(ctx=ctx, provider=provider, user_globals={"setup": setup})
    code = f"""
def setup(ctx):
    ctx.universe = "all"
    ctx.start = "2026-09-28"
    ctx.end = "2026-09-30"
    ctx.cash = 1000000
    ctx.freq = "{freq}"
"""
    run_id = "intraday-review-" + freq
    assert (
        run_request(
            {
                "run_id": run_id,
                "code": code,
                "options": {"market": "JP"},
                "params": {},
            }
        )
        == 1
    )
    result = json.loads(fake_redis.kv["qm:lab:result:" + run_id])
    assert result["status"] == "failed"
    assert "daily SDK bars only" in result["error"]
