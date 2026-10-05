"""JP uses the existing manual task, sandbox and hosted readiness contracts."""

import inspect
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import duckdb
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.live_trading.routers import manual_executions as api
from backend.services.live_trading.routers import real_trading_utils as utils
from backend.services.live_trading.services import manual_execution_service as manual
from backend.services.live_trading.services.internal_strategy_dispatcher import (
    dispatch_internal_strategy_order,
)
from backend.services.simulation.services import local_market_data as local
from backend.services.simulation.services.legacy_jp_state import LegacyJPNativeState
from backend.services.trade.sandbox.context import SandboxContext
from backend.services.trade.services import trading_precheck_service as readiness
from backend.shared.database_manager_v2 import DatabaseConfig
from backend.shared.simulation_account_keys import account_key
from backend.shared.trade_redis_keys import build_trade_account_key
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture

snapshot = snapshot_fixture


@pytest.fixture
def jp_daily(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "jp"
    first = import_jquants_snapshot(snapshot, root)["version"]
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=60,H=61,L=59,C=60 WHERE Date='2026-09-30'"
        )
    newest = import_jquants_snapshot(snapshot, root)["version"]
    assert newest != first
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.setattr(local, "_default_instances", {})
    yield SimpleNamespace(root=root, first=first, newest=newest)


def test_manual_jp_uses_latest_raw_common_bar_without_research_publication(
    jp_daily, monkeypatch
):
    monkeypatch.setattr(
        manual, "_get_quote_redis", lambda: pytest.fail("JP must use local daily data")
    )
    assert manual._get_realtime_price("JP72030") == 60
    assert manual._resolve_board_lot_size("72030.JP") == 100
    plan = manual._build_execution_plan_from_signals(
        market="JP",
        signal_rows=[{"symbol": "7203", "fusion_score": 1}],
        strategy_params={"strategy_type": "TopkDropout", "topk": 1},
        account_snapshot={
            "cash": 12000,
            "positions": {
                "SH600036": {"volume": 100, "available_volume": 100, "price": 20}
            },
        },
    )
    assert plan["sell_orders"] == []
    assert plan["buy_orders"][0]["symbol"] == "72030.JP"
    assert plan["buy_orders"][0]["quantity"] == 200
    assert plan["buy_orders"][0]["order_type"] == "LIMIT"
    assert "execution_context" not in plan
    # A read-only historical data request still works without an execution factory.
    assert (
        LOCAL_MARKET_PROVIDERS["JP"].open(jp_daily.first).data_dir.name
        == jp_daily.first
    )


def test_jp_sell_budget_uses_shared_cash_without_cn_stamp_tax(jp_daily):
    plan = manual._build_execution_plan_from_signals(
        market="JP",
        signal_rows=[{"symbol": "JP216A0", "fusion_score": 1}],
        strategy_params={"strategy_type": "TopkDropout", "topk": 1, "n_drop": 1},
        account_snapshot={
            "cash": 0,
            "positions": {
                "JP72030": {"volume": 100, "available_volume": 100, "price": 60}
            },
        },
    )
    assert plan["sell_orders"][0]["symbol"] == "72030.JP"
    assert plan["buy_orders"][0]["quantity"] == 100
    assert plan["summary"]["estimated_sell_proceeds"] == 6000


def test_sandbox_jp_reads_original_user_account_and_emits_ordinary_signals():
    account = {
        "cash": 1234,
        "total_asset": 9999,
        "positions": {"JP72030": {"volume": 100, "available_volume": 100}},
    }
    context = SandboxContext("private", "777", "2", "run", {"market": "JP"})
    seen = []
    context._redis = SimpleNamespace(
        get=lambda key: seen.append(key) or json.dumps(account)
    )
    assert context.get_cash() == 1234
    assert context.get_position("7203.T")["available_volume"] == 100
    assert seen == [account_key("private", "777")]
    context.order("JP72030", 100, 60, "buy")
    assert "execution_context" not in context.flush_signals()[0]
    context._account_cache = {"_market_cash_rules": {"currency": "JPY"}, "cash": 10}
    with pytest.raises(LegacyJPNativeState):
        context.get_cash()


def test_jp_lifecycle_times_follow_tokyo_continuous_sessions():
    cfg = utils._normalize_live_trade_config(
        {
            "market": "JP",
            "enabled_sessions": ["AM"],
            "sell_time": "09:00",
            "buy_time": "09:05",
        },
        {},
    )
    assert cfg["market"] == "JP" and cfg["enabled_sessions"] == ["AM"]
    assert (
        utils._normalize_live_trade_config(
            {"market": "JP", "sell_time": "15:25", "buy_time": "15:30"}, {}
        )["buy_time"]
        == "15:30"
    )
    for clock in ("12:00", "15:31"):
        with pytest.raises(HTTPException):
            utils._normalize_live_trade_config(
                {"market": "JP", "sell_time": clock, "buy_time": clock}, {}
            )


