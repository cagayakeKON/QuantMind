"""Legacy-format conversion and restoration through the original replay flow."""

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
import pandas as pd
import duckdb
from sqlalchemy import func, select, text

from backend.tests.legacy_jp_cash_oracle import LegacyJPCashOracle
from backend.services.simulation.jp.replay_migration import (
    bind_saved_model,
    prepare_replay_import,
)
from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayTrade,
)
from backend.services.simulation.models.replay_import import ReplayImportReceipt
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.legacy_migration import stage_replay_import
from backend.services.simulation.replay.persistence import load_checkpoint_account
from backend.services.simulation.replay.session_context import (
    open_registered_session_context,
)
from backend.tests.test_market_replay_cash import (
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_replay_checkpoint import pg as pg_fixture
from backend.shared.model_registry import model_registry_service

cash_setup = cash_setup_fixture
published = published_fixture
snapshot_base = snapshot_fixture
pg_base = pg_fixture
SESSION = UUID(int=5101)
ANCHOR = date(2026, 9, 25)
DAY = date(2026, 9, 28)
NEXT = date(2026, 9, 29)
END = date(2026, 9, 30)


@pytest_asyncio.fixture
async def pg(pg_base):
    # Import receipts belong only to this new one-time migration test scope.
    async with pg_base.sessions() as db:
        # Exercise the exact appended startup DDL twice, not merely ORM create.
        ddl = (Path(__file__).parents[1] / "shared/db_init.sql").read_text(
            encoding="utf-8"
        )
        ddl = (
            "CREATE TABLE IF NOT EXISTS replay_import_receipts ("
            + ddl.split("CREATE TABLE IF NOT EXISTS replay_import_receipts (", 1)[1]
        )
        await db.execute(text(ddl))
        await db.execute(text(ddl))
        assert (
            await db.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() "
                    "AND table_name='replay_import_receipts' AND column_name='created_at'"
                )
            )
            == "timestamp with time zone"
        )
        assert not await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema=current_schema() "
                "AND table_name='replay_import_receipts' AND constraint_type='FOREIGN KEY'"
            )
        )
        await db.commit()
    return pg_base


@pytest.fixture
def snapshot(snapshot_base):
    with duckdb.connect(str(snapshot_base)) as connection:
        connection.execute("INSERT INTO research.calendar VALUES ('2026-09-24','1')")
    return snapshot_base


def legacy_record(setup, *, empty=False, completed_days=2, quantity=100):
    # Exercise the obsolete record producer as input, never as the target runner.
    old = LegacyJPCashOracle.create(setup.source.calendar, "30000", slippage_bps=0)
    old.state["next_date"] = str(DAY)
    if not empty:
        for day, signal_day, side in ((DAY, ANCHOR, "BUY"), (NEXT, DAY, "SELL"))[
            :completed_days
        ]:
            bars, master = setup.source.day(day, ["JP72030"], ["JP72030"])
            old.step(
                day,
                bars,
                master,
                [
                    {
                        "order_id": f"model:original:{side}",
                        "symbol": "JP72030",
                        "side": side,
                        "quantity": quantity,
                        "signal_date": str(signal_day),
                        "model_id": "saved-model",
                        "prediction_sha256": "saved-prediction",
                        "data_version": setup.source.data_version,
                        "submitted_at": "2026-10-02T16:00:00+09:00",
                    }
                ],
            )
            old.state["next_date"] = str(setup.source.calendar.next_session(day))
    return {
        "session_id": str(SESSION),
        "tenant_id": "test-owner",
        "user_id": "7",
        "name": "Saved replay",
        "mode": "replay",
        "pending": [],
        "revision": 4,
        "anchor_date": str(ANCHOR),
        "end_date": str(END),
        "data_version": setup.source.data_version,
        "state": deepcopy(old.state),
        "created_at": "2026-10-02T16:00:00+09:00",
        "updated_at": "2026-10-02T16:05:00+09:00",
    }


def context(setup):
    return open_registered_session_context(setup.params, reader=setup.source)


