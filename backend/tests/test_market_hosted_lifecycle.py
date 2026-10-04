"""Registered dates traverse the original public controller and bootstrap."""

from copy import deepcopy
from datetime import date
import fnmatch
import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
import httpx
import pytest
from sqlalchemy import text

from backend.services.live_trading.routers import real_trading_lifecycle as routes
from backend.services.live_trading.routers import real_trading_utils as utils
from backend.services.live_trading.services import signal_readiness_service as signals
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.services import hosted_cycle_context as hosted_inputs
from backend.services.trade.services import trading_precheck_service as precheck
from backend.services.trade_shared.deps import AuthContext
from backend.services.trade_shared.utils import redis_cache as cache_module
from backend.tests.test_market_hosted_cycle import (
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    hosted as hosted_fixture,
    ordinary as ordinary_fixture,
    pg as pg_fixture,
    pg_test,
    pipeline as pipeline_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_manual_execution import count_fills, initialize, request
from backend.tests.test_market_simulation_checkpoint import ROOT, financial_rows

boundary = boundary_fixture
cash_setup = cash_setup_fixture
hosted = hosted_fixture
ordinary = ordinary_fixture
pg = pg_fixture
pipeline = pipeline_fixture
published = published_fixture
snapshot = snapshot_fixture
ORIGINAL_RESOLVE = routes._resolve_strategy_detail
AUTH = AuthContext("00000007", "test", "7", ["user"])


def dated(**changes):
    return {
        "market": "JP",
        "data_version": "saved-publication",
        "trade_date": "2026-09-28",
        "commission_rate": "0",
        "slippage_bps": "0",
        **changes,
    }


class Redis:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.writes.append((key, deepcopy(value), kwargs))
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = deepcopy(value)
        return True

    def delete(self, *keys):
        return sum(self.values.pop(key, None) is not None for key in keys)

    def keys(self, pattern):
        return [key for key in self.values if fnmatch.fnmatchcase(key, pattern)]

    def eval(self, script, count, key, expected, updated):
        assert count == 1 and "KEEPTTL" in script
        if self.values.get(key) != expected:
            return 0
        self.values[key] = updated
        return 1


@pytest.fixture
def controller(monkeypatch, tmp_path):
    redis = Redis()
    cache = Redis()
    monkeypatch.setattr(cache_module, "redis_client", cache)
    manager = SimpleNamespace(
        submit_strategy=Mock(return_value="worker-runtime"),
        is_strategy_running=Mock(return_value=True),
        _workers={1: SimpleNamespace(is_alive=lambda: True)},
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.services.trade.sandbox.manager",
        SimpleNamespace(sandbox_manager=manager),
    )
    detail = {
        "strategy_name": "shared-strategy",
        "execution_config": utils._default_execution_config(),
        "live_trade_config": utils._default_live_trade_config(),
        "code": "pass\n",
    }
    monkeypatch.setattr(
        routes,
        "_resolve_strategy_detail",
        AsyncMock(side_effect=lambda **kw: deepcopy(detail)),
    )
    readiness = AsyncMock(
        return_value={
            "passed": True,
            "items": [],
            "trading_permission": "trade_enabled",
            "signal_readiness": {},
        }
    )
    monkeypatch.setattr(routes, "run_trading_readiness_precheck", readiness)
    cycle = AsyncMock(
        return_value={
            "status": "succeeded",
            "filled_count": 1,
            "task_id": "bootstrap-task",
        }
    )
    monkeypatch.setattr(routes, "run_simulation_cycle_for_active", cycle)
    monkeypatch.setattr(routes, "get_strategy_path", lambda user: str(tmp_path / user))
    notifications = Mock()
    monkeypatch.setattr(routes, "_schedule_user_notification", notifications)
    service = SimpleNamespace(
        get_default_model_hosted_status=AsyncMock(
            return_value={"available": True, "latest_run_id": "native-run"}
        ),
        get_latest_hosted_task=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(routes, "manual_execution_service", service)
    portfolio = {
        "daily_pnl": 123,
        "daily_return": 0.01,
        "currency": "CNY",
        "aggregate": True,
    }
    snapshot = AsyncMock(return_value=portfolio)
    monkeypatch.setattr(routes, "_fetch_active_portfolio_snapshot", snapshot)
    monkeypatch.setenv("SIM_BOOTSTRAP_FIRST_RUN_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_auth_context] = lambda: AUTH
    app.dependency_overrides[routes.get_redis] = lambda: SimpleNamespace(client=redis)
    app.dependency_overrides[routes.get_db] = lambda: Mock()
    return SimpleNamespace(
        app=app,
        redis=redis,
        cache=cache,
        manager=manager,
        detail=detail,
        readiness=readiness,
        cycle=cycle,
        service=service,
        notifications=notifications,
        portfolio=portfolio,
        snapshot=snapshot,
    )


def form(inputs=None, **changes):
    return {
        "strategy_id": "2",
        "trading_mode": "SIMULATION",
        **({"execution_context": json.dumps(inputs)} if inputs is not None else {}),
        **changes,
    }


@pytest.mark.asyncio
async def test_status_uses_advanced_runtime_inputs_and_declares_delayed_dates(
    controller,
):
    from backend.services.simulation.services.hosted_runtime_context import (
        publish_hosted_runtime_context,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        started = await client.post("/start", data=form(dated()))
        assert started.status_code == 200
        key = utils._active_strategy_key("test", "00000007")
        saved = json.loads(controller.redis.get(key))
        new = {
            **saved["execution_context"],
            "trade_date": "2026-09-29",
            "data_version": "next-publication",
            "prediction_sha256": "b" * 64,
        }
        provenance = {
            "market": "JP",
            "trade_date": "2026-09-29",
            "scheduled_trade_date": "2026-09-30",
            "execution_date_mode": "published_daily_delayed",
            "data_version": "next-publication",
            "prediction_sha256": "b" * 64,
        }
        assert publish_hosted_runtime_context(
            controller.redis,
            key,
            saved,
            {
                "status": "succeeded",
                "runtime_execution_context": new,
                "execution_context": provenance,
            },
        )
        status = await client.get(
            "/status", params={"market": "JP", "execution_context": json.dumps(new)}
        )
        assert status.status_code == 200, status.text
        assert status.json()["execution_context"] == new
        assert status.json()["execution_context_provenance"] == provenance
        assert controller.service.get_default_model_hosted_status.await_args.kwargs[
            "trade_date"
        ] == date(2026, 9, 29)
        stale = await client.get(
            "/status",
            params={
                "market": "JP",
                "execution_context": json.dumps(saved["execution_context"]),
            },
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_public_start_persists_and_forwards_one_date_to_original_runtime_and_bootstrap(
    controller,
):
    controller.detail["code"] = (
        "STRATEGY_CONFIG={'kwargs': {'sell_time':'09:00', 'buy_time':'09:05', 'rebalance_days':1}}\n"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        result = await client.post(
            "/start",
            data=form(dated(), live_trade_config=json.dumps({"rebalance_days": 5})),
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        expected = payload["execution_context"]
        assert expected["market"] == "JP"
        saved = json.loads(
            controller.redis.get(utils._active_strategy_key("test", "00000007"))
        )
        assert saved["execution_context"] == expected
        assert saved["sandbox_run_id"] == "worker-runtime"
        assert (
            saved["runtime_user_id"] == "00000007"
            and saved["runtime_tenant_id"] == "test"
        )
        submitted = controller.manager.submit_strategy.call_args.kwargs
        assert submitted["execution_context"] == expected
        assert (
            submitted["exec_config"]["market"]
            == submitted["live_trade_config"]["market"]
            == "JP"
        )
        assert controller.readiness.await_args.kwargs["execution_context"] == expected
        assert controller.readiness.await_args.kwargs["market"] == "JP"
        assert controller.cycle.await_args.kwargs["execution_context"] == expected
        assert (
            controller.cycle.await_args.kwargs["live_trade_config"]["rebalance_days"]
            == 1
        )
        assert submitted["live_trade_config"]["enabled_sessions"] == ["PM", "AM"]
        status = await client.get(
            "/status",
            params={"market": "JP", "execution_context": json.dumps(expected)},
        )
        assert status.status_code == 200, status.text
        assert (
            status.json()["execution_context"] == expected
            and status.json()["status"] == "running"
        )
        assert status.json()["portfolio"] == controller.portfolio
        controller.service.get_default_model_hosted_status.assert_awaited_once_with(
            tenant_id="test",
            user_id="00000007",
            market="JP",
            trade_date=date(2026, 9, 28),
        )
        again = await client.post("/start", data=form(dated()))
        assert again.status_code == 200
        assert again.json()["bootstrap"]["skipped_reason"] == "bootstrap_lock_exists"
        assert controller.cycle.await_count == 1
        lock_writes = [
            row
            for row in controller.redis.writes
            if row[0].startswith("qm:hosted:simulation:bootstrap:")
        ]
        assert len(lock_writes) == 2 and all(
            row[2] == {"ex": 86400, "nx": True} for row in lock_writes
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrong-market",
        "wrong-mode",
        "malformed",
        "holiday",
        "missing-date",
        "limit-code",
        "wrong-time",
    ],
)
async def test_invalid_registered_start_never_submits_a_worker_or_writes_active_state(
    controller, case
):
    data = form(dated())
    if case == "wrong-market":
        data["execution_config"] = json.dumps({"market": "CN"})
    elif case == "wrong-mode":
        data["trading_mode"] = "REAL"
    elif case == "malformed":
        data["execution_context"] = "not-json"
    elif case == "holiday":
        data["execution_context"] = json.dumps(dated(trade_date="2026-10-03"))
    elif case == "missing-date":
        data = form(execution_config=json.dumps({"market": "JP"}))
    elif case == "limit-code":
        controller.detail["code"] = (
            "STRATEGY_CONFIG={'kwargs': {'order_type':'LIMIT'}}\n"
        )
    elif case == "wrong-time":
        data["live_trade_config"] = json.dumps(
            {"sell_time": "15:25", "buy_time": "15:26"}
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        result = await client.post("/start", data=data)
    assert result.status_code == 400, result.text
    controller.manager.submit_strategy.assert_not_called()
    controller.cycle.assert_not_awaited()
    assert utils._active_strategy_key("test", "00000007") not in controller.redis.values


@pytest.mark.parametrize(
    "day,sell,buy,valid",
    [
        ("2026-09-28", "09:00", "09:05", True),
        ("2026-09-28", "12:30", "12:35", True),
        ("2026-09-28", "15:20", "15:24", True),
        ("2026-09-28", "15:24", "15:25", False),
        ("2026-09-28", "11:25", "11:30", False),
        ("2026-09-28", "12:00", "12:10", False),
        ("2024-11-01", "14:50", "14:59", True),
        ("2024-11-01", "14:50", "15:01", False),
    ],
)
def test_registered_windows_come_from_the_dated_calendar(day, sell, buy, valid):
    kwargs = {"sell_time": sell, "buy_time": buy}
    if valid:
        result = utils._normalize_live_trade_config(
            kwargs, {}, execution_context=dated(trade_date=day)
        )
        assert result["sell_time"] == sell and result["buy_time"] == buy
    else:
        with pytest.raises(HTTPException) as error:
            utils._normalize_live_trade_config(
                kwargs, {}, execution_context=dated(trade_date=day)
            )
        assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_status_keeps_original_cache_key_when_new_query_defaults_are_absent(
    controller,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        first = await client.get("/status")
        again = await client.get("/status")
        assert first.status_code == again.status_code == 200
        assert first.json() == again.json()
        assert "execution_context" not in first.json()
    params = {"user_id": None, "tenant_id": None, "trading_mode": None}
    digest = hashlib.md5(
        json.dumps(params, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    assert list(controller.cache.values) == [f"cache:get_status:test:00000007:{digest}"]
    controller.service.get_default_model_hosted_status.assert_awaited_once_with(
        tenant_id="test", user_id="00000007"
    )


@pytest.mark.asyncio
async def test_status_uses_saved_date_and_keeps_user_global_account_and_controller(
    controller,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        started = await client.post("/start", data=form(dated()))
        assert started.status_code == 200
        same = await client.get("/status")
        assert (
            same.status_code == 200
            and same.json()["execution_context"] == started.json()["execution_context"]
        )
        cn = await client.get("/status", params={"market": "CN"})
        assert cn.status_code == 200 and cn.json()["portfolio"] == controller.portfolio
        assert cn.json()["strategy"] == same.json()["strategy"]
        assert cn.json()["execution_context"] == same.json()["execution_context"]
        assert controller.service.get_default_model_hosted_status.await_args.kwargs == {
            "tenant_id": "test",
            "user_id": "00000007",
        }
        bad = await client.get(
            "/status",
            params={
                "market": "JP",
                "execution_context": json.dumps(dated(trade_date="2026-09-29")),
            },
        )
        assert bad.status_code == 409
        mismatched = await client.get(
            "/status", params={"market": "CN", "execution_context": json.dumps(dated())}
        )
        assert mismatched.status_code == 400


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["filled", "observe", "missing-cash"])
async def test_public_http_actual_readiness_bootstrap_ledger_and_status(
    ordinary, controller, monkeypatch, case
):
    pipe = ordinary
    if case != "missing-cash":
        await initialize(pipe.pg, cash=30000)
    if case == "observe":
        async with pipe.pg.sessions() as db:
            await db.execute(
                text(
                    "DELETE FROM engine_signal_scores WHERE run_id='native-run' AND user_id='00000007'"
                )
            )
            await db.commit()
    async with pipe.pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        original_root = {
            column.name: deepcopy(getattr(root, column.name))
            for column in SimulationAccount.__table__.columns
            if column.name not in {"market_state", "updated_at"}
        }
        initial_market_state = deepcopy(root.market_state)
    initial_financial_rows = await financial_rows(pipe.pg)
    controller.redis.values["qm:signal:latest:test:00000007"] = "native-run"
    cn = deepcopy(pipe.pg.setup.redis.client.values["simulation:account:test:7"])
    monkeypatch.setattr(
        routes,
        "run_trading_readiness_precheck",
        precheck.run_trading_readiness_precheck,
    )
    monkeypatch.setattr(
        routes,
        "run_simulation_cycle_for_active",
        __import__(
            "backend.services.simulation.services.simulation_hosted_scheduler",
            fromlist=["run_simulation_cycle_for_active"],
        ).run_simulation_cycle_for_active,
    )
    monkeypatch.setattr(routes, "_resolve_strategy_detail", ORIGINAL_RESOLVE)
    monkeypatch.setattr(
        routes, "get_strategy_storage_service", lambda: pipe.service._strategy_storage
    )
    monkeypatch.setattr(routes, "manual_execution_service", pipe.service)
    monkeypatch.setattr(signals, "manual_execution_service", pipe.service)
    monkeypatch.setattr(
        precheck, "_is_cn_trading_hours", lambda: pytest.fail("CN clock")
    )
    monkeypatch.setattr(
        utils,
        "check_stream_series_freshness",
        lambda **kw: pytest.fail("realtime fallback"),
    )

    async def session():
        async with pipe.pg.sessions() as db:
            yield db

    controller.app.dependency_overrides[routes.get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(controller.app), base_url="http://audit"
    ) as client:
        result = await client.post(
            "/start", data=form(request(pipe)["execution_context"])
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["signal_readiness"]["latest_run_id"] == "native-run"
        if case == "filled":
            assert (
                payload["bootstrap"]["status"] == "succeeded"
                and payload["bootstrap"]["filled_count"] == 1
            )
            assert await count_fills(pipe) == 1
        else:
            assert await count_fills(pipe) == 0
            if case == "observe":
                assert payload["trading_permission"] == "observe_only"
                assert payload["bootstrap"]["status"] == "succeeded"
                assert payload["bootstrap"]["filled_count"] == 0
            else:
                assert payload["bootstrap"]["status"] == "failed"
        status = await client.get("/status")
        assert status.status_code == 200 and status.json()["status"] == "running"
        assert status.json()["execution_context"] == payload["execution_context"]
        assert status.json()["portfolio"] == controller.portfolio
        assert (
            status.json()["signal_source_status"]["latest_default_model_id"]
            == "model-jp"
        )
        saved = json.loads(
            controller.redis.get(utils._active_strategy_key("test", "00000007"))
        )
        assert (
            saved["sandbox_run_id"] == controller.manager.submit_strategy.return_value
        )
        assert (
            controller.manager.submit_strategy.call_args.kwargs["execution_context"]
            == payload["execution_context"]
        )
    assert pipe.pg.setup.redis.client.values["simulation:account:test:7"] == cn
    if case == "observe":
        expected_cycle = await hosted_inputs.prepare_hosted_cycle_context(
            payload["execution_context"],
            tenant_id="test",
            user_id="00000007",
            strategy_id="2",
            config=controller.manager.submit_strategy.call_args.kwargs[
                "live_trade_config"
            ],
            active_runtime_id=saved["run_id"],
            cycle_run_id=payload["bootstrap"]["task_id"],
        )
        assert expected_cycle.signals() == []
    if case != "filled":
        assert await financial_rows(pipe.pg) == initial_financial_rows
    async with pipe.pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.base_currency == "CNY"
        assert {name: getattr(root, name) for name in original_root} == original_root
        if case == "observe":
            checkpoint = root.market_state["JP"]
            assert checkpoint["cycle_completed"] is True
            assert checkpoint["cycle_inputs"] == expected_cycle.provenance()
            assert checkpoint["metadata"]["prepared_date"] == str(
                expected_cycle.trade_date
            )
            state = checkpoint["metadata"]["state"]
            initial_state = initial_market_state["JP"]["metadata"]["state"]
            # No-signal sessions still close the dated cash journal, but cannot
            # create orders, fills, positions or change the existing cash funds.
            assert {
                key: value
                for key, value in state.items()
                if key not in {"daily", "cursor"}
            } == {
                key: value
                for key, value in initial_state.items()
                if key not in {"daily", "cursor"}
            }
            assert state["cursor"] == str(expected_cycle.trade_date)
            assert state["daily"] == [
                {
                    "trade_date": str(expected_cycle.trade_date),
                    "cash": initial_state["initial_cash"],
                    "settled_cash": initial_state["settled_cash"],
                    "market_value": "0",
                    "equity": initial_state["initial_cash"],
                    "stale_symbols": [],
                    "currency": "JPY",
                }
            ]
        elif case == "missing-cash":
            assert root.market_state == initial_market_state
            assert not root.market_state or "JP" not in root.market_state