def test_manual_public_requests_only_need_existing_fields_and_market():
    payload = api.ManualExecutionPreviewRequest(
        model_id="m", run_id="r", strategy_id="2", market="JP"
    )
    assert payload.market == "JP"
    assert "execution_context" not in api.ManualExecutionPreviewRequest.model_fields
    assert (
        "execution_context"
        not in inspect.signature(
            manual.ManualExecutionService.create_manual_task
        ).parameters
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["REAL", "SHADOW"])
async def test_jp_dispatch_never_calls_real_broker(mode):
    with pytest.raises(HTTPException) as error:
        await dispatch_internal_strategy_order(
            order_data={
                "symbol": "7203.T",
                "side": "BUY",
                "quantity": 100,
                "trading_mode": mode,
            },
            user_id="777",
            tenant_id="private",
            redis=None,
            db=None,
        )
    assert error.value.status_code == 400 and "simulation only" in error.value.detail


@pytest_asyncio.fixture
async def pg_consumer(monkeypatch):
    if os.getenv("QM_JP_TEST_PG") != "1":
        pytest.skip("PG consumer verification is opt-in")
    schema = "jp_consumer_" + uuid.uuid4().hex
    tenant = "jp-consumer-" + uuid.uuid4().hex
    uid = str(10000000 + int(uuid.uuid4().hex[:8], 16) % 100000000)
    admin = create_async_engine(DatabaseConfig().get_master_url())
    engine = None
    redis = manual.get_redis()
    key = account_key(tenant, uid)
    from backend.services.simulation.models import Base
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.position_lot import SimulationPositionLot
    from backend.services.simulation.models.corporate_action import (
        SimulationCorporateAction,
    )
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger

    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            DatabaseConfig().get_master_url(),
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as conn:
            for ddl in (
                "CREATE TABLE qm_user_models (model_id TEXT, tenant_id TEXT, user_id TEXT, metadata_json JSONB, status TEXT, is_default BOOLEAN, activated_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)",
                "CREATE TABLE qm_model_inference_runs (run_id TEXT, tenant_id TEXT, user_id TEXT, model_id TEXT, status TEXT, prediction_trade_date DATE, data_trade_date DATE, created_at TIMESTAMPTZ, fallback_used BOOLEAN, model_source TEXT)",
                "CREATE TABLE engine_signal_scores (run_id TEXT, tenant_id TEXT, user_id TEXT, symbol TEXT, fusion_score FLOAT, light_score FLOAT, tft_score FLOAT, score_rank INT, signal_side TEXT, expected_price FLOAT, quality TEXT, created_at TIMESTAMPTZ)",
            ):
                await conn.execute(text(ddl))
            # Public preview now reads the ordinary lot/ledger projection. Build
            # its actual standard schema rather than bypassing that read path.
            tables = [
                model.__table__
                for model in (
                    SimulationAccount,
                    SimulationPositionLot,
                    SimulationCorporateAction,
                    SimulationCashLedger,
                )
            ]
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=tables)
            )
            await conn.execute(
                text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
            )
            params = {
                "tenant": tenant,
                "uid": uid,
                "jp": json.dumps({"market": "JP", "target_horizon_days": 5}),
                "cn": json.dumps({"market": "CN"}),
            }
            await conn.execute(
                text(
                    "INSERT INTO qm_user_models VALUES ('jp', :tenant, :uid, CAST(:jp AS JSONB), 'ready', TRUE, now(), now()), ('cn', :tenant, :uid, CAST(:cn AS JSONB), 'ready', FALSE, now(), now())"
                ),
                params,
            )
            await conn.execute(
                text(
                    "INSERT INTO qm_model_inference_runs VALUES ('run-jp', :tenant, :uid, 'jp', 'completed', '2026-09-30', '2026-09-29', now(), FALSE, 'user_default')"
                ),
                params,
            )
            await conn.execute(
                text(
                    "INSERT INTO engine_signal_scores (run_id,tenant_id,user_id,symbol,fusion_score) VALUES ('run-jp', :tenant, :uid, 'JP72030', 1)"
                ),
                params,
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def session(**kwargs):
            async with sessions() as db:
                yield db
                await db.commit()

        monkeypatch.setattr(manual, "get_session", session)
        redis.client.set(
            key,
            json.dumps(
                {
                    "cash": 12000,
                    "available_cash": 12000,
                    "total_asset": 12000,
                    "base_currency": "CNY",
                    "positions": {},
                }
            ),
            ex=120,
        )
        yield SimpleNamespace(
            sessions=sessions, tenant=tenant, uid=uid, redis=redis, key=key
        )
    finally:
        redis.client.delete(
            key, account_key(tenant, uid, "JP"), build_trade_account_key(tenant, uid)
        )
        keys = list(redis.client.scan_iter(match=f"simulation:*:{tenant}:*"))
        if keys:
            redis.client.delete(*keys)
        if engine:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_pg_manual_preview_and_hosted_window_use_standard_user_default(
    pg_consumer, jp_daily, monkeypatch
):
    pg = pg_consumer
    service = manual.ManualExecutionService()
    monkeypatch.setattr(
        service,
        "_strategy_storage",
        SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "id": "2",
                    "name": "jp",
                    "is_verified": True,
                    "parameters": {"strategy_type": "TopkDropout", "topk": 1},
                }
            )
        ),
    )
    status = await service.get_default_model_hosted_status(
        tenant_id=pg.tenant, user_id=pg.uid
    )
    assert status["latest_default_model_id"] == "jp"
    assert status["latest_run_id"] == "run-jp"
    assert status["execution_window_start"] == "2026-09-30"
    preview = await service.build_execution_preview(
        tenant_id=pg.tenant,
        user_id=pg.uid,
        model_id="jp",
        run_id="run-jp",
        strategy_id="2",
        trading_mode="SIMULATION",
        market="JP",
    )
    assert preview["strategy_context"]["market"] == "JP"
    assert preview["account_snapshot"]["available_cash"] == 12000
    assert preview["buy_orders"][0]["quantity"] == 200
    assert "execution_context" not in preview["strategy_context"]
    async with pg.sessions() as db:
        defaults = (
            await db.execute(
                text("SELECT model_id,is_default FROM qm_user_models ORDER BY model_id")
            )
        ).all()
    assert defaults == [("cn", False), ("jp", True)]