def test_conversion_preserves_funding_history_model_provenance_and_source(cash_setup):
    source = legacy_record(cash_setup)
    unchanged = deepcopy(source)
    plan = prepare_replay_import(source, context=context(cash_setup))
    row = plan["session"]
    assert source == unchanged
    assert row["session_id"] == SESSION and row["user_id"] == 7
    assert row["model_id"] == "saved-model" and not row["auto_trade"]
    assert row["status"] == "ready" and row["next_date"] == END
    assert row["created_at"] == datetime(2026, 10, 2, 7, tzinfo=timezone.utc)
    assert row["signal_progress"]["legacy_import"]["source"] == source
    assert plan["snapshots"][-1]["market_state"]["metadata"]["state"] == source["state"]
    assert [trade["executed_at"] for trade in plan["trades"]] == [
        datetime(2026, 9, 28),
        datetime(2026, 9, 29),
    ]
    assert plan["trades"][-1]["holding_days"] == 1
    assert all(order["origin"] == "signal" for order in plan["orders"])
    assert prepare_replay_import(source, context=context(cash_setup)) == plan
    assert cash_setup.redis.client.keys_touched == []


def test_empty_replay_keeps_initial_funding_and_next_original_session(cash_setup):
    source = legacy_record(cash_setup, empty=True)
    plan = prepare_replay_import(source, context=context(cash_setup))
    assert plan["orders"] == plan["trades"] == plan["snapshots"] == []
    assert plan["session"]["cursor_date"] is None
    assert plan["session"]["next_date"] == DAY


def test_rejected_orders_preserve_reasons_without_creating_trades(cash_setup):
    source = legacy_record(cash_setup, quantity=100000)
    assert all(row["status"] == "rejected" for row in source["state"]["orders"])
    plan = prepare_replay_import(source, context=context(cash_setup))
    assert plan["trades"] == []
    assert [row["reject_reason"] for row in plan["orders"]] == [
        row["reason"] for row in source["state"]["orders"]
    ]
    assert all(row["filled_quantity"] == 0 for row in plan["orders"])


@pytest.mark.parametrize(
    "fault",
    [
        "pending",
        "daily_gap",
        "cursor",
        "next",
        "funding",
        "fill",
        "version",
        "owner",
        "naive_timestamp",
        "duplicate_order",
        "model_version",
        "schema_bool",
        "config_identity",
        "missing_end",
    ],
)
def test_unsupported_or_corrupt_source_is_rejected_without_mutation(cash_setup, fault):
    source = legacy_record(cash_setup)
    state = source["state"]
    if fault == "pending":
        source["pending"] = [{"quantity": 100}]
    elif fault == "daily_gap":
        state["daily"].pop(0)
    elif fault == "cursor":
        state["cursor"] = str(DAY)
    elif fault == "next":
        state["next_date"] = str(NEXT)
    elif fault == "funding":
        state["cash_funds"][0]["amount"] = "123"
    elif fault == "fill":
        state["fills"][0]["price"] = "101"
    elif fault == "version":
        source["data_version"] = "missing-publication"
    elif fault == "owner":
        source["user_id"] = "admin"
    elif fault == "naive_timestamp":
        source["created_at"] = "2026-10-02T07:00:00"
    elif fault == "duplicate_order":
        state["orders"].append(deepcopy(state["orders"][0]))
    elif fault == "model_version":
        state["orders"][0]["data_version"] = "another-publication"
    elif fault == "schema_bool":
        state["schema_version"] = True
    elif fault == "config_identity":
        state["config"]["market"] = "CN"
    elif fault == "missing_end":
        source["end_date"] = None
    unchanged = deepcopy(source)
    with pytest.raises(ValueError):
        prepare_replay_import(source, context=context(cash_setup))
    assert source == unchanged and cash_setup.redis.client.keys_touched == []


async def counts(db):
    return [
        (await db.scalar(select(func.count()).select_from(model)))
        for model in (ReplayOrder, ReplayTrade, ReplayEquitySnapshot)
    ]


def accounts(setup):
    return ReplayAccountManager(
        SESSION,
        setup.redis,
        cash_rules=setup.rules,
        checkpointed=True,
    )


