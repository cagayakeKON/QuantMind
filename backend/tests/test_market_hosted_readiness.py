"""Dated inputs in the original readiness service and ordinary HTTP routes."""

from datetime import date, timedelta
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import text

from backend.services.live_trading.routers import real_trading_preflight as routes
from backend.services.live_trading.services import hosted_readiness_inputs as inputs
from backend.services.live_trading.services import signal_readiness_service as signals
from backend.services.simulation.services.account_context import (
    SimulationAccountContext,
)
from backend.services.trade.services import trading_precheck_service as precheck
from backend.services.trade_shared.deps import AuthContext
from backend.shared import model_registry as models
from backend.tests.test_market_hosted_execution import (
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    hosted as hosted_fixture,
    pg as pg_fixture,
    pipeline as pipeline_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_manual_execution import request
from backend.tests.test_market_replay_cash import DAY

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


def saved_inputs(**changes):
    return {
        "market": "JP",
        "data_version": "quote-version",
        "trade_date": str(DAY),
        **changes,
    }


class Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row

    def first(self):
        return self.row


class Database:
    def __init__(self, count=1):
        self.count = count
        self.calls = []
        self.rollback = AsyncMock()

    async def execute(self, sql, params=None):
        query = str(sql)
        self.calls.append((query, params))
        if "to_regclass" in query:
            return Result(
                {
                    "sim_orders": True,
                    "sim_trades": True,
                    "simulation_fund_snapshots": True,
                }
            )
        if "COUNT(*)" in query:
            return Result({"cnt": self.count})
        assert query.strip() == "SELECT 1"
        return Result({"ok": 1})


class Redis:
    def __init__(self, marker="current-run"):
        self.values = {"qm:signal:latest:test:00000007": marker}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.writes.append((key, value, kwargs))
        self.values[key] = value


@pytest.fixture
def quote_context(monkeypatch):
    bar = SimpleNamespace(symbol="72030.JP", trade_date=DAY, open=100, suspended=False)
    reader = SimpleNamespace(
        calendar=SimpleNamespace(sessions=[DAY]),
        load_date=lambda day: {bar.symbol: bar},
    )
    rules = SimpleNamespace(market="JP", reader=reader, data_version="quote-version")
    context = SimulationAccountContext("JP", DAY, rules)
    factory = SimpleNamespace(prepare_inputs=lambda params: context)
    monkeypatch.setattr(
        inputs, "registered_account_input_adapter", lambda market: factory
    )
    return SimpleNamespace(
        context=context, rules=rules, bar=bar, reader=reader, factory=factory
    )


@pytest.fixture
def service(monkeypatch):
    model = {
        "model_id": "model-jp",
        "status": "ready",
        "metadata_json": {"market": "JP"},
    }
    getter = AsyncMock(return_value=model)
    listing = AsyncMock(return_value=[model])
    setter = AsyncMock(return_value=model)
    for name, method in (
        ("get_default_model", getter),
        ("list_models", listing),
        ("set_default_model", setter),
    ):
        monkeypatch.setattr(models.model_registry_service, name, method)
    worker = SimpleNamespace(is_alive=lambda: True)
    sandbox = SimpleNamespace(_workers={1: worker})
    monkeypatch.setitem(
        sys.modules,
        "backend.services.trade.sandbox.manager",
        SimpleNamespace(sandbox_manager=sandbox),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "wall clock/realtime must not replace dated opening inputs"
        )

    monkeypatch.setattr(precheck, "_is_cn_trading_hours", forbidden)
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils.check_stream_series_freshness",
        forbidden,
    )
    status = {
        "available": True,
        "reason_code": "ready",
        "message": "ready",
        "latest_run_id": "current-run",
        "latest_default_model_id": "model-jp",
    }
    hosted = SimpleNamespace(
        get_default_model_hosted_status=AsyncMock(return_value=status),
        load_pred_parquet_signal_rows=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(signals, "manual_execution_service", hosted)
    evaluator = signals.SignalReadinessService()
    monkeypatch.setattr(
        evaluator,
        "_read_signal_latest_key",
        lambda key, redis, **kwargs: str(redis.get(key) or ""),
    )
    monkeypatch.setattr(precheck, "signal_readiness_service", evaluator)
    return SimpleNamespace(
        model=model,
        getter=getter,
        listing=listing,
        setter=setter,
        sandbox=sandbox,
        status=status,
        hosted=hosted,
        evaluator=evaluator,
    )


def kwargs(**changes):
    return {
        "mode": "SIMULATION",
        "redis_client": Redis(),
        "tenant_id": "test",
        "user_id": "00000007",
        "market": "JP",
        "execution_context": saved_inputs(),
        **changes,
    }


def test_dated_query_parsing_keeps_missing_inputs_and_normalizes_market():
    assert inputs.parse_hosted_readiness_inputs(None, mode="REAL", market="CN") is None
    parsed = inputs.parse_hosted_readiness_inputs(
        json.dumps(saved_inputs(market=" jp ")), mode="SIMULATION", market="JP"
    )
    assert parsed.market == "JP" and parsed.trade_date == DAY


@pytest.mark.parametrize(
    "raw,mode,market",
    [
        ("{", "SIMULATION", "JP"),
        ("null", "SIMULATION", "JP"),
        ("[]", "SIMULATION", "JP"),
        (saved_inputs(), "REAL", "JP"),
        (saved_inputs(), "SHADOW", "JP"),
        (saved_inputs(), "SIMULATION", "CN"),
        (saved_inputs(trade_date="bad"), "SIMULATION", "JP"),
        (saved_inputs(commission_rate=-1), "SIMULATION", "JP"),
    ],
)
def test_invalid_new_query_inputs_are_explicit_400(raw, mode, market):
    with pytest.raises(routes.HTTPException) as error:
        routes._dated_readiness_kwargs(raw, mode=mode, market=market)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opening,suspended,available",
    [
        (100, False, True),
        (0, False, False),
        (float("nan"), False, False),
        (float("inf"), False, False),
        (100, True, False),
    ],
)
async def test_date_quotes_do_not_substitute_missing_prices(
    quote_context, opening, suspended, available
):
    quote_context.bar.open, quote_context.bar.suspended = opening, suspended
    result = await inputs.check_hosted_dated_quotes(
        inputs.parse_hosted_readiness_inputs(
            saved_inputs(), mode="SIMULATION", market="JP"
        )
    )
    assert result["ok"] is available
    assert str(DAY) in result["message"] and "daily_open" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["date", "security", "version", "factory", "unsupported"]
)
async def test_dated_quote_context_cannot_cross_boundaries(
    quote_context, bad, monkeypatch
):
    if bad == "date":
        quote_context.bar.trade_date += timedelta(days=1)
    elif bad == "security":
        quote_context.bar.symbol = "600036.SH"
    elif bad == "version":
        quote_context.rules.data_version = "other-version"
    elif bad == "factory":
        quote_context.factory.prepare_inputs = lambda params: object()
    else:
        monkeypatch.setattr(
            inputs, "registered_account_input_adapter", lambda market: None
        )
    with pytest.raises(ValueError):
        await inputs.check_hosted_dated_quotes(
            inputs.parse_hosted_readiness_inputs(
                saved_inputs(), mode="SIMULATION", market="JP"
            )
        )