@pytest.mark.asyncio
async def test_standard_sandbox_target_uses_shared_cash_and_jp_lot(
    pg_consumer, jp_daily, monkeypatch
):
    from backend.services.trade.services import sandbox_signal_consumer as consumer

    pg = pg_consumer
    service = consumer.SandboxSignalConsumer()
    monkeypatch.setattr(consumer, "get_session", manual.get_session)
    monkeypatch.setattr(consumer, "redis_client", pg.redis)
    monkeypatch.setattr(
        service,
        "_account_manager",
        SimpleNamespace(
            get_account=AsyncMock(
                return_value={
                    "total_asset": 12000,
                    "cash": 12000,
                    "base_currency": "CNY",
                    "positions": {},
                }
            )
        ),
    )
    create = AsyncMock()
    monkeypatch.setattr(service, "_create_and_execute_order", create)
    await service._handle_order_target_percent(
        {"run_id": "standard-run", "data": {"symbol": "7203.T", "target_percent": 0.5}},
        pg.tenant,
        pg.uid,
        2,
    )
    assert create.await_args.kwargs["symbol"] == "JP72030"
    assert create.await_args.kwargs["quantity"] == 100
    assert create.await_args.kwargs["price"] == 60
    assert "execution_context" not in create.await_args.kwargs


@pytest.mark.asyncio
async def test_internal_sync_reads_same_user_simulation_account(pg_consumer):
    from backend.services.trade.routers import internal_strategy_lifecycle as gateway

    pg = pg_consumer
    async with pg.sessions() as db:
        account = await gateway.sync_account_state(
            x_user_id=pg.uid,
            x_tenant_id=pg.tenant,
            db=db,
            market="JP",
            trading_mode="SIMULATION",
        )
    assert account["cash"] == 12000 and account["market"] == "JP"
    assert account["base_currency"] == "CNY"
    assert "execution_context" not in account