@pytest.mark.asyncio
async def test_import_rollback_rerun_restore_and_continue_in_original_runner(
    pg, tmp_path, monkeypatch
):
    source = legacy_record(pg.setup, completed_days=1)
    directory = tmp_path / "saved-model"
    directory.mkdir()
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "context": {"market": "JP"},
                "data_source": "quantdb_factors",
                "factor_source": "l1_factors",
                "jp_data_version": pg.setup.source.data_version,
                "train_end": "2026-09-24",
                "val_end": "2026-09-24",
                "target_horizon_days": 1,
            }
        ),
        encoding="utf-8",
    )
    prediction = directory / "pred.parquet"
    pd.DataFrame(
        [{"symbol": "JP72030", "trade_date": DAY, "pred": 1, "split": "test"}]
    ).to_parquet(prediction, index=False)
    source["state"]["orders"][0]["prediction_sha256"] = hashlib.sha256(
        prediction.read_bytes()
    ).hexdigest()

    async def resolve(**kwargs):
        assert kwargs["market"] == "JP" and kwargs["model_id"] == "saved-model"
        assert kwargs["tenant_id"] == "test-owner" and kwargs["user_id"] == "7"
        return SimpleNamespace(
            fallback_used=False,
            effective_model_id="saved-model",
            storage_path=str(directory),
        )

    monkeypatch.setattr(model_registry_service, "resolve_effective_model", resolve)
    session_context = context(pg.setup)
    plan = await bind_saved_model(
        prepare_replay_import(source, context=session_context)
    )
    async with pg.sessions() as db:
        assert await stage_replay_import(db, plan)
        assert await counts(db) == [1, 1, 1]
        await db.rollback()
    async with pg.sessions() as db:
        assert await counts(db) == [0, 0, 0]
        assert await db.get(ReplaySession, SESSION) is None
        assert await db.get(ReplayImportReceipt, SESSION) is None
        assert await stage_replay_import(db, plan)
        await db.commit()
    assert pg.setup.redis.client.keys_touched == []
    async with pg.sessions() as db:
        assert not await stage_replay_import(db, plan)
        row = await db.get(ReplaySession, SESSION)
        receipt = await db.get(ReplayImportReceipt, SESSION)
        assert (
            receipt.source_sha256
            == plan["session"]["signal_progress"]["legacy_import"]["source_sha256"]
        )
        assert receipt.created_at.tzinfo is not None
        restored = await load_checkpoint_account(db, row, accounts(pg.setup))
        assert restored["_market_cash_rules"]["state"] == source["state"]
        result = await session_context.runner(NEXT).execute_day(
            db,
            SESSION,
            NEXT,
            accounts(pg.setup),
            initial_cash=row.initial_cash,
            strategy_params=row.strategy_params,
            accepted=[],
            skip=True,
        )
        row.cursor_date, row.next_date = NEXT, END
        row.sessions_done += 1
        row.status = "ready"
        await db.commit()
    pg.setup.redis.client.values.clear()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        assert (
            await load_checkpoint_account(db, row, accounts(pg.setup)) == result.account
        )
        assert result.account["positions"]["72030.JP"]["volume"] == 200
        assert await counts(db) == [1, 1, 2]
        assert not await stage_replay_import(db, plan)
        assert row.cursor_date == NEXT and row.status == "ready"
        await db.rollback()
    # An already verified pre-receipt import can gain its durable receipt,
    # without changing its later cursor or replay history.
    async with pg.sessions() as db:
        await db.delete(await db.get(ReplayImportReceipt, SESSION))
        await db.commit()
    async with pg.sessions() as db:
        assert not await stage_replay_import(db, plan)
        assert (await db.get(ReplaySession, SESSION)).cursor_date == NEXT
        assert await counts(db) == [1, 1, 2]
        assert await db.get(ReplayImportReceipt, SESSION) is not None
        await db.commit()


@pytest.mark.asyncio
async def test_import_does_not_bypass_existing_unresolved_action_block(pg):
    source = legacy_record(pg.setup)
    session_context = context(pg.setup)
    plan = prepare_replay_import(source, context=session_context)
    async with pg.sessions() as db:
        await stage_replay_import(db, plan)
        await db.commit()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        with pytest.raises(ValueError, match="Unresolved rights/corporate action"):
            await session_context.runner(END).execute_day(
                db,
                SESSION,
                END,
                accounts(pg.setup),
                initial_cash=row.initial_cash,
                strategy_params=row.strategy_params,
                accepted=[],
                skip=True,
            )
        await db.rollback()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        restored = await load_checkpoint_account(db, row, accounts(pg.setup))
        assert restored["_market_cash_rules"]["state"] == source["state"]
        assert row.cursor_date == NEXT and await counts(db) == [2, 2, 2]
    assert pg.setup.redis.client.keys_touched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["owner", "source", "history", "receipt", "plan_digest"]
)
async def test_existing_target_conflicts_do_not_overwrite_data(pg, fault):
    plan = prepare_replay_import(legacy_record(pg.setup), context=context(pg.setup))
    async with pg.sessions() as db:
        await stage_replay_import(db, plan)
        await db.commit()
    async with pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        if fault == "owner":
            row.user_id = 8
        elif fault == "source":
            row.signal_progress = {}
        elif fault == "history":
            trade = (await db.execute(select(ReplayTrade))).scalars().first()
            trade.price += 1
        elif fault == "receipt":
            receipt = await db.get(ReplayImportReceipt, SESSION)
            receipt.source_sha256 = "changed"
        else:
            plan["session"]["signal_progress"]["legacy_import"]["source_sha256"] = (
                "changed"
            )
        await db.commit()
    async with pg.sessions() as db:
        with pytest.raises(
            ValueError,
            match="source digest" if fault == "plan_digest" else "Existing replay",
        ):
            await stage_replay_import(db, plan)
        await db.rollback()
        assert await counts(db) == [2, 2, 2]
    assert pg.setup.redis.client.keys_touched == []


