"""Dated inputs in the original manual preview, persistence and consumer.

Financial and task writes are restricted to fresh UUID schemas. Model/strategy
fixtures provide explicit inputs; the original planner and order loop run.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from backend.services.live_trading.routers import manual_executions as routes
from backend.services.live_trading.services import manual_execution_context as inputs
from backend.services.live_trading.services import manual_execution_service as manual
from backend.services.live_trading.services.manual_execution_persistence import (
    manual_execution_persistence as persistence,
)
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.fill import SimulationFill
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.order import SimOrder
from backend.services.simulation.models.trade import SimTrade
from backend.services.trade_shared.deps import get_auth_context
from backend.tests.test_market_simulation_cycle import controlled_context, initialize
from backend.tests.test_market_simulation_submission import (
    DAY,
    KEY,
    ROOT,
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    pg as pg_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)

boundary = boundary_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
published = published_fixture
snapshot = snapshot_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


@pytest_asyncio.fixture
async def pipeline(boundary, monkeypatch):
    pg = boundary.pg
    context = controlled_context(pg)

    @asynccontextmanager
    async def sessions(*args, **kwargs):
        async with pg.sessions() as db:
            yield db
            await db.commit()

    for module in (manual, inputs):
        monkeypatch.setattr(module, "get_session", sessions)
    from backend.services.live_trading.services import (
        manual_execution_persistence as store,
    )

    monkeypatch.setattr(store, "get_session", sessions)
    monkeypatch.setattr(manual, "get_redis", lambda: pg.setup.redis)
    monkeypatch.setattr(
        manual, "_get_realtime_price", lambda *args: pytest.fail("realtime lookup")
    )
    monkeypatch.setattr(
        manual.fundamental_aligner,
        "filter_instruments",
        lambda *args, **kwargs: pytest.fail("CN fundamentals"),
    )
    logs = []
    for name in ("update_state", "append_log"):
        monkeypatch.setattr(
            manual.manual_execution_log_stream,
            name,
            lambda **kwargs: logs.append(kwargs),
        )
    await persistence.ensure_tables()
    async with pg.sessions() as db:
        await db.execute(
            text("""CREATE TABLE qm_model_inference_runs (
            run_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, model_id TEXT,
            status TEXT, data_trade_date DATE, prediction_trade_date DATE,
            signals_count INTEGER)""")
        )
        await db.execute(
            text("""CREATE TABLE engine_signal_scores (
            run_id TEXT, tenant_id TEXT, user_id TEXT, symbol TEXT,
            fusion_score DOUBLE PRECISION, light_score DOUBLE PRECISION, tft_score DOUBLE PRECISION,
            score_rank INTEGER, signal_side TEXT, expected_price DOUBLE PRECISION,
            quality TEXT, created_at TIMESTAMPTZ)""")
        )
        await db.execute(
            text("""INSERT INTO qm_model_inference_runs VALUES (
            'native-run', 'test', '00000007', 'model-jp', 'completed', :signal, :trade, 1)"""),
            {"signal": context.signal_input.data_day, "trade": DAY},
        )
        await db.commit()
    strategy = {
        "name": "shared-strategy",
        "is_verified": True,
        "parameters": {
            **context.params,
            "strategy_type": "TopkDropout",
            "topk": 1,
            "n_drop": 1,
        },
    }
    service = manual.ManualExecutionService()
    service._strategy_storage = SimpleNamespace(get=AsyncMock(return_value=strategy))
    state = SimpleNamespace(context=context)

    async def prepare(params, **kwargs):
        assert kwargs == {
            "tenant_id": "test",
            "user_id": "00000007",
            "strategy_id": "2",
            "trade_date": state.context.trade_date,
        }
        assert (
            params["model_id"] == "model-jp"
            and params["data_version"] == context.cash_rules.data_version
        )
        if params.get("prediction_sha256") not in (
            None,
            state.context.signal_input.prediction_sha256,
        ):
            raise ValueError("Pinned prediction snapshot changed")
        if params.get("_model_data_version") not in (None, "trained-version"):
            raise ValueError("Pinned model publication changed")
        return state.context

    monkeypatch.setattr(inputs, "prepare_registered_cycle_context", prepare)
    monkeypatch.setattr(routes, "manual_execution_service", service)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_auth_context] = lambda: SimpleNamespace(
        tenant_id="test", user_id="00000007"
    )
    yield SimpleNamespace(
        pg=pg,
        service=service,
        context=context,
        state=state,
        strategy=strategy,
        logs=logs,
        app=app,
        boundary=boundary,
    )


def request(pipe, **changes):
    return {
        "tenant_id": "test",
        "user_id": "00000007",
        "model_id": "model-jp",
        "run_id": "native-run",
        "strategy_id": "2",
        "trading_mode": "SIMULATION",
        "execution_context": inputs.saved_manual_inputs(pipe.context),
        **changes,
    }


async def task(pipe, task_id):
    return await persistence.get_task(task_id, user_id="00000007", tenant_id="test")


async def count_fills(pipe):
    async with pipe.pg.sessions() as db:
        return len((await db.execute(select(SimulationFill))).scalars().all())


@pytest.mark.asyncio
async def test_original_http_preview_and_persisted_task_execute_and_recover(pipeline):
    pipe = pipeline
    await initialize(pipe.pg, cash=10000)
    cn = pipe.pg.setup.redis.client.values["simulation:account:test:7"]
    cache = deepcopy(pipe.pg.setup.redis.client.values)
    async with pipe.pg.sessions() as db:
        initial = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
    payload = {
        key: value
        for key, value in request(pipe).items()
        if key not in {"tenant_id", "user_id"}
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pipe.app), base_url="http://test"
    ) as client:
        response = await client.post("/manual-executions/preview", json=payload)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["buy_orders"][0]["quantity"] == 100
        assert (
            preview["buy_orders"][0]["order_type"] == "MARKET"
            and preview["buy_orders"][0]["price"] is None
        )
        assert "prediction_sha256" in preview["strategy_context"]["execution_context"]
        assert pipe.pg.setup.redis.client.values == cache
        response = await client.post(
            "/manual-executions",
            json={**payload, "preview_hash": preview["preview_hash"]},
        )
        assert response.status_code == 200, response.text
        created = response.json()
    async with pipe.pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == initial
    saved = await task(pipe, created["task_id"])
    assert saved["status"] == "queued" and saved["request_json"][
        "execution_context"
    ] == inputs.saved_manual_inputs(pipe.context)
    await pipe.service.execute_task_by_id(saved["task_id"])
    completed = await task(pipe, saved["task_id"])
    assert completed["status"] == "completed", completed
    assert completed["success_count"] == 1 and completed["failed_count"] == 0
    assert completed["result_json"]["execution_context"] == pipe.context.provenance()
    assert any(
        "agent_price_mode=dated_next_open" in log.get("line", "") for log in pipe.logs
    )
    assert not any(
        "agent_price_mode=protect_limit" in log.get("line", "") for log in pipe.logs
    )
    assert await count_fills(pipe) == 1
    assert pipe.pg.setup.redis.client.values["simulation:account:test:7"] == cn
    pipe.pg.setup.redis.client.values.pop(KEY)
    after = deepcopy(pipe.pg.setup.redis.client.values)
    async with pipe.pg.sessions() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        root = await db.get(SimulationAccount, ROOT)
        assert (
            root.base_currency == "CNY" and root.market_state["JP"]["cycle_completed"]
        )
        restored = await pipe.context.accounts(db, pipe.pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert (
            restored["cash"] == 0 and restored["positions"]["72030.JP"]["volume"] == 100
        )
    assert pipe.pg.setup.redis.client.values == after


@pytest.mark.asyncio
async def test_original_task_without_preview_uses_same_consumer(pipeline):
    await initialize(pipeline.pg, cash=10000)
    created = await pipeline.service.create_manual_task(**request(pipeline))
    await pipeline.service.execute_task_by_id(created["task_id"])
    assert (await task(pipeline, created["task_id"]))["success_count"] == 1
    assert await count_fills(pipeline) == 1


@pytest.mark.asyncio
async def test_native_negative_score_keeps_original_manual_topk_rule(pipeline):
    await initialize(pipeline.pg, cash=10000)
    pipeline.context.signal_input.frame.loc[0, "score"] = -0.5
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    assert (
        preview["summary"]["signal_count"] == 1
        and preview["buy_orders"][0]["quantity"] == 100
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["missing_run", "unfinished", "model", "unverified", "strategy"]
)
async def test_original_run_model_and_verified_strategy_gates_remain(pipeline, case):
    changes = {}
    if case == "missing_run":
        changes["run_id"] = "pred_parquet_model-jp"
    elif case == "unfinished":
        async with pipeline.pg.sessions() as db:
            await db.execute(
                text("UPDATE qm_model_inference_runs SET status='running'")
            )
            await db.commit()
    elif case == "model":
        changes["model_id"] = "other-model"
    elif case == "unverified":
        pipeline.strategy["is_verified"] = False
    else:
        pipeline.service._strategy_storage.get.return_value = None
    with pytest.raises(HTTPException):
        await pipeline.service.create_manual_task(**request(pipeline, **changes))
    async with pipeline.pg.sessions() as db:
        assert not (
            await db.execute(text("SELECT task_id FROM trade_manual_execution_tasks"))
        ).all()
    assert await count_fills(pipeline) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["signal", "trade", "market", "real", "shadow"])
async def test_new_run_date_and_market_inputs_are_not_rebound(pipeline, case):
    changes = {}
    if case in {"signal", "trade"}:
        column = "data_trade_date" if case == "signal" else "prediction_trade_date"
        async with pipeline.pg.sessions() as db:
            await db.execute(
                text(f"UPDATE qm_model_inference_runs SET {column}=DATE '2026-09-25'")
            )
            await db.commit()
    elif case == "market":
        pipeline.strategy["parameters"]["market"] = "CN"
    else:
        changes["trading_mode"] = case.upper()
    with pytest.raises(HTTPException) as raised:
        await pipeline.service.create_manual_task(**request(pipeline, **changes))
    assert raised.value.status_code == 409
    assert await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_preview_hash_pins_new_publication_and_snapshot(pipeline):
    await initialize(pipeline.pg, cash=10000)
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    altered = {
        **inputs.saved_manual_inputs(pipeline.context),
        "prediction_sha256": "b" * 64,
    }
    with pytest.raises(HTTPException) as raised:
        await pipeline.service.create_manual_task(
            **request(pipeline, execution_context=altered),
            preview_hash=preview["preview_hash"],
        )
    assert raised.value.status_code == 409 and await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_persisted_prediction_change_becomes_failed_task(pipeline):
    await initialize(pipeline.pg, cash=10000)
    created = await pipeline.service.create_manual_task(**request(pipeline))
    pipeline.state.context = replace(
        pipeline.context,
        signal_input=replace(pipeline.context.signal_input, prediction_sha256="b" * 64),
    )
    await pipeline.service.execute_task_by_id(created["task_id"])
    failed = await task(pipeline, created["task_id"])
    assert (
        failed["status"] == "failed" and "snapshot changed" in failed["error_message"]
    )
    assert await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_missing_cash_is_not_initialized_by_manual_task(pipeline):
    cache = deepcopy(pipeline.pg.setup.redis.client.values)
    with pytest.raises(HTTPException):
        await pipeline.service.create_manual_task(**request(pipeline))
    assert (
        await count_fills(pipeline) == 0
        and pipeline.pg.setup.redis.client.values == cache
    )


@pytest.mark.asyncio
async def test_unavailable_registered_fundamentals_never_use_cn(pipeline):
    await initialize(pipeline.pg, cash=10000)
    pipeline.strategy["parameters"]["f_missing_field_max"] = 25
    with pytest.raises(HTTPException, match="unavailable"):
        await pipeline.service.build_execution_preview(**request(pipeline))
    assert await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_original_stock_pool_still_filters_native_manual_rows(
    pipeline, monkeypatch
):
    from backend.shared.stock_pool.resolver import resolver

    monkeypatch.setattr(
        resolver,
        "resolve",
        AsyncMock(
            return_value=SimpleNamespace(
                pool_id="selected-pool",
                checksum="pinned",
                api_symbols=["JP216A0"],
                warnings=[],
                unfiltered=False,
            )
        ),
    )
    rows = await pipeline.service._load_signal_rows(
        tenant_id="test",
        user_id="00000007",
        run_id="native-run",
        model_id="model-jp",
        pool_id="selected-pool",
        cycle_context=pipeline.context,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_new_quote_failure_persists_failed_task_without_money(
    pipeline, monkeypatch
):
    await initialize(pipeline.pg, cash=10000)
    created = await pipeline.service.create_manual_task(**request(pipeline))
    cache = deepcopy(pipeline.pg.setup.redis.client.values)
    monkeypatch.setattr(
        pipeline.pg.setup.source,
        "load_date",
        lambda *args: (_ for _ in ()).throw(ValueError("dated input unavailable")),
    )
    await pipeline.service.execute_task_by_id(created["task_id"])
    failed = await task(pipeline, created["task_id"])
    assert (
        failed["status"] == "failed"
        and failed["error_message"] == "dated input unavailable"
    )
    assert (
        await count_fills(pipeline) == 0
        and pipeline.pg.setup.redis.client.values == cache
    )


@pytest.mark.asyncio
async def test_task_rerun_reuses_original_order_ids_and_fills(pipeline):
    await initialize(pipeline.pg, cash=10000)
    created = await pipeline.service.create_manual_task(**request(pipeline))
    saved = await task(pipeline, created["task_id"])
    await pipeline.service.process_task(saved)
    await pipeline.service.process_task(saved)
    assert await count_fills(pipeline) == 1
    async with pipeline.pg.sessions() as db:
        trades = (await db.execute(select(SimTrade))).scalars().all()
        orders = (await db.execute(select(SimOrder))).scalars().all()
        assert len(trades) == len(orders) == 1


@pytest.mark.asyncio
async def test_original_pg_fusion_rows_take_priority_over_native_file(pipeline):
    await initialize(pipeline.pg, cash=10000)
    async with pipeline.pg.sessions() as db:
        await db.execute(
            text("""INSERT INTO engine_signal_scores
            (run_id, tenant_id, user_id, symbol, fusion_score, signal_side, expected_price)
            VALUES ('native-run', 'test', '00000007', 'JP72030', -0.7, 'buy', 999)""")
        )
        await db.commit()
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    assert preview["buy_orders"][0]["fusion_score"] == -0.7
    assert preview["buy_orders"][0]["reference_price"] == 100
    assert preview["buy_orders"][0]["price"] is None
    assert not preview["summary"]["inferred_signal_plan"]


@pytest.mark.asyncio
async def test_wrong_market_in_original_pg_rows_is_not_replaced_with_file(pipeline):
    await initialize(pipeline.pg, cash=10000)
    async with pipeline.pg.sessions() as db:
        await db.execute(
            text("""INSERT INTO engine_signal_scores
            (run_id, tenant_id, user_id, symbol, fusion_score)
            VALUES ('native-run', 'test', '00000007', 'SH600036', 0.9)""")
        )
        await db.commit()
    with pytest.raises(HTTPException) as raised:
        await pipeline.service.create_manual_task(**request(pipeline))
    assert raised.value.status_code == 409 and await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_registered_fundamentals_use_signal_day_and_original_comparator(
    pipeline, monkeypatch
):
    from backend.services.simulation.jp import feature_snapshot

    await initialize(pipeline.pg, cash=10000)
    calls = []

    def reader(day, symbols, columns):
        calls.append((day, symbols, columns))
        return pd.DataFrame({"pe_ttm": [20]}, index=["JP72030"])

    monkeypatch.setattr(feature_snapshot, "create_reader", lambda spec: reader)
    pipeline.strategy["parameters"]["f_pe_ttm_max"] = 25
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    assert preview["buy_orders"][0]["quantity"] == 100
    assert calls == [(pipeline.context.signal_input.data_day, ["JP72030"], ["pe_ttm"])]


@pytest.mark.asyncio
async def test_unselected_native_rows_do_not_reread_matching_rules(
    pipeline, monkeypatch
):
    await initialize(pipeline.pg, cash=10000)
    context = replace(
        pipeline.context,
        signal_input=replace(
            pipeline.context.signal_input,
            frame=pd.DataFrame(
                [
                    {"symbol": "JP72030", "score": 0.9},
                    {"symbol": "JP216A0", "score": 0.1},
                ]
            ),
        ),
    )
    pipeline.context = pipeline.state.context = context
    calls = []
    original = type(context.execution).matching_rules

    def selected(self, symbol, bar, **kwargs):
        calls.append(self.symbol(symbol))
        return original(self, symbol, bar, **kwargs)

    monkeypatch.setattr(type(context.execution), "matching_rules", selected)
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    assert preview["buy_orders"][0]["quantity"] == 100
    assert calls and set(calls) == {"72030.JP"}


@pytest.mark.asyncio
async def test_selected_native_bar_must_match_execution_date(pipeline, monkeypatch):
    await initialize(pipeline.pg, cash=10000)
    reader = pipeline.context.cash_rules.reader
    original = reader.load_date

    def shifted(day, symbols):
        return {
            symbol: replace(bar, trade_date=date(2026, 9, 29))
            for symbol, bar in original(day, symbols).items()
        }

    monkeypatch.setattr(reader, "load_date", shifted)
    with pytest.raises(HTTPException) as raised:
        await pipeline.service.create_manual_task(**request(pipeline))
    assert raised.value.status_code == 409
    assert await count_fills(pipeline) == 0


@pytest.mark.asyncio
async def test_native_tick_slippage_and_fees_use_original_order_and_cash(pipeline):
    original = pipeline.context
    rules = type(original.cash_rules)(
        original.cash_rules.reader, commission_rate="0.001", slippage_bps="5"
    )
    context = replace(
        original,
        cash_rules=rules,
        params={**original.params, "commission_rate": "0.001", "slippage_bps": "5"},
    )
    pipeline.context = pipeline.state.context = context
    bar = rules.reader.get_bar("JP72030", DAY)
    matching = context.execution.matching_rules("JP72030", bar)
    price = matching.price("buy", bar, rules.match_config)
    fee = matching.fees(100, price, "buy", rules.match_config)[3]
    initial = price * 100 + fee
    async with pipeline.pg.sessions() as db:
        await context.accounts(db, pipeline.pg.setup.redis).initialize(initial, DAY)
        await db.commit()
    created = await pipeline.service.create_manual_task(**request(pipeline))
    await pipeline.service.execute_task_by_id(created["task_id"])
    done = await task(pipeline, created["task_id"])
    assert done["success_count"] == 1 and done["failed_count"] == 0
    async with pipeline.pg.sessions() as db:
        fill = (await db.execute(select(SimulationFill))).scalar_one()
        # Original fill projection columns are Float; dated cash remains Decimal.
        assert Decimal(str(fill.fill_price)) == price
        assert Decimal(str(fill.commission)) == fee
        account = await context.accounts(db, pipeline.pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert account["cash"] == 0


@pytest.mark.asyncio
async def test_final_mark_failure_keeps_committed_fill_and_retry_ids(
    pipeline, monkeypatch
):
    await initialize(pipeline.pg, cash=10000)
    created = await pipeline.service.create_manual_task(**request(pipeline))
    original = type(pipeline.context).finish_day

    async def fail(*args):
        raise ValueError("Exact dated closing mark unavailable")

    monkeypatch.setattr(type(pipeline.context), "finish_day", fail)
    await pipeline.service.execute_task_by_id(created["task_id"])
    failed = await task(pipeline, created["task_id"])
    assert failed["status"] == "failed" and await count_fills(pipeline) == 1
    assert failed["request_json"]["execution_plan"]["buy_orders"][0]["quantity"] == 100
    async with pipeline.pg.sessions() as db:
        assert (
            not (await db.get(SimulationAccount, ROOT))
            .market_state["JP"]
            .get("cycle_completed")
        )
    monkeypatch.setattr(type(pipeline.context), "finish_day", original)
    await persistence.update_task(task_id=created["task_id"], status="queued")
    await pipeline.service.execute_task_by_id(created["task_id"])
    assert (await task(pipeline, created["task_id"]))["status"] == "completed"
    assert await count_fills(pipeline) == 1


@pytest.mark.asyncio
async def test_original_sell_snapshot_buy_cycle_with_dated_share_action(
    pipeline, monkeypatch
):
    from backend.tests.test_market_simulation_submission import service, submit
    from backend.services.simulation.services.corporate_action_service import (
        SimulationCorporateActionService,
    )
    from backend.shared.stock_utils import StockCodeUtil

    # The original root includes a CN lot. Its original aggregate projection
    # needs that market's quote even when applying a JP action. Supply only this
    # controlled CN quote; JP marks must still use the dated reader.
    async def other_market_price(session, symbol):
        assert StockCodeUtil.to_prefix(symbol) == "SH600036"
        return 50.0

    monkeypatch.setattr(
        SimulationCorporateActionService, "_load_latest_price", other_market_price
    )

    await initialize(pipeline.pg, cash=10000)
    async with pipeline.pg.sessions() as db:
        outcome = await submit(service(pipeline.pg, db), client_order_id="prior-fill")
        assert outcome.success
        await pipeline.context.finish_day(
            pipeline.context.accounts(db, pipeline.pg.setup.redis)
        )
        await db.commit()
        conn = await db.connection()
        await conn.run_sync(
            lambda sync: SimulationCorporateAction.__table__.create(
                sync, checkfirst=True
            )
        )
        await db.commit()
    following = date(2026, 9, 29)
    context = replace(
        pipeline.context,
        trade_date=following,
        signal_input=replace(
            pipeline.context.signal_input,
            data_day=DAY,
            frame=pd.DataFrame([{"symbol": "JP216A0", "score": 0.8}]),
        ),
    )
    pipeline.context = pipeline.state.context = context
    async with pipeline.pg.sessions() as db:
        await db.execute(
            text(
                "UPDATE qm_model_inference_runs SET data_trade_date=:signal, prediction_trade_date=:trade"
            ),
            {"signal": DAY, "trade": following},
        )
        await db.commit()
    raw_day = pipeline.pg.setup.source.day

    def controlled(day, symbols=None, *args, **kwargs):
        bars, info = raw_day(day, symbols, *args, **kwargs)
        if day == following and "JP216A0" in bars:
            bars = deepcopy(bars)
            bars["JP216A0"].update(open=50, high=55, low=45, close=50, volume=100000)
        return bars, info

    monkeypatch.setattr(pipeline.pg.setup.source, "day", controlled)
    preview = await pipeline.service.build_execution_preview(**request(pipeline))
    assert preview["sell_orders"][0]["quantity"] == 200
    assert preview["buy_orders"][0]["quantity"] == 200
    created = await pipeline.service.create_manual_task(
        **request(pipeline), preview_hash=preview["preview_hash"]
    )
    await pipeline.service.execute_task_by_id(created["task_id"])
    done = await task(pipeline, created["task_id"])
    assert done["success_count"] == 2 and done["failed_count"] == 0, done
    assert (
        done["result_json"]["preview_summary"]["execution_phase"]
        == "sell_snapshot_poll_buy"
    )
    assert await count_fills(pipeline) == 3
    async with pipeline.pg.sessions() as db:
        account = await context.accounts(db, pipeline.pg.setup.redis).get_account(
            7, tenant_id="test", market="JP"
        )
        assert (
            set(account["positions"]) == {"216A0.JP"}
            and account["positions"]["216A0.JP"]["volume"] == 200
        )
        actions = (await db.execute(select(SimulationCorporateAction))).scalars().all()
        assert len(actions) == 1 and actions[0].symbol == "JP72030"