@pytest.mark.asyncio
async def test_jp_precheck_uses_local_daily_and_never_autoselects_default(
    jp_daily, monkeypatch
):
    class Result:
        def mappings(self):
            return self

        def one(self):
            return {
                "sim_orders": True,
                "sim_trades": True,
                "simulation_fund_snapshots": True,
            }

    db = SimpleNamespace(execute=AsyncMock(return_value=Result()), rollback=AsyncMock())
    from backend.shared.model_registry import model_registry_service

    default = AsyncMock(
        return_value={"model_id": "jp", "metadata_json": {"market": "JP"}}
    )
    monkeypatch.setattr(model_registry_service, "get_default_model", default)
    monkeypatch.setattr(
        model_registry_service,
        "list_models",
        AsyncMock(side_effect=AssertionError("no auto default")),
    )
    monkeypatch.setattr(
        readiness, "_is_cn_trading_hours", lambda: pytest.fail("CN clock")
    )
    monkeypatch.setattr(
        utils,
        "check_stream_series_freshness",
        lambda **kwargs: pytest.fail("tick protocol"),
    )
    monkeypatch.setattr(
        readiness,
        "signal_readiness_service",
        SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "available": True,
                    "blocking": False,
                    "trading_permission": "trade_enabled",
                }
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.trade.sandbox.manager",
        SimpleNamespace(
            sandbox_manager=SimpleNamespace(
                _workers={1: SimpleNamespace(is_alive=lambda: True)}
            )
        ),
    )
    kwargs = {
        "mode": "SIMULATION",
        "redis_client": None,
        "user_id": "777",
        "tenant_id": "private",
        "market": "JP",
    }
    assert (await readiness.run_trading_readiness_precheck(db, **kwargs))["passed"]
    default.return_value = None
    assert not (await readiness.run_trading_readiness_precheck(db, **kwargs))["passed"]


