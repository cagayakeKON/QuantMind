"""JP inputs execute through the original HTTP API and real PG checkpoints."""

from copy import deepcopy
import asyncio
from datetime import date
import json
from types import SimpleNamespace
import uuid

import duckdb
from fastapi import FastAPI
import httpx
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayTrade,
)
from backend.services.simulation.replay import router, session_context
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.persistence import load_checkpoint_account
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from backend.shared.model_registry import model_registry_service
from backend.tests.test_market_replay_checkpoint import (
    pg as pg_fixture,
    pytestmark,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)

pg = pg_fixture
cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture


@pytest_asyncio.fixture
async def api(pg, snapshot, published, tmp_path, monkeypatch):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "INSERT INTO research.calendar VALUES "
            "('2026-09-22','1'),('2026-09-23','1'),('2026-09-24','1')"
        )
    import_jquants_snapshot(snapshot, published)
    source = open_market_execution_data("JP")
    directory = tmp_path / "model"
    directory.mkdir()
    # Training and execution publications may legitimately differ.
    meta = {
        "context": {"market": "JP"},
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "jp_data_version": pg.setup.source.data_version,
        "train_end": "2026-09-22",
        "val_end": "2026-09-22",
        "target_horizon_days": 1,
    }
    (directory / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    prediction = directory / "pred.parquet"
    pd.DataFrame(
        [
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, day),
                "pred": 1,
                "split": "test",
            }
            for day in (25, 28, 29, 30)
        ]
    ).to_parquet(prediction, index=False)
    calls = []

    async def resolve(**kwargs):
        calls.append(deepcopy(kwargs))
        assert kwargs["market"] == "JP"
        return SimpleNamespace(
            fallback_used=False,
            effective_model_id=kwargs["model_id"] or "saved-jp-model",
            storage_path=str(directory),
            model_source="user",
        )

    monkeypatch.setattr(model_registry_service, "resolve_effective_model", resolve)
    monkeypatch.setattr(
        session_context,
        "ReplayAccountManager",
        lambda session_id, **kwargs: ReplayAccountManager(
            session_id, pg.setup.redis, **kwargs
        ),
    )

    def wrong_calendar(*args, **kwargs):
        raise AssertionError("JP API reached legacy CN calendar")

    monkeypatch.setattr(router, "get_local_market_data", wrong_calendar)
    auth = AuthContext("7", "test-owner", "7", ["user"])

    async def get_test_auth():
        return auth

    async def get_test_db():
        async with pg.sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_auth_context] = get_test_auth
    app.dependency_overrides[get_db] = get_test_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield SimpleNamespace(
            client=client,
            pg=pg,
            source=source,
            directory=directory,
            meta=meta,
            prediction=prediction,
            calls=calls,
            auth=auth,
        )


async def create(api, *, auto=True, **changes):
    request = {
        "name": "market API fixture",
        "model_id": "saved-jp-model",
        "initial_cash": 30000,
        "start_date": "2026-09-28",
        "end_date": "2026-09-29",
        "auto_trade": auto,
        "strategy_params": {
            "market": "jp",
            "topk": 1,
            "max_position_pct": 1,
            "commission_rate": "0",
            "slippage_bps": "0",
        },
    }
    request.update(changes)
    response = await api.client.post("/api/v1/replay/sessions", json=request)
    return response


async def quantities(api, session_id):
    async with api.pg.sessions() as db:
        return [
            (
                await db.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.session_id == uuid.UUID(session_id))
                )
            ).scalar_one()
            for model in (ReplayOrder, ReplayTrade, ReplayEquitySnapshot)
        ]


@pytest.mark.asyncio
async def test_same_http_create_automatic_step_reports_and_restart_recovery(api):
    response = await create(api)
    assert response.status_code == 201, response.text
    created = response.json()
    sid = created["session_id"]
    params = created["strategy_params"]
    assert created["sessions_total"] == 2 and created["next_date"] == "2026-09-28"
    assert (
        params["market"] == "JP" and params["data_version"] == api.source.data_version
    )
    assert params["_model_data_version"] == api.meta["jp_data_version"]
    assert params["_model_data_version"] != params["data_version"]
    assert len(params["prediction_sha256"]) == 64
    assert api.pg.setup.redis.client.values == {}
    assert await quantities(api, sid) == [0, 0, 0]
    response = await api.client.post(f"/api/v1/replay/sessions/{sid}/step")
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["filled"][0]["symbol"] == "JP72030"
    assert first["account"]["currency"] == "JPY"
    assert set(first["account"]["positions"]) == {"JP72030"}
    assert "_market_cash_rules" not in first["account"]
    assert await quantities(api, sid) == [1, 1, 1]
    # Recover using a fresh manager and empty cache, through the same next step.
    api.pg.setup.redis.client.values.clear()
    async with api.pg.sessions() as db:
        row = await db.get(ReplaySession, uuid.UUID(sid))
        context = session_context.open_registered_session_context(row.strategy_params)
        recovered = await load_checkpoint_account(
            db, row, context.accounts(row.session_id)
        )
        assert context.public_account(recovered) == first["account"]
    response = await api.client.post(f"/api/v1/replay/sessions/{sid}/step")
    assert response.status_code == 200, response.text
    assert (
        response.json()["account"]["positions"]["JP72030"]["volume"] >= 200
    )  # dated split
    detail = (await api.client.get(f"/api/v1/replay/sessions/{sid}")).json()
    assert detail["status"] == "finished" and detail["sessions_done"] == 2
    assert detail["next_date"] is None and detail["cursor_date"] == "2026-09-29"
    for route in ("report", "trades", "attribution"):
        result = await api.client.get(f"/api/v1/replay/sessions/{sid}/{route}")
        assert result.status_code == 200, result.text
        if route != "report":
            assert all(item["symbol"] == "JP72030" for item in result.json())
    assert sid in {
        row["session_id"]
        for row in (await api.client.get("/api/v1/replay/sessions")).json()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["7", "0007", "admin"])
