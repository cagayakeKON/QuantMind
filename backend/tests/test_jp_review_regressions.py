"""Independent regressions for the gaps found in the Japanese-market review."""

import asyncio
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.inference.templates import (
    inference_ensemble_src as ensemble,
)
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator

snapshot = source_fixture


@pytest.fixture
def native(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    build_jp_features(root, evaluator=fake_evaluator)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root


@pytest.mark.asyncio
async def test_ai_pool_never_reads_existing_cn_table_and_filters_real_jp_rows(
    native, monkeypatch
):
    from backend.services.engine.ai_strategy.steps import (
        step2_pool_confirmation as pools,
    )
    from backend.services.engine.ai_strategy.steps.step1_stock_selection import (
        get_latest_table,
    )
    from backend.services.engine.ai_strategy.api.v1.generation import MARKET_QLIB_CONFIG

    def no_pg(*args, **kwargs):
        raise AssertionError("JP stock selection must not read a CN PG table")

    monkeypatch.setattr(pools, "get_db", no_pg)
    assert get_latest_table("JP") == "stock_daily_latest_jp"
    cfg = MARKET_QLIB_CONFIG["JP"]
    assert cfg["region"] == "us" and cfg["benchmark"] == "TOPIX"
    assert "quantjp" in cfg["provider_uri"]
    result = await pools.query_pool("SELECT symbol WHERE true", "7", "JP")
    assert {row.symbol for row in result.items} == {"JP72030", "JP216A0"}
    assert result.summary["asOf"] == "2026-09-30"
    threshold = min(row.metrics["close"] for row in result.items)
    filtered = await pools.query_pool(
        f"SELECT symbol WHERE close > {threshold}", "7", "JP"
    )
    assert all(row.metrics["close"] > threshold for row in filtered.items)
    sql = await pools.query_pool(
        "SQL: SELECT symbol FROM stock_daily_latest_jp WHERE close > 0", "7", "JP"
    )
    assert {row.symbol for row in sql.items} == {row.symbol for row in result.items}
    with pytest.raises(ValueError, match="unavailable"):
        await pools.query_pool("SELECT symbol WHERE finance_balance > 0", "7", "JP")


def test_ensemble_reads_native_partition_and_cli_market(native, monkeypatch):
    frame = ensemble.load_day_data("2026-09-30", native, market="JP")
    assert not frame.empty and set(frame.symbol) == {"JP72030", "JP216A0"}
    assert "feature_0" in frame and frame.trade_date.eq("2026-09-30").all()
    monkeypatch.setattr(
        "sys.argv",
        [
            "inference.py",
            "--date",
            "2026-09-30",
            "--output",
            "unused.json",
            "--market",
            "JP",
        ],
    )
    assert ensemble.parse_args().market == "JP"

    class Member:
        def predict(self, values):
            assert values.shape == (2, 1) and (values == 1).all()
            return [0.2, 0.8]

    mapped = ensemble.predict_with_model(
        Member(),
        {
            "context": {"market": "JP"},
            "features": ["trained_name"],
            "factor_field_sources": {"trained_name": "feature_0"},
        },
        frame,
    )
    assert set(mapped) == {"JP72030", "JP216A0"}
    with pytest.raises(ValueError, match="missing published"):
        ensemble.predict_with_model(
            None, {"context": {"market": "JP"}, "features": ["missing"]}, frame
        )
    from backend.services.engine.inference.script_runner import InferenceScriptRunner
    from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub

    assert InferenceScriptRunner._resolve_primary_active_data_source(
        {"context": {"market": "JP"}, "data_source": "parquet"}
    ) == str(QuantJPDataHub(native).data_dir)


@pytest.mark.asyncio
async def test_activation_uses_japanese_published_holidays_without_china_calendar(
    native, monkeypatch
):
    from backend.services.engine.qlib_app.api import user_strategies as activation
    from backend.services.engine.inference import router_service

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 2, tzinfo=timezone.utc).astimezone(tz)

    def forbidden(*args, **kwargs):
        raise AssertionError("JP activation must not open XSHG")

    recorded = []
    monkeypatch.setattr(activation, "datetime", FixedDateTime)
    monkeypatch.setattr(activation.xcals, "get_calendar", forbidden)
    monkeypatch.setattr(
        activation,
        "get_redis_sentinel_client",
        lambda: SimpleNamespace(set=lambda *a, **kw: True),
    )
    monkeypatch.setattr(
        router_service,
        "InferenceRouterService",
        lambda: SimpleNamespace(
            run_daily_inference_script=lambda **kw: (
                recorded.append(kw) or SimpleNamespace(success=True)
            )
        ),
    )
    await activation._trigger_inference_after_activate(
        strategy_id="2", tenant_id="test", user_id="7", market="JP"
    )
    assert recorded[0]["date"] == "2026-09-29"


