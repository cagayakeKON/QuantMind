"""Common account HTTP endpoints with actual isolated PG and original settings.

Only sandbox stop methods are replaced; no production process can be stopped.
All financial sessions and snapshot writes are in the fixture's UUID schema.
"""

from contextlib import asynccontextmanager
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import fnmatch
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.account_daily import SimulationAccountDaily
from backend.services.simulation.models.fund_snapshot import SimulationFundSnapshot
from backend.services.simulation.models.jp import JPSimulationSession
from backend.services.simulation.models.position_daily import SimulationPositionDaily
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.routers import simulation as routes
from backend.services.simulation.jp import account_data
from backend.services.simulation.services import account_context as adapter
from backend.services.simulation.services import fund_snapshot_service as snapshots
from backend.services.trade.sandbox.manager import sandbox_manager
from backend.services.trade_shared.deps import AuthContext
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.shared import database_manager_v2 as database
from backend.tests.test_market_simulation_cycle import (
    DAY,
    KEY,
    ROOT,
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
AUTH = AuthContext("00000007", "test", "00000007", ["user"])


@pytest_asyncio.fixture
async def api(pg, monkeypatch):
    async with pg.sessions() as db:
        conn = await db.connection()
        for model in (
            JPSimulationSession,
            SimulationFundSnapshot,
            SimulationAccountDaily,
            SimulationPositionDaily,
            Portfolio,
        ):
            await conn.run_sync(
                lambda sync, table=model.__table__: table.create(sync, checkfirst=True)
            )
        await db.commit()

    @asynccontextmanager
    async def local_session(read_only=False):
        async with pg.sessions() as db:
            if read_only:
                await db.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield db
                if not read_only:
                    await db.commit()
            except BaseException:
                await db.rollback()
                raise

    monkeypatch.setattr(database, "get_session", local_session)
    monkeypatch.setattr(adapter, "get_session", local_session)
    monkeypatch.setattr(snapshots, "get_session", local_session)
    client = pg.setup.redis.client
    client.scan_iter = lambda match="*", **kw: iter(
        [key for key in client.values if fnmatch.fnmatchcase(key, match)]
    )
    patterns = []

    def delete_pattern(pattern):
        patterns.append(pattern)
        for key in list(client.values):
            if fnmatch.fnmatchcase(key, pattern):
                client.delete(key)

    pg.setup.redis.delete_pattern = delete_pattern
    stops = Mock()
    monkeypatch.setattr(sandbox_manager, "stop_strategy", stops.stop_strategy)
    monkeypatch.setattr(
        sandbox_manager, "stop_user_strategies", stops.stop_user_strategies
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/simulation")
    app.dependency_overrides[routes.get_auth_context] = lambda: AUTH
    app.dependency_overrides[routes.get_redis] = lambda: pg.setup.redis

    async def session_dependency():
        async with local_session() as db:
            yield db

    app.dependency_overrides[routes.get_db] = session_dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://audit"
    ) as http:
        yield SimpleNamespace(pg=pg, http=http, stops=stops, patterns=patterns)


def request_body(api, **changes):
    return {
        "market": "JP",
        "initial_cash": 300000,
        "execution_context": {
            "data_version": api.pg.setup.source.data_version,
            "trade_date": str(DAY),
            "commission_rate": "0",
            "slippage_bps": "0",
        },
        **changes,
    }


async def legacy(api, tenant="test", user="00000007"):
    async with api.pg.sessions() as db:
        row = JPSimulationSession(
            tenant_id=tenant,
            user_id=user,
            mode="replay",
            anchor_date=DAY,
            data_version=api.pg.setup.source.data_version,
            state={"opaque": "keep"},
        )
        db.add(row)
        await db.commit()
        return row.session_id


@pytest.mark.asyncio
async def test_reset_runs_original_cleanup_settings_stops_and_user_aggregate(
    api, monkeypatch
):
    pg = api.pg
    cn_cache = pg.setup.redis.client.values["simulation:account:test:7"]
    active_key = "trade:active_strategy:test:00000007"
    pg.setup.redis.client.values[active_key] = json.dumps({"strategy_id": "2"})
    async with pg.sessions() as db:
        db.add(
            SimulationAccount(
                account_id="sim:other:8", tenant_id="other", user_id="8", cash=77777
            )
        )
        db.add(
            Portfolio(
                tenant_id="test",
                user_id="00000007",
                name="controlled",
                run_status="running",
            )
        )
        await db.commit()
    response = await api.http.post("/api/v1/simulation/reset", json=request_body(api))
    assert response.status_code == 200, response.text
    public = response.json()["data"]
    assert public["cash"] == public["total_asset"] == 300000
    assert public["currency"] == "JPY" and public["positions"] == {}
    assert "_market_cash_rules" not in public
    assert public["execution_context"]["data_version"] == pg.setup.source.data_version
    assert active_key not in pg.setup.redis.client.values
    assert api.stops.stop_strategy.called and api.stops.stop_user_strategies.called
    assert pg.setup.redis.client.values["simulation:account:test:7"] == cn_cache
    settings = await routes.SimulationAccountManager(pg.setup.redis).get_settings(
        7, tenant_id="test"
    )
    assert settings["initial_cash"] == 300000
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.base_currency == "CNY"  # original user root policy
        assert root.initial_equity == root.cash == 300000
        assert (
            root.market_state["JP"]["metadata"]["state"]["initial_cash"] == "300000.0"
        )
        assert not (await db.execute(select(SimulationPositionLot))).scalars().all()
        assert (await db.get(SimulationAccount, "sim:other:8")).cash == 77777
        portfolio = (await db.execute(select(Portfolio))).scalar_one()
        assert portfolio.run_status == "stopped"
        # Original global fund snapshot still sums both market cache keys.
        snap = (await db.execute(select(SimulationFundSnapshot))).scalar_one()
        assert snap.total_asset == 550000 and snap.initial_capital == 300000
    context = controlled_context(pg)
    engine = original_engine(pg, monkeypatch)
    # The raw fixture trades 1,000 shares. Use an ordinary 5% strategy cap
    # rather than asking the unchanged matcher to fill 1,500 shares.
    engine._load_strategy_config.return_value.max_position_pct = 0.05
    report = await engine.run_cycle("test", "00000007", "2", cycle_context=context)
    assert report.error is None and report.filled_count == 1
    pg.setup.redis.client.values.pop(KEY)
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 200, response.text
    read = response.json()["data"]
    assert read["positions"] == report.account_snapshot["positions"]
    assert read["execution_context"]["last_cycle_inputs"] == context.provenance()
    assert read["initial_equity"] == 300000 and read["position_count"] == 1
    assert KEY not in pg.setup.redis.client.values  # read-only recovery


@pytest.mark.asyncio
async def test_read_keeps_original_user_baselines_and_does_not_repair_cache(api):
    pg = api.pg
    await initialize(pg, cash=100000)
    await routes.SimulationAccountManager(pg.setup.redis).set_initial_cash(
        7, 100000, tenant_id="test"
    )
    async with pg.sessions() as db:
        db.add(
            SimulationFundSnapshot(
                tenant_id="test",
                user_id="7",
                snapshot_date=snapshots._local_today() - timedelta(days=1),
                total_asset=250000,
            )
        )
        await db.commit()
        state = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    pg.setup.redis.client.values.pop(KEY)
    cache = deepcopy(pg.setup.redis.client.values)
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["initial_equity"] == 100000
    assert data["baseline"]["day_open_equity"] == 250000
    assert (
        data["daily_pnl"] == -150000
    )  # original user baseline, not JP historical date
    assert pg.setup.redis.client.values == cache
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == state


@pytest.mark.asyncio
async def test_missing_checkpoint_never_reconstructs_cash_from_redis(api):
    api.pg.setup.redis.client.values[KEY] = json.dumps(
        {"cash": 999999, "positions": {}}
    )
    cache = deepcopy(api.pg.setup.redis.client.values)
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert (
        response.status_code == 200
        and response.json()["data"]["account_not_initialized"]
    )
    assert api.pg.setup.redis.client.values == cache
    async with api.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mapping", "metadata", "config_identity"])
async def test_invalid_checkpoint_is_unavailable_without_repair(api, case):
    await initialize(api.pg, cash=100000)
    async with api.pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        state = deepcopy(root.market_state)
        if case == "mapping":
            state = ["invalid"]
        if case == "metadata":
            state["JP"]["metadata"] = ["invalid"]
        if case == "config_identity":
            state["JP"]["metadata"]["state"]["config"]["data_version"] = "injected"
        root.market_state = state
        await db.commit()
    cache = deepcopy(api.pg.setup.redis.client.values)
    response = await api.http.get("/api/v1/simulation/account", params={"market": "JP"})
    assert response.status_code == 409, response.text
    assert api.pg.setup.redis.client.values == cache
    async with api.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == state


@pytest.mark.asyncio
async def test_date_and_cash_come_from_one_checkpoint_during_concurrent_commit(
    api, monkeypatch
):
    pg = api.pg
    await initialize(pg, cash=100000)
    opening, proceed = threading.Event(), threading.Event()
    original_prepare = account_data.prepare_account_inputs

    def slow_prepare(params):
        opening.set()
        assert proceed.wait(15), "audit writer did not release reader"
        return original_prepare(params)

    monkeypatch.setattr(account_data, "prepare_account_inputs", slow_prepare)

    async def replace_checkpoint():
        assert await asyncio.to_thread(opening.wait, 15)
        try:
            async with pg.sessions() as db:
                root = await db.get(SimulationAccount, ROOT)
                state = deepcopy(root.market_state)
                changed = pg.setup.rules.prepare_day(
                    pg.setup.rules.initialize(200000), date(2026, 9, 29)
                )
                state["JP"] = pg.setup.rules.checkpoint(changed)
                root.market_state = state
                await db.commit()
        finally:
            proceed.set()

    writer = asyncio.create_task(replace_checkpoint())
    try:
        response = await api.http.get(
            "/api/v1/simulation/account", params={"market": "JP"}
        )
        await writer
    finally:
        proceed.set()
        if not writer.done():
            await writer
    assert response.status_code == 200, response.text
    public = response.json()["data"]
    assert public["cash"] == 100000 and public["execution_context"][
        "trade_date"
    ] == str(DAY)
    async with pg.sessions() as db:
        saved = (await db.get(SimulationAccount, ROOT)).market_state["JP"]
        assert saved["metadata"]["prepared_date"] == "2026-09-29"
        assert saved["metadata"]["state"]["initial_cash"] == "200000"


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["00000007", "7"])
async def test_legacy_history_blocks_read_and_reset_before_any_mutation(api, user):
    identity = await legacy(api, user=user)
    cache = deepcopy(api.pg.setup.redis.client.values)
    for response in [
        await api.http.get("/api/v1/simulation/account", params={"market": "JP"}),
        await api.http.post("/api/v1/simulation/reset", json=request_body(api)),
    ]:
        assert response.status_code == 409 and "require migration" in response.text
    assert api.pg.setup.redis.client.values == cache and not api.stops.mock_calls
    async with api.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).cash == 250000
        assert (await db.get(JPSimulationSession, identity)).state == {"opaque": "keep"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant,user", [("other", "00000007"), ("test", "8")])
async def test_other_owners_legacy_history_does_not_block_original_actor(
    api, tenant, user
):
    identity = await legacy(api, tenant=tenant, user=user)
    response = await api.http.post("/api/v1/simulation/reset", json=request_body(api))
    assert response.status_code == 200, response.text
    async with api.pg.sessions() as db:
        assert (await db.get(JPSimulationSession, identity)).state == {"opaque": "keep"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["missing", "publication", "day", "fee", "cash", "step"]
)
async def test_invalid_new_inputs_fail_before_original_reset_writes(api, case):
    body = request_body(api)
    if case == "missing":
        body.pop("execution_context")
    if case == "publication":
        body["execution_context"]["data_version"] = "unpublished"
    if case == "day":
        body["execution_context"]["trade_date"] = "2026-10-03"
    if case == "fee":
        body["execution_context"]["commission_rate"] = "NaN"
    if case == "cash":
        body["initial_cash"] = "NaN"
    if case == "step":
        body["initial_cash"] = 1000
    cache = deepcopy(api.pg.setup.redis.client.values)
    response = await api.http.post("/api/v1/simulation/reset", json=body)
    assert response.status_code in {400, 422}, response.text
    assert api.pg.setup.redis.client.values == cache and not api.stops.mock_calls
    async with api.pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.cash == 250000 and root.market_state is None


@pytest.mark.asyncio
async def test_remaining_root_is_not_silently_reused_after_failed_cleanup(api):
    inputs = adapter.DatedAccountInputs(**request_body(api)["execution_context"])
    async with api.pg.sessions() as db:
        context = await adapter.prepare_registered_account_reset(
            "JP", inputs, db=db, tenant_id="test", raw_user_id="00000007", user_id=7
        )
        with pytest.raises(adapter.RegisteredAccountUnavailable, match="cleanup"):
            await context.initialize_after_reset(
                db, api.pg.setup.redis, tenant_id="test", user_id=7, initial_cash=300000
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_initialization_commit_failure_does_not_publish_dated_cash(api):
    cache = deepcopy(api.pg.setup.redis.client.values)
    inputs = adapter.DatedAccountInputs(**request_body(api)["execution_context"])
    async with api.pg.sessions() as db:
        await db.execute(
            delete(SimulationAccount).where(SimulationAccount.account_id == ROOT)
        )
        await db.commit()
        context = await adapter.prepare_registered_account_reset(
            "JP", inputs, db=db, tenant_id="test", raw_user_id="00000007", user_id=7
        )
        await db.execute(
            text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
        )
        with pytest.raises(IntegrityError):
            await context.initialize_after_reset(
                db, api.pg.setup.redis, tenant_id="test", user_id=7, initial_cash=300000
            )
        await db.rollback()
    assert api.pg.setup.redis.client.values == cache
    async with api.pg.sessions() as db:
        assert await db.get(SimulationAccount, ROOT) is None
