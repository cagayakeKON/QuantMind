"""Opt-in PG tests for registered cash rules in the original replay operations.

Only fresh UUID schemas are created/dropped. Existing accounts are never touched.
"""

import asyncio
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.models import Base
from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayStatus,
    ReplayTrade,
)
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.persistence import load_checkpoint_account
from backend.shared.database_manager_v2 import DatabaseConfig
from backend.tests.test_market_replay_cash import (
    DAY,
    SESSION,
    cash_setup as cash_setup_fixture,
    execution,
    published as published_fixture,
    signal,
    snapshot as snapshot_fixture,
)

cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="Local PostgreSQL integration is opt-in"
)
NEXT = date(2026, 9, 29)
BUY = {"symbol": "72030.JP", "side": "BUY", "quantity": 100}


@pytest_asyncio.fixture
async def pg(cash_setup):
    # One-stock fixture must allocate enough for its 100-share trading unit.
    cash_setup.params.update(topk=1, max_position_pct=1)
    schema = "replay_test_" + uuid.uuid4().hex
    url = DatabaseConfig().get_master_url()
    admin = create_async_engine(url)
    engine = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        m.__table__
                        for m in (
                            ReplaySession,
                            ReplayOrder,
                            ReplayTrade,
                            ReplayEquitySnapshot,
                        )
                    ],
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE commit_guard (session_id UUID REFERENCES replay_sessions "
                    "DEFERRABLE INITIALLY DEFERRED)"
                )
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.execute(
                text("ALTER TABLE replay_equity_snapshots DROP COLUMN market_state")
            )
            migration = next(
                line
                for line in (Path(__file__).parents[1] / "shared/db_init.sql")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.startswith(
                    "ALTER TABLE replay_equity_snapshots ADD COLUMN IF NOT EXISTS market_state"
                )
            )
            await connection.execute(text(migration))
            await connection.execute(text(migration))
        async with sessions() as db:
            db.add(
                ReplaySession(
                    session_id=SESSION,
                    tenant_id="test-owner",
                    user_id=7,
                    strategy_params=deepcopy(cash_setup.params),
                    initial_cash=30000,
                    start_date=DAY,
                    end_date=NEXT,
                    next_date=DAY,
                    sessions_total=2,
                    sessions_done=0,
                    status=ReplayStatus.READY,
                )
            )
            await db.commit()
        yield SimpleNamespace(sessions=sessions, setup=cash_setup)
    finally:
        if engine is not None:
            await engine.dispose()
        # schema is generated here, never supplied or computed from user paths.
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def manager(setup, *, redis=None, session_id=SESSION):
    return ReplayAccountManager(
        session_id, redis or setup.redis, cash_rules=setup.rules, checkpointed=True
    )


def runner(setup, day=DAY):
    engine, _ = execution(setup, day)

    async def load_signals_for_date(**kwargs):
        return [signal()]

    engine._loader = SimpleNamespace(load_signals_for_date=load_signals_for_date)
    return engine


async def execute(
    pg, db, *, accounts=None, engine=None, day=DAY, accepted=None, skip=False
):
    return await (engine or runner(pg.setup, day)).execute_day(
        db,
        SESSION,
        day,
        accounts or manager(pg.setup),
        accepted=accepted if accepted is not None else [BUY],
        initial_cash=30000,
        strategy_params=pg.setup.params,
        skip=skip,
    )


async def advance_cursor(db, day=DAY):
    row = await db.get(ReplaySession, SESSION)
    row.cursor_date = day
    row.next_date = NEXT if day == DAY else None
    row.sessions_done += 1
    row.status = ReplayStatus.READY if day == DAY else ReplayStatus.FINISHED
    return row


