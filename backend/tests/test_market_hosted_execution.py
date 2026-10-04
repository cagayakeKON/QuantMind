"""Registered model/session inputs in the original hosted task and worker.

Financial writes use the existing opt-in fixture's fresh UUID schema only.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.services.live_trading.services import hosted_execution_context as inputs
from backend.services.live_trading.services.manual_execution_service import (
    ManualExecutionService,
)
from backend.services.simulation.jp.schedule import open_schedule_context
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.services.market_schedule import ScheduleDataUnavailable
from backend.services.trade.routers import internal_strategy_lifecycle as routes
from backend.shared import model_registry as models
from backend.tests.test_market_manual_execution import (
    KEY,
    ROOT,
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    count_fills,
    initialize,
    pg as pg_fixture,
    pipeline as pipeline_fixture,
    published as published_fixture,
    request,
    snapshot as snapshot_fixture,
    task,
)

boundary = boundary_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
pipeline = pipeline_fixture
published = published_fixture
snapshot = snapshot_fixture
pg_test = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


@pytest.fixture
def status_service(monkeypatch):
    service = ManualExecutionService()
    default = {
        "model_id": "model-jp",
        "metadata_json": {"market": "JP", "target_horizon_days": 5},
        "status": "ready",
    }
    run = {
        "run_id": "native-run",
        "model_id": "model-jp",
        "effective_model_id": "model-jp",
        "data_trade_date": date(2026, 9, 18),
        "prediction_trade_date": date(2026, 9, 24),
        "model_source": "user_default",
        "fallback_used": False,
    }
    getter = AsyncMock(return_value=default)
    monkeypatch.setattr(inputs.model_registry_service, "get_default_model", getter)
    monkeypatch.setattr(
        service, "_load_default_model_inference_run_for_session", AsyncMock(return_value=run)
    )
    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(side_effect=AssertionError("legacy global default")),
    )
    monkeypatch.setattr(
        service,
        "_resolve_hosted_execution_window",
        lambda **kwargs: pytest.fail("CN calendar"),
    )
    return SimpleNamespace(service=service, default=default, run=run, getter=getter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,reason",
    [
        (date(2026, 9, 18), "window_pending"),
        (date(2026, 9, 24), "ready"),
        (date(2026, 9, 25), "dated_signal_session_mismatch"),
        (date(2026, 10, 1), "window_expired"),
        (date(2045, 1, 2), "market_inputs_unavailable"),
    ],
)
async def test_status_uses_registered_holidays_and_exact_execution_day(
    status_service, day, reason
):
    state = status_service
    result = await state.service.get_default_model_hosted_status(
        tenant_id="test",
        user_id="00000007",
        market="jp",
        trade_date=day,
    )
    assert result["reason_code"] == reason
    state.getter.assert_awaited_once_with(
        tenant_id="test", user_id="00000007", market="JP"
    )
    if reason != "market_inputs_unavailable":
        assert result["execution_window_start"] == "2026-09-24"
        assert result["execution_window_end"] == "2026-09-30"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"fallback_used": True}, "fallback_used"),
        ({"model_source": "unknown"}, "source_mismatch"),
        ({"model_source": "explicit_model_id"}, "ready"),
        ({"model_source": "strategy_binding"}, "ready"),
        ({"model_source": None}, "ready"),
        ({"data_trade_date": date(2026, 9, 21)}, "market_inputs_unavailable"),
        ({"prediction_trade_date": date(2026, 9, 25)}, "dated_signal_session_mismatch"),
    ],
)
async def test_registered_status_preserves_original_source_gates(
    status_service, change, reason
):
    status_service.run.update(change)
    result = await status_service.service.get_default_model_hosted_status(
        tenant_id="test",
        user_id="00000007",
        market="JP",
        trade_date=date(2026, 9, 24),
    )
    assert result["reason_code"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata,reason",
    [
        ({"market": "CN"}, "market_inputs_unavailable"),
        ({"market": "JP", "system_default": True}, "missing_default_model"),
    ],
)
async def test_model_market_and_original_system_default_exclusion(
    status_service, metadata, reason
):
    status_service.default["metadata_json"] = metadata
    result = await status_service.service.get_default_model_hosted_status(
        tenant_id="test",
        user_id="00000007",
        market="JP",
        trade_date=date(2026, 9, 24),
    )
    assert result["reason_code"] == reason


def test_registered_window_does_not_fallback_beyond_calendar():
    context = open_schedule_context()
    with pytest.raises(ScheduleDataUnavailable):
        context.shift_sessions(context.calendar.last_session.date(), 1)
    with pytest.raises(ScheduleDataUnavailable):
        context.shift_sessions(context.calendar.first_session.date(), -1)


@pytest_asyncio.fixture
async def hosted(pipeline, monkeypatch):
    pipe = pipeline

    @asynccontextmanager
    async def sessions(*args, **kwargs):
        async with pipe.pg.sessions() as db:
            yield db
            await db.commit()

    monkeypatch.setattr(models, "get_session", sessions)
    async with pipe.pg.sessions() as db:
        await db.execute(
            text("""CREATE TABLE qm_user_models (
            tenant_id TEXT, user_id TEXT, model_id TEXT PRIMARY KEY, source_run_id TEXT,
            status TEXT, storage_path TEXT, model_file TEXT, metadata_json JSONB,
            metrics_json JSONB, is_default BOOLEAN, created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ, activated_at TIMESTAMPTZ)""")
        )
        await db.execute(
            text("""INSERT INTO qm_user_models
            (tenant_id, user_id, model_id, status, metadata_json, is_default)
            VALUES ('test', :user, :model, 'ready', CAST(:metadata AS JSONB), :default)"""),
            [
                {
                    "user": user,
                    "model": model,
                    "metadata": json.dumps(metadata),
                    "default": default,
                }
                for user, model, metadata, default in (
                    (
                        "00000007",
                        "model-jp",
                        {
                            "market": "JP",
                            "market_default": True,
                            "target_horizon_days": 5,
                        },
                        False,
                    ),
                    ("00000007", "model-cn", {"market": "CN"}, True),
                    (
                        "8",
                        "other-user-jp",
                        {"market": "JP", "market_default": True},
                        False,
                    ),
                )
            ],
        )
        await db.execute(
            text("""ALTER TABLE qm_model_inference_runs
            ADD COLUMN created_at TIMESTAMPTZ DEFAULT now(),
            ADD COLUMN model_source TEXT DEFAULT 'user_default',
            ADD COLUMN effective_model_id TEXT,
            ADD COLUMN fallback_used BOOLEAN DEFAULT false""")
        )
        await db.commit()
    monkeypatch.setattr(routes, "manual_execution_service", pipe.service)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.verify_internal_call] = lambda: None
    pipe.hosted_app = app
    return pipe


def hosted_request(pipe, **changes):
    kwargs = request(pipe, **changes)
    kwargs.pop("model_id")
    return {
        **kwargs,
        "run_id": "ignored-caller-run",
        "task_id": "hosted-shared-1",
        "execution_config": {"market": "JP", "trading_mode": "SIMULATION"},
        "live_trade_config": {"market": "JP"},
    }


@pg_test
@pytest.mark.asyncio
async def test_original_internal_route_worker_duplicate_and_recovery(hosted):
    pipe = hosted
    await initialize(pipe.pg, cash=10000)
    root_cache = deepcopy(
        pipe.pg.setup.redis.client.values["simulation:account:test:7"]
    )
    async with pipe.pg.sessions() as db:
        initial = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    payload = {
        key: value
        for key, value in hosted_request(pipe).items()
        if key not in {"tenant_id", "user_id"}
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pipe.hosted_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/hosted-executions",
            json=payload,
            headers={"x-user-id": "00000007", "x-tenant-id": "test"},
        )
        assert response.status_code == 200, response.text
    created = await task(pipe, "hosted-shared-1")
    assert created["run_id"] == "native-run" and created["model_id"] == "model-jp"
    assert (
        created["task_type"] == "hosted" and created["task_source"] == "hosted_runner"
    )
    assert created["request_json"]["execution_plan"]["buy_orders"][0]["price"] is None
    assert (
        created["request_json"]["execution_context"]["prediction_sha256"]
        == pipe.context.signal_input.prediction_sha256
    )
    async with pipe.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == initial
    await pipe.service.execute_task_by_id(created["task_id"])
    finished = await task(pipe, created["task_id"])
    assert finished["status"] == "completed" and finished["success_count"] == 1, (
        finished
    )
    duplicate = await pipe.service.create_hosted_task(**hosted_request(pipe))
    assert duplicate["duplicate"] is True
    await pipe.service.execute_task_by_id(created["task_id"])
    assert await count_fills(pipe) == 1
    assert pipe.pg.setup.redis.client.values["simulation:account:test:7"] == root_cache
    pipe.pg.setup.redis.client.values.pop(KEY)
    cache = deepcopy(pipe.pg.setup.redis.client.values)
    async with pipe.pg.sessions() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        root = await db.get(SimulationAccount, ROOT)
        restored = await pipe.context.accounts(db, pipe.pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert restored == pipe.context.cash_rules.restore_checkpoint(
            root.market_state["JP"], pipe.context.trade_date
        )
        assert restored["positions"]["72030.JP"]["volume"] == 100
    assert pipe.pg.setup.redis.client.values == cache
    cn = await models.model_registry_service.get_default_model(
        tenant_id="test", user_id="00000007", market="CN"
    )
    assert cn["model_id"] == "model-cn" and cn["is_default"] is True


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other"),
        ("user_id", "8"),
        ("strategy_id", "3"),
        ("market", "CN"),
        ("trade_date", "2026-10-01"),
        ("prediction_sha256", "b" * 64),
    ],
)
async def test_new_dated_duplicate_cannot_return_another_execution(
    hosted, field, value
):
    pipe = hosted
    await initialize(pipe.pg, cash=10000)
    kwargs = hosted_request(pipe)
    await pipe.service.create_hosted_task(**kwargs)
    if field in kwargs:
        kwargs[field] = value
    else:
        kwargs["execution_context"][field] = value
    with pytest.raises(HTTPException) as error:
        await pipe.service.create_hosted_task(**kwargs)
    assert error.value.status_code == 409
    assert await count_fills(pipe) == 0


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "REAL",
        "SHADOW",
        "market_config",
        "mode_config",
        "missing_model",
        "fallback_run",
        "unverified",
        "missing_cash",
    ],
)
async def test_hosted_new_input_failures_do_not_persist_or_trade(hosted, failure):
    pipe = hosted
    kwargs = hosted_request(pipe)
    if failure != "missing_cash":
        await initialize(pipe.pg, cash=10000)
    if failure in {"REAL", "SHADOW"}:
        kwargs["trading_mode"] = failure
    elif failure == "market_config":
        kwargs["live_trade_config"] = {"market": "CN"}
    elif failure == "mode_config":
        kwargs["execution_config"]["trading_mode"] = "REAL"
    elif failure == "unverified":
        pipe.strategy["is_verified"] = False
    elif failure in {"missing_model", "fallback_run"}:
        async with pipe.pg.sessions() as db:
            statement = (
                "DELETE FROM qm_user_models WHERE model_id='model-jp'"
                if failure == "missing_model"
                else "UPDATE qm_model_inference_runs SET fallback_used=true"
            )
            await db.execute(text(statement))
            await db.commit()
    with pytest.raises(HTTPException):
        await pipe.service.create_hosted_task(**kwargs)
    assert await task(pipe, "hosted-shared-1") is None
    assert await count_fills(pipe) == 0


@pg_test
@pytest.mark.asyncio
async def test_original_hosted_noop_remains_completed_without_funding_changes(hosted):
    pipe = hosted
    await initialize(pipe.pg, cash=10)
    async with pipe.pg.sessions() as db:
        initial = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    created = await pipe.service.create_hosted_task(**hosted_request(pipe))
    assert created["noop"] is True and created["status"] == "completed"
    await pipe.service.execute_task_by_id(created["task_id"])
    assert await count_fills(pipe) == 0
    async with pipe.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == initial
