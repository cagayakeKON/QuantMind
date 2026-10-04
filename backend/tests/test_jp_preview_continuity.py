"""Preview and sandbox inputs consume intermediate events without finance writes."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import os

import duckdb
import pytest

from backend.services.live_trading.services import manual_execution_context as manual
from backend.services.simulation.models.account import SimulationAccount
from backend.services.trade.services import sandbox_execution_inputs as sandbox
from backend.tests.test_market_manual_execution import (
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    pg as pg_fixture,
    pipeline as pipeline_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_sandbox_execution import active, sdk, signal
from backend.tests.test_market_simulation_checkpoint import (
    ROOT,
    bar,
    engine,
    financial_rows,
    initialize,
    order,
)

boundary = boundary_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
pipeline = pipeline_fixture
published = published_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


@pytest.fixture
def snapshot(tmp_path):
    source = snapshot_fixture.__wrapped__(tmp_path)
    # Preserve the real split on 29th; the 30th is a covered ordinary day.
    with duckdb.connect(str(source)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1, ExRT='' WHERE Date='2026-09-30'"
        )
    return source


@pytest.mark.asyncio
async def test_manual_and_sandbox_project_skipped_split_session_without_writes(
    pipeline, monkeypatch
):
    pipe = pipeline
    await initialize(pipe.pg)
    async with pipe.pg.sessions() as db:
        row = await order(db)
        execution = engine(pipe.pg, db)
        fill = await execution.execute_from_bar(row, bar(pipe.pg), "JP")
        assert fill.success
        await execution.apply_filled(row, fill)
        await db.commit()
    before = await financial_rows(pipe.pg)
    cache = deepcopy(pipe.pg.setup.redis.client.values)
    async with pipe.pg.sessions() as db:
        checkpoint = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    future = replace(
        pipe.context,
        trade_date=date(2026, 9, 30),
        signal_input=replace(pipe.context.signal_input, data_day=date(2026, 9, 29)),
    )
    projected = await manual.read_manual_snapshot(
        future, tenant_id="test", user_id="00000007", redis=pipe.pg.setup.redis
    )
    assert projected["positions"]["72030.JP"]["volume"] == 200
    assert projected["cash"] == 20000
    worker = sdk(manual.saved_manual_inputs(future))
    monkeypatch.setattr(sandbox, "get_session", manual.get_session)
    inputs = await sandbox.prepare_sandbox_order_inputs(
        signal(worker), active(worker), pipe.pg.setup.redis
    )
    assert inputs.account["positions"]["72030.JP"]["volume"] == 200
    assert inputs.account["cash"] == projected["cash"]
    assert inputs.account_context.rules.backtest_state(inputs.account)[
        "applied_actions"
    ]
    assert await financial_rows(pipe.pg) == before
    assert pipe.pg.setup.redis.client.values == cache
    async with pipe.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == checkpoint
