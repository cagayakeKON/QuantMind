"""Registered dated inputs in the original ordinary cycle and scheduled job.

Financial/job writes use fresh UUID schemas and recording Redis fixtures.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from backend.services.simulation import engine as original
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.rebalance_job import SimulationRebalanceJob
from backend.services.simulation.services import hosted_cycle_context as inputs
from backend.services.simulation.services import rebalance_job_service as jobs
from backend.services.simulation.services import (
    simulation_hosted_scheduler as scheduler,
)
from backend.tests.test_market_hosted_execution import (
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    hosted as hosted_fixture,
    pg as pg_fixture,
    pipeline as pipeline_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_hosted_schedule import RedisMemory, config
from backend.tests.test_market_manual_execution import count_fills, initialize, request
from backend.tests.test_market_simulation_cycle import (
    controlled_context,
    original_engine,
)
from backend.tests.test_market_simulation_checkpoint import ROOT, KEY

boundary = boundary_fixture
cash_setup = cash_setup_fixture
hosted = hosted_fixture
pg = pg_fixture
pipeline = pipeline_fixture
published = published_fixture
snapshot = snapshot_fixture
pg_test = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)
JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def context(cash_setup):
    return controlled_context(SimpleNamespace(setup=cash_setup))


def batch(context, **changes):
    row = {
        "symbol": "72030.JP",
        "score": 0.9,
        "trade_date": context.trade_date,
        "run_id": "native-run",
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        **changes,
    }
    return inputs.HostedCycleSignals("native-run", (tuple(row.values()),))


def test_original_context_default_and_bound_pg_inputs_are_distinct(context):
    native = context.signals()
    original_provenance = context.provenance()
    assert context.signal_run_id == "pred_parquet_model-jp"
    bound = replace(context, hosted_signals=batch(context))
    assert bound.signal_run_id == "native-run" and bound.signals()[0].score == 0.9
    assert native[0].score == 0.5 and context.signals() == native
    assert context.provenance() == original_provenance
    assert bound.provenance()["signal_source"] == "engine_signal_scores"
    assert (
        bound.provenance()["prediction_sha256"]
        == original_provenance["prediction_sha256"]
    )
    digest = bound.provenance()["signal_snapshot_sha256"]
    bound.signals()[0].score = 10
    assert (
        bound.signals()[0].score == 0.9
        and bound.provenance()["signal_snapshot_sha256"] == digest
    )
    assert (
        replace(context, hosted_signals=batch(context, score=0.8)).provenance()[
            "signal_snapshot_sha256"
        ]
        != digest
    )
    bound.require_owner(
        context.tenant_id, context.user_id, context.strategy_id, "native-run"
    )
    with pytest.raises(ValueError):
        bound.require_owner(
            context.tenant_id,
            context.user_id,
            context.strategy_id,
            context.signal_run_id,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "other"},
        {"user_id": "8"},
        {"run_id": "other"},
        {"symbol": "SH600036"},
        {"symbol": "JP72030"},
        {"score": float("nan")},
        {"score": float("inf")},
    ],
)
def test_new_signal_binding_rejects_wrong_rows(context, change):
    with pytest.raises(ValueError):
        replace(context, hosted_signals=batch(context, **change))


def test_new_signal_binding_rejects_wrong_day_and_duplicates(context):
    with pytest.raises(ValueError):
        replace(
            context,
            hosted_signals=batch(
                context, trade_date=context.trade_date + timedelta(days=1)
            ),
        )
    rows = batch(context).rows
    with pytest.raises(ValueError):
        replace(
            context, hosted_signals=inputs.HostedCycleSignals("native-run", rows + rows)
        )
    empty = replace(context, hosted_signals=inputs.HostedCycleSignals("native-run", ()))
    assert empty.signals() == [] and empty.signal_run_id == "native-run"


@pytest_asyncio.fixture
async def ordinary(hosted, monkeypatch):
    pipe = hosted

    @asynccontextmanager
    async def sessions(*args, **kwargs):
        async with pipe.pg.sessions() as db:
            yield db
            await db.commit()

    monkeypatch.setattr(inputs, "get_session", sessions)
    monkeypatch.setattr(jobs, "get_session", sessions)
    monkeypatch.setattr(inputs, "manual_execution_service", pipe.service)
    monkeypatch.setattr(
        inputs, "get_strategy_storage_service", lambda: pipe.service._strategy_storage
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_hosted_signal_run_id",
        AsyncMock(side_effect=AssertionError("legacy global model")),
    )
    async with pipe.pg.sessions() as db:
        await db.execute(
            text("ALTER TABLE engine_signal_scores ADD COLUMN trade_date DATE")
        )
        await db.execute(
            text("""INSERT INTO engine_signal_scores
            (run_id, tenant_id, user_id, symbol, fusion_score, trade_date)
            VALUES ('native-run','test','00000007','JP72030',0.9,:day),
                   ('native-run','test','00000007','JP72040',-1,:day),
                   ('other-run','test','00000007','SH600036',10,:day),
                   ('native-run','test','8','JP72030',10,:day)"""),
            {"day": pipe.context.trade_date},
        )
        connection = await db.connection()
        await connection.run_sync(
            lambda sync: SimulationRebalanceJob.__table__.create(sync, checkfirst=True)
        )
        await db.commit()
    pipe.engine = original_engine(pipe.pg, monkeypatch)
    monkeypatch.setattr(original, "simulation_engine", pipe.engine)
    return pipe


def cycle_kwargs(pipe, **changes):
    return {
        "tenant_id": "test",
        "user_id": "00000007",
        "strategy_id": "2",
        "run_id": "ordinary-hosted-1",
        "live_trade_config": config(),
        "execution_context": request(pipe)["execution_context"],
        **changes,
    }


@pg_test
@pytest.mark.asyncio
async def test_original_cycle_uses_pg_fusion_scores_fills_and_recovers(ordinary):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    prepared = await inputs.prepare_hosted_cycle_context(
        request(pipe)["execution_context"],
        tenant_id="test",
        user_id="00000007",
        strategy_id="2",
        config=config(),
    )
    assert [(s.symbol, s.score, s.run_id) for s in prepared.signals()] == [
        ("72030.JP", 0.9, "native-run")
    ]
    cn = deepcopy(pipe.pg.setup.redis.client.values["simulation:account:test:7"])
    result = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert result["status"] == "succeeded" and result["filled_count"] == 1, result
    assert result["signal_count"] == 1 and await count_fills(pipe) == 1
    again = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert again["status"] == "succeeded" and again["order_count"] == 0
    assert await count_fills(pipe) == 1
    assert pipe.pg.setup.redis.client.values["simulation:account:test:7"] == cn
    pipe.pg.setup.redis.client.values.pop(KEY)
    cache = deepcopy(pipe.pg.setup.redis.client.values)
    async with pipe.pg.sessions() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        root = await db.get(SimulationAccount, ROOT)
        assert root.base_currency == "CNY"
        assert root.market_state["JP"]["cycle_inputs"] == prepared.provenance()
        restored = await prepared.accounts(db, pipe.pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert restored == prepared.cash_rules.restore_checkpoint(
            root.market_state["JP"], prepared.trade_date
        )
    assert pipe.pg.setup.redis.client.values == cache


@pg_test
@pytest.mark.asyncio
async def test_original_explicit_empty_batch_does_not_use_native_fallback(ordinary):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    async with pipe.pg.sessions() as db:
        initial = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
        await db.execute(
            text(
                "DELETE FROM engine_signal_scores WHERE user_id='00000007' AND run_id='native-run'"
            )
        )
        await db.commit()
    result = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert result["status"] == "succeeded"
    assert result["signal_count"] == result["order_count"] == 0
    async with pipe.pg.sessions() as db:
        saved = (await db.get(SimulationAccount, ROOT)).market_state
        assert (
            saved["JP"]["metadata"]["state"]["fills"]
            == initial["JP"]["metadata"]["state"]["fills"]
        )
        assert saved["JP"]["cycle_completed"] is True
    assert await count_fills(pipe) == 0


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "wrong_day",
        "wrong_market",
        "wrong_mode",
        "missing_model",
        "fallback",
        "missing_cash",
        "wrong_row_day",
        "duplicate_rows",
        "foreign_row",
    ],
)
async def test_new_inputs_fail_closed_without_legacy_fallback(
    ordinary, failure, monkeypatch
):
    pipe = ordinary
    monkeypatch.setenv("SIM_HOSTED_STRICT_SIGNAL_BATCH", "0")
    kwargs = cycle_kwargs(pipe)
    if failure != "missing_cash":
        await initialize(pipe.pg, cash=30000)
    if failure == "wrong_day":
        kwargs["scheduled_trade_date"] = pipe.context.trade_date + timedelta(days=1)
    elif failure == "wrong_market":
        kwargs["live_trade_config"]["market"] = "CN"
    elif failure == "wrong_mode":
        kwargs["live_trade_config"]["trading_mode"] = "REAL"
    elif failure in {
        "missing_model",
        "fallback",
        "wrong_row_day",
        "duplicate_rows",
        "foreign_row",
    }:
        statement = {
            "missing_model": "DELETE FROM qm_user_models WHERE model_id='model-jp'",
            "fallback": "UPDATE qm_model_inference_runs SET fallback_used=true",
            "wrong_row_day": "UPDATE engine_signal_scores SET trade_date=trade_date+1 WHERE user_id='00000007' AND run_id='native-run'",
            "duplicate_rows": "INSERT INTO engine_signal_scores SELECT * FROM engine_signal_scores WHERE user_id='00000007' AND run_id='native-run'",
            "foreign_row": "UPDATE engine_signal_scores SET symbol='SH600036' WHERE user_id='00000007' AND run_id='native-run'",
        }[failure]
        async with pipe.pg.sessions() as db:
            await db.execute(text(statement))
            await db.commit()
    result = await scheduler.run_simulation_cycle_for_active(**kwargs)
    assert result["status"] in {"skipped", "failed"} and result["error"], result
    assert await count_fills(pipe) == 0


@pg_test
@pytest.mark.asyncio
async def test_bound_pg_inputs_cannot_change_after_day_completion(ordinary):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    assert (await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe)))[
        "status"
    ] == "succeeded"
    async with pipe.pg.sessions() as db:
        await db.execute(
            text(
                "UPDATE engine_signal_scores SET fusion_score=0.8 WHERE user_id='00000007' AND run_id='native-run'"
            )
        )
        await db.commit()
    result = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert result["status"] == "failed" and "different model inputs" in result["error"]
    assert await count_fills(pipe) == 1


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unverified", "missing", "storage_failure"])
async def test_ordinary_strategy_configuration_rules_are_not_manual_task_gates(
    ordinary, kind
):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    if kind == "unverified":
        pipe.strategy["is_verified"] = False
    elif kind == "missing":
        pipe.service._strategy_storage.get.return_value = None
    else:
        pipe.service._strategy_storage.get.side_effect = RuntimeError(
            "controlled lookup failure"
        )
    result = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert result["status"] == "succeeded" and result["filled_count"] == 1, result


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_original_scheduler_persists_job_and_preserves_wall_clock_and_locks(
    ordinary, expired
):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    day = pipe.context.trade_date
    saved_inputs = request(pipe)["execution_context"]
    if expired:
        saved_inputs["trade_date"] = (day - timedelta(days=1)).isoformat()
    payload = {
        "mode": "SIMULATION",
        "strategy_id": "2",
        "started_at": datetime.combine(
            day, datetime.min.time(), tzinfo=JST
        ).isoformat(),
        "execution_config": {"market": "JP"},
        "live_trade_config": config(),
        "execution_context": saved_inputs,
    }
    client = RedisMemory(payload)
    instance = scheduler.SimulationHostedScheduler(SimpleNamespace(client=client))
    now = datetime.combine(day, datetime.min.time(), tzinfo=JST).replace(hour=9)
    result = await instance._process_key("trade:active_strategy:test:0007", now=now)
    assert result is True
    async with pipe.pg.sessions() as db:
        job = (await db.execute(select(SimulationRebalanceJob))).scalar_one()
        assert job.user_id == "00000007" and job.planned_run_at == now.replace(
            hour=8, tzinfo=None
        )
        assert job.status == "succeeded"
    assert await count_fills(pipe) == 1
    assert [entry[0] for entry in client.writes] == (["set"])
    if not expired:
        assert (
            await instance._process_key("trade:active_strategy:test:0007", now=now)
            is False
        )
        assert await count_fills(pipe) == 1


@pg_test
@pytest.mark.asyncio
async def test_original_volume_rejection_is_not_repaired_by_the_adapter(ordinary):
    pipe = ordinary
    await initialize(pipe.pg, cash=1000000)
    result = await scheduler.run_simulation_cycle_for_active(**cycle_kwargs(pipe))
    assert result["status"] == "failed" and result["error"].startswith("no_fill:")
    assert result["order_count"] == result["rejected_count"] == 1
    assert await count_fills(pipe) == 0


@pg_test
@pytest.mark.asyncio
async def test_new_context_uses_original_engine_identity_normalization(ordinary):
    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    result = await scheduler.run_simulation_cycle_for_active(
        **cycle_kwargs(pipe, tenant_id=" test ", user_id=" 00000007 ")
    )
    assert result["status"] == "succeeded" and result["filled_count"] == 1, result
    assert await count_fills(pipe) == 1


@pg_test
@pytest.mark.asyncio
async def test_hosted_next_session_resolves_fresh_artifact_and_preserves_cny(
    ordinary, monkeypatch
):
    import pandas as pd

    pipe = ordinary
    await initialize(pipe.pg, cash=30000)
    startup = request(pipe)["execution_context"]
    first = await scheduler.run_simulation_cycle_for_active(
        **cycle_kwargs(pipe, execution_context=startup)
    )
    assert first["status"] == "succeeded" and await count_fills(pipe) == 1
    cn_cache = pipe.pg.setup.redis.client.values["simulation:account:test:7"]
    day = pipe.context.trade_date + timedelta(days=1)
    next_context = replace(
        pipe.context,
        trade_date=day,
        signal_input=replace(
            pipe.context.signal_input,
            data_day=pipe.context.trade_date,
            frame=pd.DataFrame([{"symbol": "JP72030", "score": 0.7}]),
            prediction_sha256="b" * 64,
        ),
    )
    pipe.context = pipe.state.context = next_context
    original_day = next_context.cash_rules.reader.day

    def read(selected_day, *args, **kwargs):
        bars, metadata = original_day(selected_day, *args, **kwargs)
        if selected_day == day:
            bars = deepcopy(bars)
            for bar in bars.values():
                bar.update(adj_factor=1, ex_rights_type="")
        return bars, metadata

    monkeypatch.setattr(next_context.cash_rules.reader, "day", read)
    async with pipe.pg.sessions() as db:
        await db.execute(
            text(
                "UPDATE qm_model_inference_runs SET run_id='native-next-run', data_trade_date=:signal, prediction_trade_date=:day WHERE run_id='native-run'"
            ),
            {"signal": day - timedelta(days=1), "day": day},
        )
        await db.execute(
            text(
                "UPDATE engine_signal_scores SET run_id='native-next-run', trade_date=:day WHERE run_id='native-run' AND user_id='00000007'"
            ),
            {"day": day},
        )
        await db.commit()
    result = await scheduler.run_simulation_cycle_for_active(
        **cycle_kwargs(
            pipe,
            run_id="ordinary-hosted-2",
            execution_context=startup,
            scheduled_trade_date=day,
        )
    )
    assert result["status"] == "succeeded", result
    assert pipe.pg.setup.redis.client.values["simulation:account:test:7"] == cn_cache
    async with pipe.pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        checkpoint = root.market_state["JP"]
        assert root.cash == 250000 and root.base_currency == "CNY"
        assert checkpoint["cycle_inputs"]["trade_date"] == str(day)
        assert checkpoint["cycle_inputs"]["prediction_sha256"] == "b" * 64
        assert [
            row["trade_date"] for row in checkpoint["metadata"]["state"]["daily"]
        ] == ["2026-09-28", "2026-09-29"]