async def counts(db):
    return [
        (await db.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (ReplayOrder, ReplayTrade, ReplayEquitySnapshot)
    ]


@pytest.mark.asyncio
async def test_cash_snapshot_and_cursor_commit_together_then_recover_without_cache(pg):
    accounts = manager(pg.setup)
    async with pg.sessions() as db:
        result = await execute(pg, db, accounts=accounts)
        assert pg.setup.redis.client.values == {}
        assert await counts(db) == [1, 1, 1]
        snapshot = (await db.execute(select(ReplayEquitySnapshot))).scalar_one()
        assert set(snapshot.positions) == {"JP72030"}
        assert "positions" not in snapshot.market_state
        fill = snapshot.market_state["metadata"]["state"]["fills"][0]
        trade = (await db.execute(select(ReplayTrade))).scalar_one()
        assert fill["order_id"] == str(trade.order_id)
        assert fill["executed_at"] == "2026-09-28T00:00:00Z"
        await advance_cursor(db)
        await db.commit()
        assert (
            json.loads(next(iter(pg.setup.redis.client.values.values())))
            == result.account
        )
    pg.setup.redis.client.values.clear()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        restored = await load_checkpoint_account(db, row, manager(pg.setup))
        assert restored == result.account
        assert pg.setup.redis.client.values == {}
        result2 = await execute(pg, db, day=NEXT, accepted=[], skip=True)
        await advance_cursor(db, NEXT)
        await db.commit()
        assert result2.account["positions"]["72030.JP"]["volume"] == 200
        assert await counts(db) == [1, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["second_fill", "snapshot", "outer_rollback", "commit"]
)
async def test_failed_day_keeps_financial_rows_and_cache_unchanged(
    pg, failure, monkeypatch
):
    engine = runner(pg.setup)
    if failure == "second_fill":
        original = engine._persist_fill
        calls = 0

        async def broken(*args, **kwargs):
            nonlocal calls
            result = await original(*args, **kwargs)
            await args[0].flush()
            calls += 1
            if calls == 2:
                raise RuntimeError("injected fill failure")
            return result

        monkeypatch.setattr(engine, "_persist_fill", broken)
    elif failure == "snapshot":
        original = engine._write_snapshot

        async def broken(*args, **kwargs):
            await original(*args, **kwargs)
            await args[0].flush()
            raise RuntimeError("injected snapshot failure")

        monkeypatch.setattr(engine, "_write_snapshot", broken)
    async with pg.sessions() as db:
        if failure in ("second_fill", "snapshot"):
            with pytest.raises(RuntimeError, match="injected"):
                await execute(pg, db, engine=engine, accepted=[BUY, BUY])
            # Original router may commit a failure status. No partial fills remain.
            row = await db.get(ReplaySession, SESSION)
            row.status = ReplayStatus.FAILED
            await db.commit()
        else:
            await execute(pg, db)
            await advance_cursor(db)
            if failure == "commit":
                await db.execute(
                    text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
                )
                with pytest.raises(IntegrityError):
                    await db.commit()
            await db.rollback()
    assert pg.setup.redis.client.values == {}
    assert pg.setup.redis.client.keys_touched == []
    async with pg.sessions() as db:
        assert await counts(db) == [0, 0, 0]
        row = await db.get(ReplaySession, SESSION)
        assert (
            row.cursor_date is None and row.sessions_done == 0 and row.next_date == DAY
        )


@pytest.mark.asyncio
async def test_checkpoint_cannot_commit_without_cursor_and_repeated_day_is_rejected(pg):
    async with pg.sessions() as db:
        await execute(pg, db)
        with pytest.raises(ValueError, match="advanced cursor"):
            await db.commit()
        await db.rollback()
        assert await counts(db) == [0, 0, 0]
        await execute(pg, db)
        with pytest.raises(ValueError, match="previous replay day"):
            await execute(pg, db)
        await advance_cursor(db)
        await db.commit()
    before = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        with pytest.raises(ValueError, match="stale"):
            await execute(pg, db)
        await db.rollback()
        assert await counts(db) == [1, 1, 1]
    assert pg.setup.redis.client.values == before


@pytest.mark.asyncio
async def test_unavailable_cache_does_not_fail_committed_day_or_reexecute_fills(pg):
    accounts = manager(pg.setup, redis=SimpleNamespace(client=None))
    async with pg.sessions() as db:
        result = await execute(pg, db, accounts=accounts)
        await advance_cursor(db)
        await db.commit()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        assert await load_checkpoint_account(db, row, accounts) == result.account
        assert await counts(db) == [1, 1, 1]
        with pytest.raises(ValueError, match="stale"):
            await execute(pg, db, accounts=accounts)
    with pytest.raises(ValueError, match="database scope"):
        await accounts.get()


@pytest.mark.asyncio
async def test_proposal_is_staged_without_financial_writes_and_executes_once(pg):
    async with pg.sessions() as db:
        accounts = manager(pg.setup)
        proposal = await runner(pg.setup).propose_day(
            db,
            SESSION,
            DAY,
            accounts,
            pg.setup.params,
            tenant_id="test-owner",
            user_id="7",
        )
        assert proposal["proposals"]
        assert proposal["account"]["cash"] == 30000
        assert await counts(db) == [0, 0, 0]
        row = await db.get(ReplaySession, SESSION)
        row.pending_orders = proposal
        row.status = ReplayStatus.AWAITING_CONFIRM
        await db.commit()
        assert pg.setup.redis.client.values == {}
        result = await execute(pg, db, accounts=accounts)
        await advance_cursor(db)
        await db.commit()
        assert len(result.filled) == 1
        assert await counts(db) == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["market", "data_version", "prepared_date", "cash", "missing", "schema_version"],
)
async def test_corrupt_or_unmigrated_checkpoint_never_falls_back_to_redis(pg, field):
    async with pg.sessions() as db:
        await execute(pg, db)
        await advance_cursor(db)
        await db.commit()
        snap = (await db.execute(select(ReplayEquitySnapshot))).scalar_one()
        checkpoint = deepcopy(snap.market_state)
        if field == "missing":
            snap.market_state = None
        elif field == "cash":
            snap.cash += 1
        elif field == "prepared_date":
            checkpoint["metadata"][field] = "2026-09-25"
            snap.market_state = checkpoint
        elif field == "schema_version":
            checkpoint[field] = True
            snap.market_state = checkpoint
            # Python considers True == 1; force the deliberate JSON corruption.
            flag_modified(snap, "market_state")
        else:
            checkpoint[field] = "CN" if field == "market" else "another-version"
            snap.market_state = checkpoint
        await db.commit()
    before = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        with pytest.raises(ValueError):
            await execute(pg, db, day=NEXT, accepted=[], skip=True)
        await db.rollback()
        assert await counts(db) == [1, 1, 1]
    assert pg.setup.redis.client.values == before


