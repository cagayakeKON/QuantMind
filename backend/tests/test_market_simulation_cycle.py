"""Original ordinary cycle consumes optional native, dated market inputs."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from backend.services.simulation import engine as original
from backend.services.simulation.jp import cycle_data
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.order import SimOrder
from backend.services.simulation.services import cycle_context as adapter
from backend.services.simulation.services.dated_account import (
    DatedSimulationAccountManager,
)
from backend.services.simulation.services.ledger_service import SimulationLedgerService
from backend.services.simulation.services.market_execution_data import ReplaySignalInput
from backend.tests.test_market_replay_signals import (
    model_input as model_input_fixture,
)
from backend.tests.test_market_simulation_checkpoint import (
    DAY,
    KEY,
    ROOT,
    cash_setup as cash_setup_fixture,
    financial_rows,
    initialize,
    pg as pg_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)

model_input = model_input_fixture
cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture
pg = pg_fixture
real_pg = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


@pytest.fixture
def native_input(model_input, monkeypatch):
    row, state = model_input
    calls = []

    async def resolve(tenant, user, model, *, strategy_id):
        calls.append((tenant, user, model, strategy_id))
        return state.directory, {
            **state.meta,
            "jp_data_version": "training-publication",
            "effective_model_id": "saved-jp-model",
        }

    monkeypatch.setattr(cycle_data, "resolve_model", resolve)
    return row, state, calls


async def prepare(native, **changes):
    row, _, _ = native
    params = {**row.strategy_params, "model_id": row.model_id, **changes}
    return await adapter.prepare_registered_cycle_context(
        params,
        tenant_id=row.tenant_id,
        user_id="00000007",
        strategy_id="2",
        trade_date=date(2026, 9, 30),
    )


@pytest.mark.asyncio
async def test_original_model_input_is_pinned_without_using_another_default(
    native_input,
):
    context = await prepare(native_input)
    row, state, calls = native_input
    assert calls == [(row.tenant_id, "00000007", row.model_id, "2")]
    assert context.signal_input.data_day == date(2026, 9, 29)
    assert [s.symbol for s in context.signals()] == ["216A0.JP", "72030.JP"]
    assert all(s.trade_date == date(2026, 9, 30) for s in context.signals())
    assert all(s.user_id == "00000007" for s in context.signals())
    assert context.provenance()["model_data_version"] == "training-publication"
    assert context.provenance()["data_version"] == row.strategy_params["data_version"]
    assert str(state.directory) not in str(context.provenance())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"prediction_sha256": "0" * 64},
        {"_model_data_version": "different"},
        {"data_version": ""},
        {"data_version": "../../outside"},
        {"mode": "code"},
        {"stop_loss_pct": 0.05},
    ],
)
async def test_unavailable_new_inputs_fail_without_cn_fallback(native_input, change):
    with pytest.raises((RuleDataMissing, NotImplementedError)):
        await prepare(native_input, **change)


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
async def test_unregistered_market_does_not_construct_a_context(market):
    assert (
        await adapter.prepare_registered_cycle_context(
            {"market": market},
            tenant_id="t",
            user_id="7",
            strategy_id="2",
            trade_date=DAY,
        )
        is None
    )


@pytest.mark.asyncio
async def test_open_quotes_do_not_use_close_or_create_realtime_ticks(
    native_input, monkeypatch
):
    context = await prepare(native_input)
    reader = context.cash_rules.reader
    bars = reader.load_date(context.trade_date, ["JP72030"])
    bar = bars["72030.JP"]
    bars["72030.JP"] = replace(bar, close=99999)
    monkeypatch.setattr(reader, "load_date", lambda *args: bars)
    quotes, loaded = await context.quotes(["JP72030"])
    assert quotes["72030.JP"].current_price == bar.open
    assert loaded is bars
    bars["72030.JP"] = replace(bar, open=0, close=99999)
    assert (await context.quotes(["JP72030"]))[0] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["user", "tenant", "strategy", "batch"])
async def test_cycle_context_cannot_be_reused_for_another_owner(native_input, case):
    context = await prepare(native_input)
    args = [
        context.tenant_id,
        context.user_id,
        context.strategy_id,
        context.signal_run_id,
    ]
    args[["tenant", "user", "strategy", "batch"].index(case)] = "other"
    with pytest.raises(ValueError, match="owner/strategy/model"):
        context.require_owner(*args)


@pytest.mark.asyncio
async def test_new_minimum_score_matches_original_default_threshold(native_input):
    context = await prepare(native_input)
    context.signal_input.frame.loc[0, "score"] = -0.2
    assert [s.symbol for s in context.signals()] == ["216A0.JP"]


def controlled_context(pg):
    signal_day = pg.setup.source.calendar.sessions[
        pg.setup.source.calendar.sessions.index(DAY) - 1
    ]
    return adapter.SimulationCycleContext(
        "test",
        "00000007",
        "2",
        "model-jp",
        {**pg.setup.params, "_model_data_version": "trained-version"},
        DAY,
        ReplaySignalInput(
            "JP",
            signal_day,
            pd.DataFrame([{"symbol": "JP72030", "score": 0.5}]),
            Path("/controlled/model"),
            Path("/controlled/model/pred.parquet"),
            pg.setup.source.data_version,
            "a" * 64,
        ),
        pg.setup.rules,
    )


def original_engine(pg, monkeypatch):
    engine = original.SimulationEngine(redis=pg.setup.redis)

    @asynccontextmanager
    async def sessions(*args, **kwargs):
        async with pg.sessions() as db:
            yield db

    monkeypatch.setattr(original, "get_session", sessions)
    monkeypatch.setattr(
        original,
        "mirror_virtual_fill",
        AsyncMock(side_effect=AssertionError("live mirror")),
    )
    engine._sync_snapshot = AsyncMock()
    engine.signal_loader = SimpleNamespace(
        load_latest_signals=AsyncMock(side_effect=AssertionError("old model"))
    )
    engine._load_live_quotes = AsyncMock(side_effect=AssertionError("realtime quote"))
    engine._load_bars = AsyncMock(side_effect=AssertionError("other publication"))
    engine._load_strategy_config = AsyncMock(
        return_value=original.StrategyConfig(topk=1, max_position_pct=0.5)
    )
    return engine


@real_pg
@pytest.mark.asyncio
async def test_actual_original_cycle_selects_fills_marks_and_recovers(pg, monkeypatch):
    await initialize(pg)
    context = controlled_context(pg)
    reader = context.cash_rules.reader
    original_load = reader.load_date

    def closing_marks(day, symbols=None):
        return {
            symbol: replace(bar, close=1000)
            for symbol, bar in original_load(day, symbols).items()
        }

    monkeypatch.setattr(reader, "load_date", closing_marks)
    engine = original_engine(pg, monkeypatch)
    original_manager = engine.account_manager
    report = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert report.error is None
    assert report.signal_count == report.order_count == report.filled_count == 1
    assert report.orders[0]["symbol"] == "JP72030"
    assert report.account_snapshot["cash"] == 20000
    assert report.account_snapshot["total_asset"] == 120000
    assert report.account_snapshot["execution_context"] == context.provenance()
    assert "_market_cash_rules" not in report.account_snapshot
    assert engine.account_manager is original_manager
    engine._sync_snapshot.assert_awaited_once_with("test", "00000007", context.market)
    counts = await financial_rows(pg)
    assert (
        counts["sim_orders"] == counts["sim_trades"] == counts["simulation_fills"] == 1
    )
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.base_currency == "CNY"
        saved = deepcopy(root.market_state)
        assert saved["JP"]["cycle_inputs"] == context.provenance()
    # A completed daily cycle reads its committed mark without another opening trade.
    again = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert again.error is None and again.order_count == 0
    assert again.account_snapshot == report.account_snapshot
    assert (await financial_rows(pg))["sim_trades"] == 1
    changed = replace(
        context,
        signal_input=replace(context.signal_input, prediction_sha256="b" * 64),
    )
    rejected = await engine.run_cycle("test", "00000007", "2", cycle_context=changed)
    assert "completed with different model inputs" in rejected.error
    assert (await financial_rows(pg))["sim_trades"] == 1
    pg.setup.redis.client.values.pop(KEY)
    async with pg.sessions() as db:
        fresh = context.accounts(db, pg.setup.redis)
        recovered = await fresh.get_account(7, tenant_id="test", market="JP")
        assert context.public_account(recovered) == report.account_snapshot
        assert (await db.get(SimulationAccount, ROOT)).market_state == saved


@real_pg
@pytest.mark.asyncio
async def test_day_checkpoint_root_commit_failure_does_not_publish_or_complete(pg):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    import uuid

    await initialize(pg)
    context = controlled_context(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        saved = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
        manager = context.accounts(db, pg.setup.redis)
        await context.finish_day(manager)
        await db.execute(
            text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    assert pg.setup.redis.client.values == cache
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == saved


@real_pg
@pytest.mark.asyncio
async def test_cycle_without_initialized_market_cash_never_resets_existing_account(
    pg, monkeypatch
):
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)
    report = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert "initialize or migrate" in report.error
    assert (await financial_rows(pg))["sim_orders"] == 0
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.cash == 250000 and root.market_state is None


@real_pg
@pytest.mark.asyncio
async def test_cycle_preserves_single_order_failure_isolation(pg, monkeypatch):
    await initialize(pg)
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)

    async def failure(*args, **kwargs):
        raise ValueError("controlled ledger failure")

    monkeypatch.setattr(SimulationLedgerService, "_append_cash_entries", failure)
    report = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert report.filled_count == 0 and report.rejected_count == 1
    assert "1 笔订单" in report.error
    counts = await financial_rows(pg)
    assert counts["sim_trades"] == counts["simulation_fills"] == 0
    assert report.account_snapshot["cash"] == 30000


@real_pg
@pytest.mark.asyncio
async def test_day_mark_failure_rolls_back_metadata_and_cache(pg, monkeypatch):
    await initialize(pg)
    context = controlled_context(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        account = context.accounts(db, pg.setup.redis)
        await account.prepare_dated_day(DAY)
        current = await account.get_account(7, tenant_id="test", market="JP")
        current["cash"] -= 1
        with pytest.raises(ValueError, match="cash or inventory"):
            await account.stage_day_checkpoint(current)
        await db.rollback()
    assert pg.setup.redis.client.values == cache


@real_pg
@pytest.mark.asyncio
async def test_registered_pool_uses_original_filter_and_never_trades_outside(
    pg, monkeypatch
):
    from backend.shared.stock_pool.resolver import resolver

    await initialize(pg)
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)
    calls = []

    async def pool(ref, ctx):
        calls.append((ref, ctx.tenant_id, ctx.user_id, ctx.market))
        return SimpleNamespace(market="CN")

    monkeypatch.setattr(resolver, "resolve", pool)
    report = await engine.run_cycle(
        "test", "00000007", "2", pool_id="foreign", cycle_context=context
    )
    assert "pool does not belong" in report.error
    assert calls == [("foreign", "test", "00000007", "JP")]
    assert (await financial_rows(pg))["sim_orders"] == 0


@real_pg
@pytest.mark.asyncio
async def test_unadapted_inventory_action_cannot_change_only_the_cash_checkpoint(
    pg, monkeypatch
):
    await initialize(pg)
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)
    first = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert first.filled_count == 1
    async with pg.sessions() as db:
        saved = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    next_day = date(2026, 9, 29)
    next_context = replace(
        context,
        trade_date=next_day,
        signal_input=replace(context.signal_input, data_day=DAY),
    )
    second = await engine.run_cycle("test", "00000007", "2", cycle_context=next_context)
    assert "corporate-action ledger adapter" in second.error
    assert (await financial_rows(pg))["sim_trades"] == 1
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == saved
