"""Read-only migration provenance in the original account guard, isolated PG."""

from copy import deepcopy
from datetime import date
import os
from uuid import UUID

import pytest
from sqlalchemy import text

from backend.services.simulation.jp.account_data import legacy_history_exists
from backend.services.simulation.jp.replay_migration import prepare_replay_import
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.jp import JPSimulationSession
from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayTrade,
)
from backend.services.simulation.models.replay_import import ReplayImportReceipt
from backend.services.simulation.replay.legacy_migration import stage_replay_import
from backend.tests.test_jp_replay_migration import legacy_record
from backend.tests.test_market_simulation_account_api import (
    ROOT,
    api as api_fixture,
    cash_setup as cash_setup_fixture,
    pg as pg_fixture,
    published as published_fixture,
    request_body,
    AUTH,
    snapshot as snapshot_fixture,
)

api = api_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
published = published_fixture
snapshot = snapshot_fixture

pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


async def archive(api):
    saved = legacy_record(api.pg.setup, completed_days=1)
    async with api.pg.sessions() as db:
        connection = await db.connection()
        for model in (
            ReplaySession,
            ReplayOrder,
            ReplayTrade,
            ReplayEquitySnapshot,
            ReplayImportReceipt,
        ):
            await connection.run_sync(
                lambda sync, table=model.__table__: table.create(sync, checkfirst=True)
            )
        db.add(
            JPSimulationSession(
                session_id=UUID(saved["session_id"]),
                tenant_id="test",
                user_id="00000007",
                name=saved["name"],
                mode="replay",
                anchor_date=date.fromisoformat(saved["anchor_date"]),
                end_date=date.fromisoformat(saved["end_date"]),
                data_version=saved["data_version"],
                state=saved["state"],
                pending=[],
                revision=saved["revision"],
            )
        )
        await db.flush()
        source = await db.scalar(
            text("SELECT to_jsonb(s) FROM jp_simulation_sessions s")
        )
        await stage_replay_import(db, prepare_replay_import(source))
        await db.commit()
    return source


@pytest.mark.asyncio
async def test_imported_archive_does_not_block_original_account_read_or_write_history(
    api,
):
    source = await archive(api)
    before = deepcopy(api.pg.setup.redis.client.values)
    async with api.pg.sessions() as db:
        assert not await legacy_history_exists(
            db, tenant_id="test", user_ids={"00000007", "7"}
        )
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 200, response.text
    assert api.pg.setup.redis.client.values == before and not api.stops.mock_calls
    async with api.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).cash == 250000
        assert (
            await db.scalar(text("SELECT to_jsonb(s) FROM jp_simulation_sessions s"))
            == source
        )
        assert await db.scalar(text("SELECT count(*) FROM replay_trades")) == 1
    # The original reset initializes a distinct ordinary checkpoint after the
    # verified import; it must not turn replay fills into ordinary trades.
    response = await api.http.post("/api/v1/simulation/reset", json=request_body(api))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["currency"] == "JPY"
    async with api.pg.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM replay_trades")) == 1
        assert await db.scalar(text("SELECT count(*) FROM sim_trades")) == 0
        assert (
            await db.scalar(text("SELECT to_jsonb(s) FROM jp_simulation_sessions s"))
            == source
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "receipt_owner",
        "receipt_tenant",
        "receipt_market",
        "receipt_version",
        "receipt_format",
        "receipt_digest",
        "owner",
        "tenant",
        "market",
        "version",
        "marker",
        "digest",
        "cursor",
        "source",
        "daily",
    ],
)
async def test_incomplete_or_changed_archive_keeps_migration_block(api, case):
    source = await archive(api)
    identity = UUID(source["session_id"])
    async with api.pg.sessions() as db:
        row = await db.get(ReplaySession, identity)
        if case == "missing":
            await db.delete(await db.get(ReplayImportReceipt, identity))
        elif case.startswith("receipt_"):
            receipt = await db.get(ReplayImportReceipt, identity)
            field = {
                "receipt_owner": "user_id",
                "receipt_tenant": "tenant_id",
                "receipt_market": "market",
                "receipt_version": "data_version",
                "receipt_format": "source_format",
                "receipt_digest": "source_sha256",
            }[case]
            setattr(receipt, field, 8 if field == "user_id" else "changed")
        elif case == "owner":
            row.user_id = 8
        elif case == "tenant":
            row.tenant_id = "other"
        elif case in {"market", "version"}:
            row.strategy_params = {
                **row.strategy_params,
                {"market": "market", "version": "data_version"}[case]: "changed",
            }
        elif case in {"marker", "digest"}:
            progress = deepcopy(row.signal_progress)
            if case == "marker":
                progress.pop("legacy_import")
            else:
                progress["legacy_import"]["source_sha256"] = "changed"
            row.signal_progress = progress
        elif case == "cursor":
            row.cursor_date = None
        else:
            legacy = await db.get(JPSimulationSession, identity)
            if case == "daily":
                legacy.mode = "daily"
            else:
                legacy.name = "changed source"
        await db.commit()
    async with api.pg.sessions() as db:
        assert await legacy_history_exists(
            db, tenant_id="test", user_ids={"00000007", "7"}
        )
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 409 and "require migration" in response.text
    assert not api.stops.mock_calls


@pytest.mark.asyncio
async def test_original_discard_keeps_import_proof_and_ordinary_account_available(
    api, monkeypatch
):
    from backend.services.simulation.replay import account, router

    source = await archive(api)
    identity = UUID(source["session_id"])
    app = api.http._transport.app
    app.include_router(router.router)
    app.dependency_overrides[router.get_auth_context] = lambda: AUTH
    monkeypatch.setattr(account, "get_redis", lambda: api.pg.setup.redis)
    replay_key = f"replay:account:{identity}"
    api.pg.setup.redis.client.set(replay_key, "discard only this replay")
    discarded = await api.http.delete(f"/api/v1/replay/sessions/{identity}")
    assert discarded.status_code == 204, discarded.text
    assert api.pg.setup.redis.client.get(replay_key) is None
    async with api.pg.sessions() as db:
        assert await db.get(ReplaySession, identity) is None
        assert await db.get(ReplayImportReceipt, identity) is not None
        assert await db.scalar(text("SELECT count(*) FROM replay_trades")) == 0
        assert await db.scalar(text("SELECT count(*) FROM replay_orders")) == 0
        assert (
            await db.scalar(text("SELECT count(*) FROM replay_equity_snapshots")) == 0
        )
        assert not await legacy_history_exists(
            db, tenant_id="test", user_ids={"00000007", "7"}
        )
        assert (
            await db.scalar(text("SELECT to_jsonb(s) FROM jp_simulation_sessions s"))
            == source
        )
        with pytest.raises(ValueError, match="discarded"):
            await stage_replay_import(db, prepare_replay_import(source))
        await db.rollback()
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 200, response.text
    response = await api.http.post("/api/v1/simulation/reset", json=request_body(api))
    assert response.status_code == 200, response.text
    async with api.pg.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM sim_trades")) == 0
        assert await db.get(ReplayImportReceipt, identity) is not None
        assert (
            await db.scalar(text("SELECT to_jsonb(s) FROM jp_simulation_sessions s"))
            == source
        )
