"""Dated share events use original lots/audit rows in an isolated PG schema."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)
from backend.services.trade_shared import redis_client as redis_module
from backend.tests.test_market_simulation_cycle import (
    KEY,
    ROOT,
    DAY,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
    pg as pg_fixture,
    controlled_context,
    original_engine,
    initialize,
)

cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture
pg = pg_fixture

pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)
SPLIT_DAY = date(2026, 9, 29)


@pytest_asyncio.fixture
async def action_pg(pg, monkeypatch):
    async with pg.sessions() as db:
        conn = await db.connection()
        await conn.run_sync(
            lambda sync: SimulationCorporateAction.__table__.create(sync)
        )
        await db.commit()
    monkeypatch.setattr(redis_module, "redis_client", pg.setup.redis)

    async def price(session, symbol):
        return 50.0

    monkeypatch.setattr(SimulationCorporateActionService, "_load_latest_price", price)

    def forbidden(**kwargs):
        raise AssertionError(
            "Dated actions must not publish the combined CN root cache"
        )

    monkeypatch.setattr(
        SimulationCorporateActionService, "_persist_projection_cache", forbidden
    )
    yield pg


async def buy_and_context(pg, monkeypatch):
    await initialize(pg)
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)
    first = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert first.filled_count == 1 and first.error is None
    next_context = replace(
        context,
        trade_date=SPLIT_DAY,
        signal_input=replace(context.signal_input, data_day=DAY),
    )
    return engine, next_context


async def rows(pg):
    async with pg.sessions() as db:
        return {
            "actions": [
                (a.symbol, a.share_ratio, a.status, a.effective_date)
                for a in (await db.execute(select(SimulationCorporateAction))).scalars()
            ],
            "lots": [
                (
                    lot.account_id,
                    lot.symbol,
                    lot.quantity_open,
                    lot.quantity_remaining,
                    lot.cost_price,
                    lot.cost_amount,
                )
                for lot in (
                    await db.execute(
                        select(SimulationPositionLot).order_by(SimulationPositionLot.id)
                    )
                ).scalars()
            ],
            "cash": [
                (
                    entry.event_type,
                    entry.amount,
                    entry.ref_type,
                    entry.ref_id,
                    entry.currency,
                )
                for entry in (
                    await db.execute(
                        select(SimulationCashLedger).order_by(SimulationCashLedger.id)
                    )
                ).scalars()
            ],
            "state": deepcopy((await db.get(SimulationAccount, ROOT)).market_state),
        }


def override_raw_action(reader, monkeypatch, day, factor, kind):
    original_day = reader.day

    def read(selected_day, *args, **kwargs):
        bars, info = original_day(selected_day, *args, **kwargs)
        if selected_day == day:
            bars = deepcopy(bars)
            for bar in bars.values():
                bar.update(adj_factor=factor, ex_rights_type=kind)
        return bars, info

    monkeypatch.setattr(reader, "day", read)


@pytest.mark.asyncio
async def test_split_commits_original_lots_audit_checkpoint_then_recovers(
    action_pg, monkeypatch
):
    pg = action_pg
    engine, context = await buy_and_context(pg, monkeypatch)
    async with pg.sessions() as db:
        db.add(
            SimulationAccount(account_id="sim:other:8", tenant_id="other", user_id="8")
        )
        db.add(
            SimulationPositionLot(
                account_id="sim:other:8",
                tenant_id="other",
                user_id="8",
                symbol="JP72030",
                quantity_open=100,
                quantity_remaining=100,
                cost_amount=10000,
                cost_price=100,
            )
        )
        await db.commit()
    before = await rows(pg)
    cache_cn = pg.setup.redis.client.values["simulation:account:test:7"]
    cache_jp = pg.setup.redis.client.values[KEY]
    async with pg.sessions() as db:
        manager = context.accounts(db, pg.setup.redis)
        await manager.prepare_dated_day(SPLIT_DAY)
        prepared = await manager.get_account(7, tenant_id="test", market="JP")
        assert prepared["positions"]["72030.JP"]["volume"] == 200
        assert prepared["positions"]["72030.JP"]["cost"] == 50
        assert prepared["cash"] == 20000
        assert pg.setup.redis.client.values[KEY] == cache_jp
        await db.commit()
    after = await rows(pg)
    assert after["lots"][0] == before["lots"][0]  # original CN holding
    assert after["lots"][-1] == before["lots"][-1]  # another owner's JP holding
    assert after["lots"][1][2:] == (200, 200, 50, 10000)
    assert after["actions"] == [("JP72030", 2, "applied", datetime(2026, 9, 29))]
    audit = [x for x in after["cash"] if x[0] == "BONUS_SHARE_VALUE"]
    assert len(audit) == 1 and audit[0][1:3] == (5000, "corporate_action")
    assert audit[0][-1] == "JPY"
    assert pg.setup.redis.client.values["simulation:account:test:7"] == cache_cn
    # Cycle continuation sees the already applied action and never applies it twice.
    result = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert result.error is None
    assert (await rows(pg))["actions"] == after["actions"]
    again = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert again.account_snapshot == result.account_snapshot
    pg.setup.redis.client.values.pop(KEY)
    async with pg.sessions() as db:
        recovered = await context.accounts(db, pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert context.public_account(recovered) == result.account_snapshot
    following = replace(
        context,
        trade_date=date(2026, 9, 30),
        signal_input=replace(context.signal_input, data_day=SPLIT_DAY),
    )
    before_unavailable = await rows(pg)
    continued = await engine.run_cycle("test", "00000007", "2", cycle_context=following)
    assert "Unresolved rights/corporate action" in continued.error
    assert await rows(pg) == before_unavailable
    saved = await rows(pg)
    # A separate controlled continuation replaces the deliberately unresolved
    # rights fixture, without changing the original missing-rights rule.
    override_raw_action(
        context.cash_rules.reader, monkeypatch, date(2026, 9, 30), 1, ""
    )
    continued = await engine.run_cycle("test", "00000007", "2", cycle_context=following)
    assert continued.error is None
    assert (await rows(pg))["actions"] == saved["actions"] == after["actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "after_action",
        "apply_error",
        "root_commit",
        "inventory",
        "wrong_date",
        "wrong_symbol",
    ],
)
async def test_action_lots_and_cash_checkpoint_roll_back_together(
    action_pg, monkeypatch, failure
):
    pg = action_pg
    _, context = await buy_and_context(pg, monkeypatch)
    if failure == "inventory":
        async with pg.sessions() as db:
            lot = (
                await db.execute(
                    select(SimulationPositionLot).where(
                        SimulationPositionLot.symbol == "JP72030"
                    )
                )
            ).scalar_one()
            lot.quantity_remaining -= 1
            await db.commit()
    before, cache = await rows(pg), deepcopy(pg.setup.redis.client.values)
    if failure in {"wrong_date", "wrong_symbol"}:
        bar = context.cash_rules.reader.get_bar("JP72030", SPLIT_DAY)
        wrong = replace(
            bar,
            **(
                {"trade_date": DAY}
                if failure == "wrong_date"
                else {"symbol": "216A0.JP"}
            ),
        )
        monkeypatch.setattr(context.cash_rules.reader, "get_bar", lambda *args: wrong)
    if failure == "apply_error":
        original_apply = SimulationCorporateActionService._apply_action

        async def fail_after_apply(**kwargs):
            await original_apply(**kwargs)
            raise ValueError("controlled original action failure")

        monkeypatch.setattr(
            SimulationCorporateActionService, "_apply_action", fail_after_apply
        )
    async with pg.sessions() as db:
        manager = context.accounts(db, pg.setup.redis)
        if failure == "inventory":
            with pytest.raises(ValueError, match="original lots before"):
                await manager.prepare_dated_day(SPLIT_DAY)
        elif failure == "apply_error":
            with pytest.raises(ValueError, match="controlled original action failure"):
                await manager.prepare_dated_day(SPLIT_DAY)
        elif failure in {"wrong_date", "wrong_symbol"}:
            with pytest.raises(ValueError, match="corporate-action opening mark"):
                await manager.prepare_dated_day(SPLIT_DAY)
        else:
            await manager.prepare_dated_day(SPLIT_DAY)
            assert pg.setup.redis.client.values == cache
            if failure == "root_commit":
                await db.execute(
                    text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
                )
                with pytest.raises(IntegrityError):
                    await db.commit()
        await db.rollback()
    assert await rows(pg) == before
    assert pg.setup.redis.client.values == cache


@pytest.mark.asyncio
async def test_skipped_held_session_is_unavailable_instead_of_silent_split_loss(
    action_pg, monkeypatch
):
    pg = action_pg
    _, context = await buy_and_context(pg, monkeypatch)
    override_raw_action(
        context.cash_rules.reader, monkeypatch, date(2026, 9, 30), 1, ""
    )
    before = await rows(pg)
    async with pg.sessions() as db:
        with pytest.raises(ValueError, match="consecutive"):
            await context.accounts(db, pg.setup.redis).prepare_dated_day(
                date(2026, 9, 30)
            )
        await db.rollback()
    assert await rows(pg) == before


@pytest.mark.asyncio
async def test_reverse_split_retains_original_zero_audit_marker(action_pg, monkeypatch):
    pg = action_pg
    _, context = await buy_and_context(pg, monkeypatch)
    override_raw_action(context.cash_rules.reader, monkeypatch, SPLIT_DAY, 2, "2")
    async with pg.sessions() as db:
        manager = context.accounts(db, pg.setup.redis)
        await manager.prepare_dated_day(SPLIT_DAY)
        await db.commit()
    saved = await rows(pg)
    assert saved["lots"][1][2:] == (50, 50, 200, 10000)
    audit = [item for item in saved["cash"] if item[0] == "BONUS_SHARE_VALUE"]
    assert len(audit) == 1 and audit[0][1] == 0
    assert audit[0][-1] == "JPY"
    async with pg.sessions() as db:
        manager = context.accounts(db, pg.setup.redis)
        await manager.prepare_dated_day(SPLIT_DAY)
        await db.commit()
    assert await rows(pg) == saved