@pytest_asyncio.fixture
async def task_pipeline(pg_consumer, jp_daily, monkeypatch):
    """Real task persistence and PG signals, isolated from production/log Redis."""
    from backend.services.live_trading.services import (
        manual_execution_persistence as persistence,
    )

    pg = pg_consumer
    async with pg.sessions() as db:
        await db.execute(
            text("""CREATE TABLE trade_manual_execution_tasks (
          task_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, strategy_id TEXT,
          strategy_name TEXT, run_id TEXT, model_id TEXT, prediction_trade_date DATE,
          trading_mode TEXT, task_type TEXT, task_source TEXT, trigger_mode TEXT,
          trigger_context_json JSONB, strategy_snapshot_json JSONB, parent_runtime_id TEXT,
          status TEXT, stage TEXT, error_stage TEXT, error_message TEXT, progress INT,
          signal_count INT, order_count INT, success_count INT, failed_count INT,
          request_json JSONB, result_json JSONB, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
        )""")
        )
        # Shared cancellation only touches original pending order identifiers.
        await db.execute(
            text(
                "CREATE TABLE orders (client_order_id TEXT, tenant_id TEXT, user_id TEXT, status TEXT)"
            )
        )
        await db.commit()
    monkeypatch.setattr(persistence, "get_session", manual.get_session)
    monkeypatch.setattr(
        manual,
        "manual_execution_log_stream",
        SimpleNamespace(
            update_state=lambda **kwargs: None, append_log=lambda **kwargs: None
        ),
    )
    service = manual.ManualExecutionService()
    strategy = {
        "id": "2",
        "name": "jp",
        "is_verified": True,
        "parameters": {"strategy_type": "TopkDropout", "topk": 1},
    }
    service._strategy_storage = SimpleNamespace(get=AsyncMock(return_value=strategy))
    yield SimpleNamespace(
        pg=pg,
        service=service,
        strategy=strategy,
        request={
            "tenant_id": pg.tenant,
            "user_id": pg.uid,
            "model_id": "jp",
            "run_id": "run-jp",
            "strategy_id": "2",
            "trading_mode": "SIMULATION",
            "market": "JP",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["missing_run", "unfinished", "model", "unverified", "strategy", "REAL", "SHADOW"],
)
async def test_standard_jp_manual_gates_reject_before_persisting(task_pipeline, case):
    pipe = task_pipeline
    request = dict(pipe.request)
    if case == "missing_run":
        request["run_id"] = "missing"
    elif case == "unfinished":
        async with pipe.pg.sessions() as db:
            await db.execute(
                text("UPDATE qm_model_inference_runs SET status='running'")
            )
            await db.commit()
    elif case == "model":
        request["model_id"] = "other"
    elif case == "unverified":
        pipe.strategy["is_verified"] = False
    elif case == "strategy":
        pipe.service._strategy_storage.get.return_value = None
    else:
        request["trading_mode"] = case
    before = pipe.pg.redis.client.get(pipe.pg.key)
    with pytest.raises(HTTPException):
        await pipe.service.create_manual_task(**request)
    async with pipe.pg.sessions() as db:
        assert (
            await db.execute(text("SELECT count(*) FROM trade_manual_execution_tasks"))
        ).scalar_one() == 0
    assert pipe.pg.redis.client.get(pipe.pg.key) == before


@pytest.mark.asyncio
async def test_standard_jp_http_preview_hash_and_task_recovery(
    task_pipeline, monkeypatch
):
    import httpx
    from fastapi import FastAPI
    from backend.services.trade_shared.deps import AuthContext

    pipe = task_pipeline
    monkeypatch.setattr(api, "manual_execution_service", pipe.service)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_auth_context] = lambda: AuthContext(
        user_id=pipe.pg.uid,
        tenant_id=pipe.pg.tenant,
        raw_sub=pipe.pg.uid,
        roles=["user"],
    )
    payload = {
        key: value
        for key, value in pipe.request.items()
        if key not in {"tenant_id", "user_id"}
    }
    before = pipe.pg.redis.client.get(pipe.pg.key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        preview_response = await client.post("/manual-executions/preview", json=payload)
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        bad = await client.post(
            "/manual-executions", json={**payload, "preview_hash": "stale"}
        )
        assert bad.status_code == 409
        response = await client.post(
            "/manual-executions",
            json={**payload, "preview_hash": preview["preview_hash"]},
        )
        assert response.status_code == 200, response.text
        created = response.json()
        recovered = await client.get(f"/manual-executions/{created['task_id']}")
        assert recovered.status_code == 200
    saved = recovered.json()
    assert saved["status"] == "queued" and saved["task_type"] == "manual"
    assert saved["request_json"]["market"] == "JP"
    assert saved["request_json"]["execution_plan"]["buy_orders"][0]["quantity"] == 200
    assert "execution_context" not in saved["request_json"]
    assert pipe.pg.redis.client.get(pipe.pg.key) == before
    # Without preview the original worker recomputes its plan later.
    ordinary = await pipe.service.create_manual_task(**pipe.request)
    assert "execution_plan" not in ordinary["task"]["request_json"]
    assert ordinary["task"]["request_json"]["market"] == "JP"


@pytest.mark.asyncio
async def test_standard_jp_pg_rows_precede_file_and_pool_never_widens(
    task_pipeline, monkeypatch
):
    from backend.shared.stock_pool.resolver import resolver

    pipe = task_pipeline
    fallback = AsyncMock(side_effect=AssertionError("existing PG batch must win"))
    monkeypatch.setattr(pipe.service, "load_pred_parquet_signal_rows", fallback)
    async with pipe.pg.sessions() as db:
        await db.execute(text("UPDATE engine_signal_scores SET fusion_score=-0.7"))
        await db.commit()
    preview = await pipe.service.build_execution_preview(**pipe.request)
    assert preview["buy_orders"][0]["fusion_score"] == -0.7
    assert preview["buy_orders"][0]["quantity"] == 200
    fallback.assert_not_awaited()
    monkeypatch.setattr(
        resolver,
        "resolve",
        AsyncMock(
            return_value=SimpleNamespace(
                pool_id="limited",
                checksum="unit",
                api_symbols=["JP216A0"],
                warnings=[],
                unfiltered=False,
            )
        ),
    )
    rows = await pipe.service._load_signal_rows(
        tenant_id=pipe.pg.tenant,
        user_id=pipe.pg.uid,
        run_id="run-jp",
        model_id="jp",
        pool_id="limited",
    )
    assert rows == []


@pytest.mark.asyncio
async def test_standard_hosted_uses_latest_completed_pg_batch_duplicates_and_noop(
    task_pipeline,
):
    pipe = task_pipeline
    async with pipe.pg.sessions() as db:
        await db.execute(
            text(
                "INSERT INTO qm_model_inference_runs SELECT 'newer-run',tenant_id,user_id,model_id,status,prediction_trade_date,data_trade_date,created_at + INTERVAL '1 minute',fallback_used,model_source FROM qm_model_inference_runs"
            )
        )
        await db.execute(
            text(
                "INSERT INTO engine_signal_scores SELECT 'newer-run',tenant_id,user_id,symbol,fusion_score,light_score,tft_score,score_rank,signal_side,expected_price,quality,created_at FROM engine_signal_scores"
            )
        )
        await db.commit()
    request = {
        key: value
        for key, value in pipe.request.items()
        if key not in {"model_id", "market"}
    }
    request.update(
        run_id="ignored-caller",
        execution_config={"market": "JP"},
        live_trade_config={"market": "JP"},
        task_id="hosted-" + uuid.uuid4().hex,
    )
    created = await pipe.service.create_hosted_task(**request)
    saved = created["task"]
    assert saved["run_id"] == "newer-run" and saved["model_id"] == "jp"
    assert saved["task_type"] == "hosted" and saved["task_source"] == "hosted_runner"
    assert saved["request_json"]["market"] == "JP"
    assert saved["request_json"]["execution_plan"]["buy_orders"][0]["quantity"] == 200
    assert "execution_context" not in saved["request_json"]
    duplicate = await pipe.service.create_hosted_task(**request)
    assert duplicate["duplicate"] and duplicate["task_id"] == saved["task_id"]
    before = json.loads(pipe.pg.redis.client.get(pipe.pg.key))
    pipe.pg.redis.client.set(
        pipe.pg.key, json.dumps({**before, "cash": 10, "available_cash": 10}), ex=120
    )
    cash_state = pipe.pg.redis.client.get(pipe.pg.key)
    request["task_id"] = "hosted-" + uuid.uuid4().hex
    noop = await pipe.service.create_hosted_task(**request)
    assert noop["noop"] and noop["status"] == "completed"
    assert noop["task"]["result_json"]["order_count"] == 0
    assert pipe.pg.redis.client.get(pipe.pg.key) == cash_state


@pytest.mark.asyncio
async def test_standard_jp_worker_intent_fills_through_public_consumer(
    pg_consumer, jp_daily, monkeypatch
):
    from backend.services.simulation.models import Base
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.account_daily import SimulationAccountDaily
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger
    from backend.services.simulation.models.fill import SimulationFill
    from backend.services.simulation.models.order import SimOrder
    from backend.services.simulation.models.order_v2 import SimulationOrderV2
    from backend.services.simulation.models.position_lot import SimulationPositionLot
    from backend.services.simulation.models.trade import SimTrade
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )
    from backend.services.trade.services import sandbox_signal_consumer as consumer
    from backend.services.trade.sandbox import worker
    from sqlalchemy import select

    pg = pg_consumer
    async with pg.sessions() as db:
        await db.execute(text("DROP TABLE simulation_accounts"))
        connection = await db.connection()
        tables = [
            model.__table__
            for model in (
                SimulationAccount,
                SimulationAccountDaily,
                SimulationCashLedger,
                SimulationFill,
                SimulationOrderV2,
                SimulationPositionLot,
                SimOrder,
                SimTrade,
            )
        ]
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
        await db.commit()
    manager = SimulationAccountManager(pg.redis)
    await manager.init_account(int(pg.uid), 12000, pg.tenant, market="JP")
    monkeypatch.setattr(consumer, "get_session", manual.get_session)
    monkeypatch.setattr(consumer, "redis_client", pg.redis)
    service = consumer.SandboxSignalConsumer()
    service._account_manager = manager
    sdk = SandboxContext(pg.tenant, pg.uid, "2", "standard-run", {"market": "JP"})
    sdk._redis = pg.redis.client
    published = []
    monkeypatch.setattr(
        worker, "_publish_signals_to_redis", lambda rows: published.extend(rows)
    )

    def stop_after_tick(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop_after_tick)
    worker._restricted_execute("order_target_percent('7203.T', 0.5)", sdk)
    intent = next(row for row in published if row["type"] == "order_target_percent")
    assert intent["data"]["symbol"] == "JP72030" and "execution_context" not in intent
    await service._process_signal(intent)
    async with pg.sessions() as db:
        order = (await db.execute(select(SimOrder))).scalar_one()
        trade = (await db.execute(select(SimTrade))).scalar_one()
        ledger = (await db.execute(select(SimulationCashLedger))).scalar_one()
        account = (await db.execute(select(SimulationAccount))).scalar_one()
        assert order.quantity == trade.quantity == 100
        assert order.symbol == "JP72030" and order.status.value == "filled"
        assert (
            order.submitted_at.tzinfo is not None
            and trade.executed_at.tzinfo is not None
        )
        assert account.base_currency == ledger.currency == "CNY"
        assert account.cash == pytest.approx(12000 - trade.price * 100)


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "FUTURES", "CRYPTO", "JP"])
def test_common_sdk_cache_and_order_envelopes_keep_original_contract(market):
    from unittest.mock import Mock

    symbol = "JP72030" if market == "JP" else "SH600036"
    sdk = SandboxContext("private", "7", "2", "r", {"market": market})
    redis = Mock()
    redis.get.return_value = json.dumps(
        {
            "cash": 17,
            "total_asset": 19,
            "positions": {symbol: {"volume": 100, "cost": 2}},
        }
    )
    sdk._redis = redis
    assert sdk.get_cash() == 17
    assert sdk.get_position(symbol.lower())["available_volume"] == 100
    sdk.set_time(123)
    sdk.order(symbol, 100, 2, "BUY")
    sdk.order_target_percent(symbol, 0.5)
    signals = sdk.flush_signals()
    assert [row["type"] for row in signals] == ["order", "order_target_percent"]
    assert signals[0]["data"]["order_type"] == "limit"
    assert all(
        row["timestamp"] == 123 and "execution_context" not in row for row in signals
    )
    redis.get.assert_called_once_with("simulation:account:private:7")