async def test_manual_proposals_prefix_codes_confirmation_and_saved_model_owner(
    api, user
):
    api.auth.user_id = user
    created = await create(api, auto=False, model_id=None)
    assert created.status_code == 201, created.text
    row = created.json()
    sid = row["session_id"]
    assert row["model_id"] == "saved-jp-model"
    assert row["strategy_params"]["_model_user_id"] == user
    base = f"/api/v1/replay/sessions/{sid}"
    proposal = await api.client.post(base + "/propose")
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["proposals"][0]["symbol"] == "JP72030"
    assert await quantities(api, sid) == [0, 0, 0]
    assert api.pg.setup.redis.client.values == {}
    cached = await api.client.post(base + "/propose")
    assert cached.json() == proposal.json()
    confirmed = {"symbol": "JP72030", "side": "BUY", "quantity": 100}
    step = await api.client.post(base + "/step", json={"confirmed": [confirmed]})
    assert step.status_code == 200, step.text
    assert step.json()["filled"][0]["quantity"] == 100
    assert step.json()["filled"][0]["symbol"] == "JP72030"
    assert all(item["user_id"] == user for item in api.calls)
    skip = await api.client.post(base + "/step", json={"skip": True})
    assert skip.status_code == 200, skip.text
    assert (
        skip.json()["filled"] == []
        and skip.json()["account"]["positions"]["JP72030"]["volume"] == 200
    )
    assert await quantities(api, sid) == [1, 1, 2]


@pytest.mark.asyncio
async def test_concurrent_same_api_click_is_busy_without_advancing_two_days(
    api, monkeypatch
):
    created = await create(api)
    sid = created.json()["session_id"]
    base = f"/api/v1/replay/sessions/{sid}"
    started, release = asyncio.Event(), asyncio.Event()
    original = session_context.ReplayDayRunner.run_day

    async def held(engine, *args, **kwargs):
        started.set()
        await release.wait()
        return await original(engine, *args, **kwargs)

    monkeypatch.setattr(session_context.ReplayDayRunner, "run_day", held)
    task = asyncio.create_task(api.client.post(base + "/step"))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        duplicate = await api.client.post(base + "/step")
        assert duplicate.status_code == 409, duplicate.text
    finally:
        release.set()
        response = await task
    assert response.status_code == 200, response.text
    assert (await api.client.get(base)).json()["sessions_done"] == 1
    assert await quantities(api, sid) == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["snapshot", "root_commit"])
async def test_actual_api_failure_does_not_publish_financial_cache_or_cursor(
    api, monkeypatch, failure
):
    created = await create(api)
    sid = created.json()["session_id"]
    original = session_context.ReplayDayRunner._write_snapshot

    async def broken(engine, db, *args, **kwargs):
        await original(engine, db, *args, **kwargs)
        if failure == "snapshot":
            await db.flush()
            raise RuntimeError("injected API snapshot failure")
        await db.execute(
            text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
        )

    monkeypatch.setattr(session_context.ReplayDayRunner, "_write_snapshot", broken)
    result = await api.client.post(f"/api/v1/replay/sessions/{sid}/step")
    assert result.status_code == 500, result.text
    assert await quantities(api, sid) == [0, 0, 0]
    assert api.pg.setup.redis.client.values == {}
    detail = (await api.client.get(f"/api/v1/replay/sessions/{sid}")).json()
    assert detail["sessions_done"] == 0 and detail["cursor_date"] is None
    assert detail["next_date"] == "2026-09-28"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["../missing", "", 17])
async def test_invalid_execution_version_fails_before_model_or_financial_writes(
    api, version
):
    result = await create(
        api, strategy_params={"market": "JP", "data_version": version}
    )
    assert result.status_code == 400, result.text
    assert api.calls == [] and api.pg.setup.redis.client.values == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"mode": "code", "strategy_code": "pass"}, {"stop_loss_pct": 0.1}]
)
async def test_missing_code_or_intraday_source_rejected_before_creation(api, change):
    result = await create(api, **change)
    assert result.status_code == 422, result.text
    assert "data adapter" in result.json()["detail"]
    assert api.pg.setup.redis.client.values == {} and api.calls == []
    assert (
        len((await api.client.get("/api/v1/replay/sessions")).json()) == 1
    )  # original test row only