@pytest.mark.asyncio
async def test_concurrent_same_day_uses_pg_lock_and_commits_one_fill(pg):
    started = asyncio.Event()

    async def second():
        async with pg.sessions() as db:
            started.set()
            try:
                await execute(pg, db)
                await advance_cursor(db)
                await db.commit()
                return "committed"
            except ValueError as error:
                await db.rollback()
                return str(error)

    async with pg.sessions() as db:
        await execute(pg, db)
        task = asyncio.create_task(second())
        try:
            await started.wait()
            await asyncio.sleep(0.05)
            assert not task.done()
            await advance_cursor(db)
            await db.commit()
            assert "stale" in await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with pg.sessions() as db:
        assert await counts(db) == [1, 1, 1]


@pytest.mark.asyncio
async def test_other_session_or_owner_cannot_stage_cash(pg):
    async with pg.sessions() as db:
        with pytest.raises(ValueError, match="another session"):
            await execute(pg, db, accounts=manager(pg.setup, session_id=uuid.uuid4()))
        with pytest.raises(ValueError, match="another owner"):
            await runner(pg.setup).propose_day(
                db,
                SESSION,
                DAY,
                manager(pg.setup),
                pg.setup.params,
                tenant_id="another-tenant",
                user_id="7",
            )
        assert await counts(db) == [0, 0, 0]
    assert pg.setup.redis.client.keys_touched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_alias", ["admin", "0007"])
async def test_original_router_alias_and_owner_policy_remains_the_authority(
    pg, user_alias
):
    from fastapi import HTTPException
    from backend.services.simulation.replay.router import _load_owned_session

    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        row.user_id = 0 if user_alias == "admin" else 7
        await db.commit()
        auth = SimpleNamespace(tenant_id="test-owner", user_id=user_alias)
        owned = await _load_owned_session(db, SESSION, auth)
        assert owned.session_id == SESSION
        proposal = await runner(pg.setup).propose_day(
            db,
            SESSION,
            DAY,
            manager(pg.setup),
            pg.setup.params,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
        )
        assert proposal["proposals"]
        for wrong in (
            SimpleNamespace(tenant_id="another-tenant", user_id=user_alias),
            SimpleNamespace(tenant_id="test-owner", user_id="8"),
        ):
            with pytest.raises(HTTPException) as error:
                await _load_owned_session(db, SESSION, wrong)
            assert error.value.status_code == 404
        assert await counts(db) == [0, 0, 0]
    assert pg.setup.redis.client.keys_touched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [None, [BUY]])
async def test_original_run_day_saves_registered_cash_without_another_execution_loop(
    pg, approved
):
    async with pg.sessions() as db:
        result = await runner(pg.setup).run_day(
            db,
            SESSION,
            DAY,
            "test-owner",
            "7",
            manager(pg.setup),
            strategy_params=pg.setup.params,
            approved_orders=approved,
            initial_cash=30000,
        )
        assert len(result.filled) == 1
        await advance_cursor(db)
        await db.commit()
        row = await db.get(ReplaySession, SESSION)
        assert (
            await load_checkpoint_account(db, row, manager(pg.setup)) == result.account
        )
        assert await counts(db) == [1, 1, 1]