@pytest.mark.asyncio
async def test_original_five_precheck_items_use_registered_inputs(
    service, quote_context
):
    db = Database()
    result = await precheck.run_trading_readiness_precheck(db, **kwargs())
    assert result["passed"] is True and result["trading_permission"] == "trade_enabled"
    assert [item["key"] for item in result["items"]] == [
        "db",
        "default_model_configured",
        "simulation_sandbox_pool",
        "stream_series_freshness",
        "signal_readiness",
    ]
    service.getter.assert_awaited_once_with(
        tenant_id="test", user_id="00000007", market="JP"
    )
    service.hosted.get_default_model_hosted_status.assert_awaited_once_with(
        tenant_id="test", user_id="00000007", market="JP", trade_date=DAY
    )
    assert db.calls[-1][1] == {
        "tenant_id": "test",
        "user_id": "00000007",
        "run_id": "current-run",
    }
    service.listing.assert_not_awaited()
    service.setter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["select", "set_failure", "none", "list_failure"])
async def test_original_automatic_default_selection_is_market_scoped(
    service, quote_context, outcome
):
    service.getter.return_value = None
    if outcome == "set_failure":
        service.setter.side_effect = ValueError("controlled setter failure")
    elif outcome == "none":
        service.listing.return_value = []
    elif outcome == "list_failure":
        service.listing.side_effect = ValueError("controlled list failure")
    result = await precheck.run_trading_readiness_precheck(Database(), **kwargs())
    model_item = result["items"][1]
    assert model_item["passed"] is (outcome in {"select", "set_failure"})
    service.listing.assert_awaited_once_with(
        tenant_id="test", user_id="00000007", include_archived=False, market="JP"
    )
    if outcome in {"select", "set_failure"}:
        service.setter.assert_awaited_once_with(
            tenant_id="test", user_id="00000007", model_id="model-jp"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "reader_error", "pool", "model"])