@pytest.mark.asyncio
async def test_required_prediction_replacement_rolls_back_day_not_prior_checkpoint(api):
    response = await create(api)
    assert response.status_code == 201, response.text
    sid = response.json()["session_id"]
    base = f"/api/v1/replay/sessions/{sid}"
    assert (await api.client.post(base + "/step")).status_code == 200
    async with api.pg.sessions() as db:
        checkpoint = (
            await db.execute(
                select(ReplayEquitySnapshot).where(
                    ReplayEquitySnapshot.session_id == uuid.UUID(sid)
                )
            )
        ).scalar_one()
        saved = deepcopy(checkpoint.market_state)
    frame = pd.read_parquet(api.prediction)
    frame["pred"] = 0.123
    frame.to_parquet(api.prediction, index=False)
    result = await api.client.post(base + "/step")
    assert result.status_code == 500 and "saved snapshot" in result.text, result.text
    assert await quantities(api, sid) == [1, 1, 1]
    async with api.pg.sessions() as db:
        checkpoint = (
            await db.execute(
                select(ReplayEquitySnapshot).where(
                    ReplayEquitySnapshot.session_id == uuid.UUID(sid)
                )
            )
        ).scalar_one()
        assert checkpoint.market_state == saved
        row = await db.get(ReplaySession, uuid.UUID(sid))
        assert row.sessions_done == 1 and row.cursor_date == date(2026, 9, 28)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["tenant", "user"])
async def test_original_owner_lookup_still_enforces_tenant_and_integer_alias(
    api, identity
):
    created = await create(api)
    sid = created.json()["session_id"]
    if identity == "tenant":
        api.auth.tenant_id = "other-tenant"
    else:
        api.auth.user_id = "8"
    for method, suffix in [("get", ""), ("post", "/step"), ("post", "/propose")]:
        response = await getattr(api.client, method)(
            f"/api/v1/replay/sessions/{sid}{suffix}"
        )
        assert response.status_code == 404
    assert await quantities(api, sid) == [0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,quantity", [("JP72030", 101), ("SH600036", 100)])
async def test_shared_manual_api_uses_dated_units_and_rejects_foreign_security(
    api, symbol, quantity
):
    created = await create(api, auto=False)
    sid = created.json()["session_id"]
    base = f"/api/v1/replay/sessions/{sid}"
    assert (await api.client.post(base + "/propose")).status_code == 200
    result = await api.client.post(
        base + "/step",
        json={"confirmed": [{"symbol": symbol, "side": "BUY", "quantity": quantity}]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["filled"] == [] and result.json()["rejected"]
    assert result.json()["account"]["cash"] == 30000
    assert await quantities(api, sid) == [0, 0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"context": {"market": "CN"}},
        {"factor_source": "wrong"},
        {"train_end": "2026-09-28"},
        {"jp_data_version": None},
    ],
)
async def test_native_model_market_source_and_label_cutoff_checked_before_create(
    api, change
):
    (api.directory / "metadata.json").write_text(
        json.dumps({**api.meta, **change}), encoding="utf-8"
    )
    result = await create(api)
    assert result.status_code == 400, result.text
    assert api.pg.setup.redis.client.values == {}
    assert len((await api.client.get("/api/v1/replay/sessions")).json()) == 1


@pytest.mark.asyncio
async def test_model_owner_inputs_are_server_derived_and_current_publication_cannot_move_saved_session(
    api, snapshot, published
):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )

    result = await create(
        api,
        strategy_params={
            "market": "JP",
            "topk": 1,
            "max_position_pct": 1,
            "commission_rate": 0,
            "slippage_bps": 0,
            "_model_user_id": "another-user",
            "_model_data_version": "invented",
            "_mode": "code",
            "prediction_sha256": "0" * 64,
        },
    )
    assert result.status_code == 201, result.text
    sid = result.json()["session_id"]
    params = result.json()["strategy_params"]
    assert params["_model_user_id"] == "7" and params["_mode"] == "signals"
    with duckdb.connect(str(snapshot)) as conn:
        # Advance CURRENT to visibly different raw prices after session creation.
        conn.execute(
            "UPDATE research.daily_prices SET O=O*10,H=H*10,L=L*10,C=C*10,Va=Va*10 "
            "WHERE Date='2026-09-28'"
        )
    import_jquants_snapshot(snapshot, published)
    assert open_market_execution_data("JP").data_version != params["data_version"]
    step = await api.client.post(f"/api/v1/replay/sessions/{sid}/step")
    assert step.status_code == 200, step.text
    assert step.json()["filled"][0]["price"] == 100
    assert all(call["user_id"] == "7" for call in api.calls)