@pytest.mark.asyncio
async def test_next_day_failure_restores_previous_funding_inventory_and_history(
    pg, monkeypatch
):
    async with pg.sessions() as db:
        original = await execute(pg, db)
        await advance_cursor(db)
        await db.commit()
    before = deepcopy(pg.setup.redis.client.values)
    engine = runner(pg.setup, NEXT)
    original_snapshot = engine._write_snapshot

    async def fail(*args, **kwargs):
        await original_snapshot(*args, **kwargs)
        await args[0].flush()
        raise RuntimeError("next-day failure after split and marking")

    monkeypatch.setattr(engine, "_write_snapshot", fail)
    async with pg.sessions() as db:
        with pytest.raises(RuntimeError, match="next-day failure"):
            await execute(pg, db, day=NEXT, accepted=[], skip=True, engine=engine)
        await db.commit()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        assert row.cursor_date == DAY and row.sessions_done == 1
        assert (
            await load_checkpoint_account(db, row, manager(pg.setup))
            == original.account
        )
        assert await counts(db) == [1, 1, 1]
    assert pg.setup.redis.client.values == before


@pytest.mark.asyncio
async def test_saved_settings_and_root_transaction_are_required_before_any_cash_write(
    pg,
):
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        params = deepcopy(pg.setup.params)
        params["commission_rate"] = "0.01"
        row.strategy_params = params
        await db.commit()
        with pytest.raises(ValueError, match="cash settings"):
            await runner(pg.setup).execute_day(
                db,
                SESSION,
                DAY,
                manager(pg.setup),
                [BUY],
                initial_cash=30000,
                strategy_params=params,
            )
        row.strategy_params = deepcopy(pg.setup.params)
        await db.commit()
        async with db.begin_nested():
            with pytest.raises(ValueError, match="root transaction"):
                await execute(pg, db)
        assert await counts(db) == [0, 0, 0]
    assert pg.setup.redis.client.keys_touched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("commission", ["0", ".0001"])
async def test_common_confirmation_shadow_matches_the_actual_checkpointed_execution(
    pg, commission
):
    from backend.services.simulation.jp.replay_cash_rules import JapanReplayCashRules
    from backend.services.simulation.replay.confirmation import (
        RegisteredReplayConfirmationRules,
    )
    from backend.services.simulation.replay.proposal import validate_confirmed

    pg.setup.params["commission_rate"] = commission
    pg.setup.rules = JapanReplayCashRules(
        pg.setup.source, commission_rate=commission, slippage_bps="0"
    )
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        row.strategy_params = deepcopy(pg.setup.params)
        await db.commit()
        accounts = manager(pg.setup)
        original = await load_checkpoint_account(db, row, accounts)
        _, context = execution(pg.setup)
        hooks = RegisteredReplayConfirmationRules(context, pg.setup.rules, original)
        accepted, rejected = validate_confirmed(
            [{"symbol": "JP72030", "side": "BUY", "quantity": 100}],
            [
                {
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                    "est_price": 100,
                    "origin": "signal",
                    "cancellable": True,
                }
            ],
            original,
            rules=hooks,
        )
        assert rejected == [] and pg.setup.redis.client.values == {}
        assert await counts(db) == [0, 0, 0]
        result = await execute(pg, db, accounts=accounts, accepted=accepted)
        await advance_cursor(db)
        await db.commit()
        actual = await load_checkpoint_account(db, row, manager(pg.setup))
        assert actual == result.account
        for field in ("cash", "market_value", "total_asset", "positions", "currency"):
            assert actual[field] == hooks._account[field]
        # Preview IDs are disposable; actual IDs reference the persisted orders.
        expected = deepcopy(hooks._account["_market_cash_rules"]["state"])
        observed = actual["_market_cash_rules"]["state"]
        assert len(expected["fills"]) == len(observed["fills"]) == 1
        preview_id = expected["fills"][0]["order_id"]
        actual_id = observed["fills"][0]["order_id"]
        trade = (await db.execute(select(ReplayTrade))).scalar_one()
        assert actual_id == str(trade.order_id)
        for item in expected["fills"] + expected["settlements"]:
            assert item["order_id"] == preview_id
            item["order_id"] = actual_id
        assert expected == observed
        assert await counts(db) == [1, 1, 1]