@pytest.mark.parametrize("fail_features", [False, True])
def test_admin_sync_passes_publication_root_and_does_not_report_failed_build_completed(
    native, monkeypatch, fail_features
):
    import sys
    from backend.services.api.routers.admin import global_market_console as console
    from backend.services.engine.data_platform import jp_features
    from backend.services.engine import qlib_data_builder

    class InlineThread:
        def __init__(self, target, args, **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    received = []

    def build(root):
        received.append(root)
        assert root == native
        if fail_features:
            raise ValueError("controlled feature publication failure")
        return {"status": "ok"}

    monkeypatch.setattr(console.threading, "Thread", InlineThread)
    monkeypatch.setitem(
        sys.modules, "test_jp_sync_entry", SimpleNamespace(run=lambda **kw: {})
    )
    monkeypatch.setattr(jp_features, "build_jp_features_in_process", build)
    monkeypatch.setattr(qlib_data_builder, "ensure_qlib_cache", lambda **kw: "jp-cache")
    router = console.make_market_router(
        market="JP",
        env_var="QM_QUANTJP_DATA_DIR",
        default_dir=str(native),
        sync_entry="test_jp_sync_entry",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[console.require_admin] = lambda: {"user_id": "7"}
    # Call endpoints directly: the worker is synchronous, no real online sync.
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/sync-datasets"
    )
    response = asyncio.run(
        endpoint(
            console.SyncDatasetsRequest(datasets=["daily_unadjusted"], with_qlib=True),
            {"user_id": "7"},
        )
    )
    job = response["data"]["job"]
    assert received == [native]
    assert job["status"] == ("failed" if fail_features else "completed")


@pytest.mark.asyncio
async def test_original_feature_catalog_accepts_jp_without_rewriting_other_markets(
    tmp_path, monkeypatch
):
    from backend.services.api.routers.admin.model_management_ops import (
        update_feature_catalog,
    )

    monkeypatch.chdir(tmp_path)
    catalog = {
        "categories": [
            {"id": "test", "features": [{"key": "KMID", "markets": ["JP", "CN"]}]}
        ]
    }
    result = await update_feature_catalog(catalog, {"user_id": "7"})
    assert result["status"] == "ok"
    saved = json.loads(
        (
            tmp_path / "config/features/model_training_feature_catalog_v1.json"
        ).read_text()
    )
    assert saved["categories"][0]["features"][0]["markets"] == ["JP", "CN"]


def test_equity_worker_does_not_project_native_jpy_cache_into_cny_root():
    from backend.services.simulation.services.equity_settlement_worker import (
        SimulationEquitySettlementWorker,
    )

    values = {
        "simulation:account:test:7": json.dumps({"market": "CN", "cash": 250000}),
        "simulation:account:test:7:JP": json.dumps({"market": "JP", "cash": 30000}),
    }
    redis = SimpleNamespace(
        client=SimpleNamespace(
            scan_iter=lambda **kw: values.keys(), get=lambda key: values[key]
        )
    )
    rows = SimulationEquitySettlementWorker(redis)._load_accounts()
    assert (
        len(rows) == 1
        and rows[0]["market"] == "CN"
        and rows[0]["account"]["cash"] == 250000
    )


def test_old_registered_fills_without_native_scope_require_audited_migration():
    from backend.services.simulation.services.dated_account import (
        require_market_ledger_scope,
    )

    with pytest.raises(ValueError, match="migration"):
        require_market_ledger_scope(
            {"metadata": {"state": {"fills": [{"order_id": "old"}]}}},
            "sim:test:7",
            "JP",
        )