@pytest.mark.asyncio
async def test_standard_internal_gateway_keeps_auth_and_jp_simulation_gate(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from unittest.mock import Mock
    from backend.services.trade.routers import internal_strategy_lifecycle as routes
    from backend.services.trade.routers.internal_strategy_utils import (
        INTERNAL_CALL_SECRET,
    )

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: Mock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/sync-account", params={"market": "JP"}, headers={"X-User-Id": "7"}
        )
        assert response.status_code == 401
        for mode in ("REAL", "SHADOW"):
            response = await client.get(
                "/sync-account",
                params={"market": "JP", "trading_mode": mode},
                headers={"X-User-Id": "7", "X-Internal-Call": INTERNAL_CALL_SECRET},
            )
            assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_standard_runtime_restore_preserves_anchor_and_keeps_native_readonly(
    native, monkeypatch
):
    from copy import deepcopy
    from unittest.mock import Mock
    from backend.services.simulation.services.simulation_runtime_restorer import (
        SimulationRuntimeRestorer,
    )
    from backend.services.trade.sandbox import manager
    from backend.shared.simulation_account_keys import active_strategy_key

    payload = {
        "strategy_id": "2",
        "mode": "SIMULATION",
        "code_str": "log('jp')",
        "started_at": "2026-09-30T00:00:00Z",
        "execution_config": {"market": "JP"},
        "live_trade_config": {"market": "JP"},
    }
    if native:
        payload["execution_context"] = {"market": "JP", "trade_date": "2026-09-30"}
    before = deepcopy(payload)
    redis = SimpleNamespace(client=Mock())
    sandbox = SimpleNamespace(
        is_strategy_running=Mock(return_value=False),
        submit_strategy=Mock(return_value="ordinary-worker"),
    )
    monkeypatch.setattr(manager, "sandbox_manager", sandbox)
    restored = await SimulationRuntimeRestorer(redis).restore_active_payload(
        tenant_id="private", user_id="7", active_data=payload
    )
    if native:
        assert not restored and payload == before
        sandbox.submit_strategy.assert_not_called()
        redis.client.set.assert_not_called()
    else:
        assert restored and payload["started_at"] == before["started_at"]
        assert payload["sandbox_restored_run_id"] == "ordinary-worker"
        assert "execution_context" not in sandbox.submit_strategy.call_args.kwargs
        redis.client.set.assert_called_once_with(
            active_strategy_key("private", "7"), json.dumps(payload, ensure_ascii=False)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_marker", "mismatch", "empty", "parquet"])
async def test_standard_jp_readiness_preserves_marker_and_observation_policy(
    task_pipeline, monkeypatch, kind
):
    from unittest.mock import Mock
    from backend.services.live_trading.services import (
        signal_readiness_service as signals,
    )

    pipe = task_pipeline
    monkeypatch.setattr(signals, "manual_execution_service", pipe.service)
    evaluator = signals.SignalReadinessService()
    redis = Mock()
    redis.get.return_value = "old-run" if kind == "mismatch" else None
    monkeypatch.setattr(
        evaluator,
        "_read_signal_latest_key",
        lambda key, client, **kwargs: client.get(key) or "",
    )
    if kind in {"empty", "parquet"}:
        async with pipe.pg.sessions() as db:
            await db.execute(text("DELETE FROM engine_signal_scores"))
            await db.commit()
    pipe.service.load_pred_parquet_signal_rows = AsyncMock(
        return_value=[{"symbol": "JP72030"}] if kind == "parquet" else []
    )
    async with pipe.pg.sessions() as db:
        result = await evaluator.evaluate(
            db,
            redis_client=redis,
            tenant_id=pipe.pg.tenant,
            user_id=pipe.pg.uid,
            mode="SIMULATION",
        )
    assert result["blocking"] is False
    assert result["trading_permission"] == (
        "observe_only" if kind == "empty" else "trade_enabled"
    )
    if kind == "parquet":
        assert result["signal_source_fallback"] == "pred_parquet"
    redis.set.assert_called_once_with(
        f"qm:signal:latest:{pipe.pg.tenant}:{pipe.pg.uid}", "run-jp", ex=86400
    )


@pytest.mark.asyncio
async def test_standard_jp_fundamentals_keep_original_comparator_without_cn(
    task_pipeline, monkeypatch
):
    import pandas as pd
    from backend.services.simulation.jp import feature_snapshot

    pipe = task_pipeline
    calls = []

    def read(day, symbols, columns):
        calls.append((day, symbols, columns))
        return pd.DataFrame({"pe_ttm": [20]}, index=["JP72030"])

    monkeypatch.setattr(feature_snapshot, "JPFeatureSnapshotReader", lambda hub: read)
    pipe.strategy["parameters"]["f_pe_ttm_max"] = 25
    preview = await pipe.service.build_execution_preview(**pipe.request)
    assert preview["buy_orders"][0]["quantity"] == 200
    assert str(calls[0][0]) == "2026-09-30"
    assert calls[0][2] == ["pe_ttm"]
    pipe.strategy["parameters"]["f_pe_ttm_max"] = 10
    with pytest.raises(HTTPException):
        await pipe.service.build_execution_preview(**pipe.request)


@pytest.mark.asyncio
async def test_standard_manual_worker_persists_completion_and_does_not_rerun(
    task_pipeline, monkeypatch
):
    from backend.services.live_trading.services import (
        internal_strategy_dispatcher as dispatcher,
    )

    pipe = task_pipeline
    preview = await pipe.service.build_execution_preview(**pipe.request)
    created = await pipe.service.create_manual_task(
        **pipe.request, preview_hash=preview["preview_hash"]
    )
    dispatch = AsyncMock(
        return_value={
            "status": "success",
            "result": {"success": True},
            "order_id": "isolated",
            "execution": "simulation",
        }
    )
    monkeypatch.setattr(dispatcher, "dispatch_internal_strategy_order", dispatch)
    monkeypatch.setattr(manual, "get_redis", lambda: pipe.pg.redis)
    await pipe.service.execute_task_by_id(created["task_id"])
    saved = await pipe.service.get_task(
        tenant_id=pipe.pg.tenant, user_id=pipe.pg.uid, task_id=created["task_id"]
    )
    assert saved["status"] == "completed" and saved["success_count"] == 1
    assert saved["failed_count"] == 0
    payload = dispatch.await_args.kwargs["order_data"]
    assert payload["symbol"] == "72030.JP" and payload["quantity"] == 200
    assert (
        payload["trading_mode"] == "SIMULATION" and "execution_context" not in payload
    )
    await pipe.service.execute_task_by_id(created["task_id"])
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/preflight", "/trading-precheck"])
async def test_public_readiness_routes_forward_only_ordinary_jp_market(
    endpoint, monkeypatch
):
    import httpx
    from fastapi import FastAPI
    from backend.services.live_trading.routers import real_trading_preflight as routes
    from backend.services.trade_shared.deps import AuthContext

    precheck = AsyncMock(
        return_value={
            "passed": True,
            "checked_at": "2026-10-05T00:00:00Z",
            "items": [],
            "signal_readiness": {},
            "trading_permission": "trade_enabled",
        }
    )
    monkeypatch.setattr(routes, "run_trading_readiness_precheck", precheck)
    monkeypatch.setattr(routes, "_upsert_preflight_snapshot", AsyncMock())
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_auth_context] = lambda: AuthContext(
        user_id="7", tenant_id="private", raw_sub="7", roles=["user"]
    )
    app.dependency_overrides[routes.get_db] = lambda: SimpleNamespace()
    app.dependency_overrides[routes.get_redis] = lambda: SimpleNamespace(
        client=SimpleNamespace()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            endpoint, params={"trading_mode": "SIMULATION", "market": "JP"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["ready" if endpoint == "/preflight" else "passed"]
        for mode in ("REAL", "SHADOW"):
            response = await client.get(
                endpoint, params={"trading_mode": mode, "market": "JP"}
            )
            assert response.status_code == 400
    assert precheck.await_args.kwargs["market"] == "JP"
    assert "execution_context" not in precheck.await_args.kwargs
    precheck.assert_awaited_once()


@pytest.mark.asyncio
async def test_standard_manual_preview_keeps_existing_native_cash_readonly(
    task_pipeline,
):
    pipe = task_pipeline
    native = {
        "cash": 12000,
        "currency": "JPY",
        "data_version": "old",
        "_market_cash_rules": {},
    }
    native_key = account_key(pipe.pg.tenant, pipe.pg.uid, "JP")
    shared_before = pipe.pg.redis.client.get(pipe.pg.key)
    pipe.pg.redis.client.set(native_key, json.dumps(native), ex=120)
    before = pipe.pg.redis.client.get(native_key)
    try:
        with pytest.raises(HTTPException) as exc:
            await pipe.service.build_execution_preview(**pipe.request)
        assert exc.value.status_code == 409
        assert pipe.pg.redis.client.get(native_key) == before
        assert pipe.pg.redis.client.get(pipe.pg.key) == shared_before
    finally:
        pipe.pg.redis.client.delete(native_key)