async def seed_legacy(pg, records):
    # Fresh UUID schema only; no dependency on the obsolete ORM/service.
    async with pg.sessions() as db:
        await db.execute(
            text("""
            CREATE TABLE jp_simulation_sessions (
                session_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, name TEXT,
                mode TEXT, pending JSONB, revision INT, anchor_date TEXT, end_date TEXT,
                data_version TEXT, state JSONB, created_at TEXT, updated_at TEXT
            )
        """)
        )
        for record in records:
            await db.execute(
                text("""
                INSERT INTO jp_simulation_sessions
                SELECT * FROM jsonb_populate_record(
                    NULL::jp_simulation_sessions, CAST(:record AS JSONB))
            """),
                {"record": json.dumps(record)},
            )
        await db.commit()


@pytest.mark.asyncio
async def test_cli_requires_matching_backup_and_keeps_source_on_apply_and_rerun(
    pg, tmp_path, monkeypatch
):
    from backend.scripts import migrate_jp_replay as cli

    source = legacy_record(pg.setup, empty=True)
    await seed_legacy(pg, [source])
    monkeypatch.setattr(cli, "create_async_engine", lambda url: pg.sessions.kw["bind"])
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"sessions": [source]}), encoding="utf-8")
    dry = await cli.migrate(backup)
    assert not dry["apply"] and dry["source_deleted"] == 0
    async with pg.sessions() as db:
        assert await db.get(ReplaySession, SESSION) is None
    broken = deepcopy(source)
    broken["revision"] += 1
    backup.write_text(json.dumps({"sessions": [broken]}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after backup"):
        await cli.migrate(backup, apply=True)
    backup.write_text(json.dumps({"sessions": [source]}), encoding="utf-8")
    applied = await cli.migrate(backup, apply=True)
    assert applied["sessions"][0]["inserted"]
    assert not (await cli.migrate(backup, apply=True))["sessions"][0]["inserted"]
    async with pg.sessions() as db:
        assert (
            await db.execute(text("SELECT to_jsonb(s) FROM jp_simulation_sessions s"))
        ).scalar_one() == source
        row = await db.get(ReplaySession, SESSION)
        restored = await load_checkpoint_account(db, row, accounts(pg.setup))
        assert restored["cash"] == 30000 and restored["positions"] == {}
        assert row.next_date == DAY and await counts(db) == [0, 0, 0]
    assert pg.setup.redis.client.keys_touched == []


@pytest.mark.asyncio
async def test_cli_target_conflict_rolls_back_the_entire_inventory(
    pg, tmp_path, monkeypatch
):
    from backend.scripts import migrate_jp_replay as cli

    first = legacy_record(pg.setup, empty=True)
    first["session_id"] = str(UUID(int=1))
    second = deepcopy(first)
    second["session_id"] = str(UUID(int=28))  # Existing original fixture session.
    await seed_legacy(pg, [first, second])
    monkeypatch.setattr(cli, "create_async_engine", lambda url: pg.sessions.kw["bind"])
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"sessions": [first, second]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Existing replay"):
        await cli.migrate(backup, apply=True)
    async with pg.sessions() as db:
        assert await db.get(ReplaySession, UUID(int=1)) is None
        assert await db.get(ReplayImportReceipt, UUID(int=1)) is None
        assert await counts(db) == [0, 0, 0]
        assert (
            await db.scalar(text("SELECT count(*) FROM jp_simulation_sessions"))
        ) == 2
    assert pg.setup.redis.client.keys_touched == []