async def test_dated_precheck_failures_do_not_pass_after_hours(
    service, quote_context, failure
):
    if failure == "missing":
        quote_context.reader.load_date = lambda day: {}
    elif failure == "reader_error":

        def fail(day):
            raise ValueError("historical units/publication unavailable")

        quote_context.reader.load_date = fail
    elif failure == "pool":
        service.sandbox._workers = {}
    else:
        service.getter.return_value = None
        service.listing.return_value = []
    result = await precheck.run_trading_readiness_precheck(Database(), **kwargs())
    assert result["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["mismatch", "missing_marker", "empty", "parquet", "unavailable"]
)
async def test_original_marker_and_observation_rules_are_kept(service, kind):
    redis = Redis(
        marker={"mismatch": "old-run", "missing_marker": None}.get(kind, "current-run")
    )
    db = Database(count=0 if kind in {"empty", "parquet"} else 1)
    if kind == "parquet":
        service.hosted.load_pred_parquet_signal_rows.return_value = [
            {"symbol": "JP72030"}
        ]
    elif kind == "unavailable":
        service.status.update(available=False, reason_code="window_pending")
    result = await service.evaluator.evaluate(
        db,
        redis_client=redis,
        tenant_id="test",
        user_id="00000007",
        mode="SIMULATION",
        execution_context=saved_inputs(),
    )
    assert result["blocking"] is False
    assert result["trading_permission"] == (
        "observe_only" if kind in {"empty", "unavailable"} else "trade_enabled"
    )
    if kind in {"mismatch", "missing_marker"}:
        assert redis.writes == [
            ("qm:signal:latest:test:00000007", "current-run", {"ex": 86400})
        ]
    if kind == "parquet":
        assert result["signal_source_fallback"] == "pred_parquet"
    if kind == "unavailable":
        assert db.calls == [] and redis.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/preflight", "/trading-precheck"])
async def test_original_http_routes_accept_dated_queries(
    service, quote_context, monkeypatch, endpoint
):
    auth = AuthContext(
        user_id="00000007", tenant_id="test", raw_sub="7", roles=["user"]
    )
    snapshot = AsyncMock()
    monkeypatch.setattr(routes, "_upsert_preflight_snapshot", snapshot)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_auth_context] = lambda: auth
    app.dependency_overrides[routes.get_redis] = lambda: SimpleNamespace(client=Redis())
    app.dependency_overrides[routes.get_db] = lambda: Database()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit"
    ) as client:
        response = await client.get(
            endpoint,
            params={
                "trading_mode": "SIMULATION",
                "market": "JP",
                "execution_context": json.dumps(saved_inputs()),
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ready" if endpoint == "/preflight" else "passed"] is True
        assert payload["signal_readiness"]["latest_run_id"] == "current-run"
        bad = await client.get(
            endpoint,
            params={
                "trading_mode": "SIMULATION",
                "market": "CN",
                "execution_context": json.dumps(saved_inputs()),
            },
        )
        assert bad.status_code == 400
        real = await client.get(
            endpoint,
            params={
                "trading_mode": "REAL",
                "market": "JP",
                "execution_context": json.dumps(saved_inputs()),
            },
        )
        assert real.status_code == 400
    if endpoint == "/preflight":
        snapshot.assert_awaited_once()


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("select_default", [False, True])
async def test_actual_pg_default_and_signal_rows_preserve_cn_model(
    hosted, monkeypatch, select_default
):
    pipe = hosted
    evaluator = signals.SignalReadinessService()
    monkeypatch.setattr(signals, "manual_execution_service", pipe.service)
    monkeypatch.setattr(precheck, "signal_readiness_service", evaluator)
    monkeypatch.setattr(
        evaluator,
        "_read_signal_latest_key",
        lambda key, redis, **kw: str(redis.get(key) or ""),
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
    monkeypatch.setattr(
        precheck, "_is_cn_trading_hours", lambda: pytest.fail("CN clock")
    )
    monkeypatch.setattr(
        "backend.services.live_trading.routers.real_trading_utils.check_stream_series_freshness",
        lambda **kw: pytest.fail("realtime"),
    )
    # Quote checks read the actual temporary JP publication from the existing
    # input fixture. PG model defaults and inference counts are not mocked.
    if select_default:
        async with pipe.pg.sessions() as db:
            await db.execute(
                text(
                    "UPDATE qm_user_models SET metadata_json=metadata_json-'market_default' WHERE model_id='model-jp'"
                )
            )
            await db.commit()
    async with pipe.pg.sessions() as db:
        await db.execute(
            text(
                "INSERT INTO engine_signal_scores (run_id,tenant_id,user_id,symbol,fusion_score) VALUES ('native-run','test','00000007','JP72030',0.9)"
            )
        )
        await db.commit()
        result = await precheck.run_trading_readiness_precheck(
            db,
            **kwargs(
                execution_context=request(pipe)["execution_context"],
                redis_client=Redis(marker="native-run"),
            ),
        )
        assert (
            result["passed"] is True and result["signal_readiness"]["signal_count"] == 1
        ), result
        await db.commit()
    cn = await models.model_registry_service.get_default_model(
        tenant_id="test", user_id="00000007", market="CN"
    )
    jp = await models.model_registry_service.get_default_model(
        tenant_id="test", user_id="00000007", market="JP"
    )
    assert cn["model_id"] == "model-cn" and cn["is_default"] is True
    assert (
        jp["model_id"] == "model-jp" and jp["metadata_json"]["market_default"] is True
    )
    assert jp["is_default"] is True  # Original public market-default projection.
    async with pipe.pg.sessions() as db:
        persisted = (
            await db.execute(
                text(
                    "SELECT model_id, is_default FROM qm_user_models WHERE user_id='00000007' ORDER BY model_id"
                )
            )
        ).all()
        assert persisted == [("model-cn", True), ("model-jp", False)]
