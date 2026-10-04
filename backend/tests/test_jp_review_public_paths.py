"""Public JP review regressions, isolated from production financial state."""

import ast
from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import traceback
from unittest.mock import AsyncMock

import duckdb
from fastapi import HTTPException
import numpy as np
import pandas as pd
import pytest

from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.mark.parametrize(
    "market,expected",
    [("CN", "SH000300"), ("US", "SH000300"), ("HK", "SH000300"), ("JP", "TOPIX")],
)
def test_blank_benchmark_preserves_old_defaults_and_jp_default(market, expected):
    from backend.shared.training.request import ContextRequest

    assert (
        ContextRequest(market=market, benchmark="   ").cleaned()["benchmark"]
        == expected
    )


@pytest.fixture
def native_publication(snapshot, tmp_path, monkeypatch):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.jp_publication import publish_pointer

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "CREATE TABLE research.valuation (Date DATE, Code VARCHAR, PER DOUBLE, PBR DOUBLE, ROE DOUBLE, EPS DOUBLE, BPS DOUBLE, MktCap DOUBLE)"
        )
        conn.execute(
            "INSERT INTO research.valuation VALUES ('2026-09-30','72030',12,1.5,0.15,4,30,123)"
        )
    root = tmp_path / "publication"
    raw = import_jquants_snapshot(snapshot, root)
    version = "review-features"
    folder = root / "versions" / version
    shutil.copytree(root / "versions" / raw["version"], folder)
    for day, multiplier in [("20260929", 1), ("20260930", 9)]:
        target = folder / "6_ml_datasets/l1_factors" / f"dt={day}"
        target.mkdir(parents=True)
        pd.DataFrame(
            {
                "symbol": ["72030.JP"],
                "time": [pd.Timestamp(day)],
                "dt": [int(day)],
                "KMID": [0.1 * multiplier],
                "KLEN": [0.2 * multiplier],
                "KMID2": [0.3 * multiplier],
                "KUP": [0.4 * multiplier],
            }
        ).to_parquet(target / "part.parquet")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["version"] = version
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    publish_pointer(root, version)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root, version


def test_native_pool_roe_industry_and_unsupported_fields(native_publication):
    from backend.services.engine.data_platform.local_stock_pool import (
        _column,
        query_local_stock_pool,
    )
    from backend.services.simulation.jp.stock_pool import (
        FIELD_MAPPING,
        open_stock_pool_inputs,
    )

    inputs = open_stock_pool_inputs()
    frame = inputs.snapshot(inputs.trade_date)
    assert _column("roe", frame, inputs.field_mapping) == "roe"
    assert _column("industry", frame, inputs.field_mapping) == "industry_name"
    rows, day, _ = query_local_stock_pool("SELECT symbol WHERE roe > 0.1", "JP")
    assert day == date(2026, 9, 30) and [row.symbol for row in rows] == ["JP72030"]
    rows, _, _ = query_local_stock_pool(
        "SQL: SELECT symbol FROM stock_daily_latest WHERE industry_name='Transportation' AND pe_ttm > 10",
        "JP",
    )
    assert [row.symbol for row in rows] == ["JP72030"]
    common, _, _ = query_local_stock_pool(
        "SQL: SELECT symbol FROM stock_daily_latest WHERE industry='Transportation' AND pe > 10 AND market_cap=123000000",
        "JP",
    )
    assert [row.symbol for row in common] == [row.symbol for row in rows]
    assert common[0].metrics["market_cap"] == 1.23
    with pytest.raises(ValueError, match="unavailable: concept_ai"):
        query_local_stock_pool("SELECT symbol WHERE concept_ai > 0", "JP")
    assert _column("roe", pd.DataFrame({"fun_roe": [1]})) == "fun_roe"
    assert FIELD_MAPPING["industry"] == "industry_name"


def test_native_return_conditions_use_adjusted_sessions():
    from backend.services.simulation.jp.stock_pool import _price_conditions

    days = pd.bdate_range("2026-09-21", periods=6)
    history = pd.DataFrame(
        {
            "trade_date": days,
            "symbol": ["72030.JP"] * 6,
            "close": [100, 110, 120, 130, 140, 150],
        }
    )
    hub = SimpleNamespace(
        _partition_dates=lambda *a, **kw: [d.strftime("%Y%m%d") for d in days],
        _normalize_kline=lambda f: f,
        _read=lambda *a: history,
    )
    frame = _price_conditions(hub, days[-1].date(), pd.DataFrame(index=["72030.JP"]))
    assert frame.loc["72030.JP", "return_5d"] == 0.5
    assert frame.loc["72030.JP", "pct_change"] == pytest.approx((150 / 140 - 1) * 100)
    assert pd.isna(frame.loc["72030.JP", "return_60d"])


@pytest.mark.asyncio
async def test_remote_jp_rejected_before_public_submit_side_effects(monkeypatch):
    from backend.services.api.routers.admin import admin_training_utils as submit
    from backend.services.api.routers.model_training import get_data_window
    from backend.services.engine.training import window_probe

    resolver = AsyncMock(
        side_effect=AssertionError("must reject before feature/probe/DB")
    )
    monkeypatch.setattr(submit, "_resolve_quantdb_factor_payload", resolver)
    with pytest.raises(HTTPException, match="local node only") as exc:
        await submit.submit_training_job(
            {"context": {"market": "JP"}, "node_id": "autodl-1"}, None, {}
        )
    assert exc.value.status_code == 422 and not resolver.called
    with pytest.raises(ValueError, match="local node only"):
        await window_probe.probe_data_window("autodl-1", "l1_factors", "JP")
    with pytest.raises(HTTPException, match="local node only") as exc:
        await get_data_window(node_id="autodl-1", market="JP", current_user={})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("market", ["JP", "US"])
def test_scheduled_feature_failure_only_changes_jp_outer_status(monkeypatch, market):
    from backend.services.engine.tasks import market_sync_scheduler as sync
    from backend.services.engine.tasks.celery_tasks import run_market_scheduled_sync
    from backend.services.engine import qlib_data_builder
    from backend.services.engine.data_platform import jp_features
    from backend.scripts import quantjp_daily_sync, quantus_daily_sync

    monkeypatch.setattr(quantjp_daily_sync, "run", lambda **kw: {"raw": "ok"})
    monkeypatch.setattr(quantus_daily_sync, "run", lambda **kw: {"raw": "ok"})

    def fail(*a, **kw):
        raise ValueError("review feature publication failed")

    monkeypatch.setattr(jp_features, "build_jp_features_in_process", fail)
    monkeypatch.setattr(qlib_data_builder, "ensure_qlib_cache", fail)
    result = run_market_scheduled_sync.run(market, {"with_qlib": True})
    if market == "JP":
        assert result["status"] == "failed" and "review feature" in result["error"]
    else:
        assert "status" not in result and result["qlib"]["status"] == "error"
    assert sync.run_market_sync


@pytest.mark.parametrize("market", ["JP", "CN"])
def test_actual_generated_aiide_runner_builds_market_request(
    monkeypatch, tmp_path, market
):
    from backend.services.engine.routers.ai_ide import executor
    from backend.services.engine.qlib_app.services import backtest_service

    for key in list(os.environ):
        if (
            key.startswith("AI_IDE_BACKTEST_")
            or key == "AI_IDE_ALLOW_FEATURE_SIGNAL_FALLBACK"
        ):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AI_IDE_BACKTEST_START_DATE", "2026-09-29")
    monkeypatch.setenv("AI_IDE_BACKTEST_END_DATE", "2026-09-30")
    if market == "JP":
        monkeypatch.setenv("AI_IDE_BACKTEST_MARKET", market)
    path = tmp_path / "strategy.py"
    path.write_text(
        "def get_strategy_config(): return {'class': 'TopkDropoutStrategy', 'kwargs': {'topk': 5}}"
    )
    tree = ast.parse(executor._build_runner_script())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_run_module_backtest"
    )
    namespace = {
        "os": os,
        "pathlib": __import__("pathlib"),
        "traceback": traceback,
        "STRATEGY_PATH": str(path),
        "QLIB_DATA_PATH": "/test/provider",
        "_init_qlib": lambda: True,
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]), "generated_runner.py", "exec"
        ),
        namespace,
    )
    seen = []

    async def run(self, request):
        seen.append(request)
        return SimpleNamespace(status="completed", execution_time=0)

    monkeypatch.setattr(backtest_service.QlibBacktestService, "run_backtest", run)
    assert (
        namespace["_run_module_backtest"](
            SimpleNamespace(
                get_strategy_config=lambda: {
                    "class": "TopkDropoutStrategy",
                    "kwargs": {"topk": 5},
                }
            )
        )
        == 0
    )
    request = seen[0]
    if market == "JP":
        assert request.market == "JP" and request.benchmark == "TOPIX"
        assert (
            request.deal_price == "open"
            and not request.use_vectorized
            and not request.allow_feature_signal_fallback
        )
        assert all(
            getattr(request, f) == 0
            for f in [
                "commission",
                "min_commission",
                "stamp_duty",
                "transfer_fee",
                "min_transfer_fee",
                "impact_cost_coefficient",
            ]
        )
    else:
        assert request.market is None and request.benchmark == "SH000300"
        assert request.deal_price == "close" and request.use_vectorized
        assert request.min_commission == 5 and request.stamp_duty == 0.0005


def test_runner_env_and_generation_injection_are_jp_context(monkeypatch):
    from backend.services.engine.routers.ai_ide import executor, skill_engine, chat
    from backend.shared import qlib_paths

    monkeypatch.setattr(
        qlib_paths, "resolve_qlib_provider_uri", lambda market: f"/provider/{market}"
    )
    monkeypatch.setattr(
        qlib_paths,
        "fallback_to_ready_provider_uri",
        lambda path: pytest.fail("JP must not fallback to CN"),
    )
    monkeypatch.delenv("AI_IDE_ALLOW_FEATURE_SIGNAL_FALLBACK", raising=False)
    env = executor._build_runner_environment("review-owner", {"market": "JP"})
    assert (
        env["AI_IDE_BACKTEST_PROVIDER_URI"] == env["QLIB_DATA_PATH"] == "/provider/JP"
    )
    assert (
        env["AI_IDE_BACKTEST_BENCHMARK"] == "TOPIX"
        and env["AI_IDE_BACKTEST_REGION"] == "us"
    )
    assert env["AI_IDE_ALLOW_FEATURE_SIGNAL_FALLBACK"] == "false"
    engine = skill_engine.SkillEngine()
    injection = engine.get_error_injection(
        "NameError: name 'qlib' is not defined", market="JP"
    )
    assert skill_engine.MARKET_QLIB_CONFIG["JP"]["provider_uri"] in injection
    assert "region='us'" in injection
    assert "JPY" in chat.STRATEGY_MARKET_CONTEXT["JP"]
    jp_prompt = engine.build_skill_prompt(
        "模型策略股票池 fundamental", {"market": "JP"}
    )
    assert "list:JP72030" in jp_prompt
    assert "list:SH600036" not in jp_prompt
    assert "pool:csi300" not in jp_prompt
    assert "features_daily" not in jp_prompt
    assert '"f_is_st_not": 1' not in jp_prompt
    assert '"f_listed_days_min": 120' not in jp_prompt
    assert "RedisLongShortTopkStrategy" not in jp_prompt
    assert "RedisStopLossStrategy" not in jp_prompt
    assert "JPY" in jp_prompt
    cn_prompt = engine.build_skill_prompt(
        "模型策略股票池 fundamental", {"market": "CN"}
    )
    assert "list:SH600036" in cn_prompt
    assert '"f_is_st_not": 1' in cn_prompt
    assert "pool:csi300" in cn_prompt


@pytest.mark.parametrize("market", ["JP", "CN"])
def test_generated_runner_startup_preserves_jp_provider_and_legacy_fallback(
    monkeypatch, market
):
    from backend.services.engine.routers.ai_ide.executor import _build_runner_script
    from backend.shared import qlib_paths

    monkeypatch.setenv("AI_IDE_BACKTEST_MARKET", market)
    monkeypatch.setenv(
        "AI_IDE_BACKTEST_PROVIDER_URI",
        "/provider/JP" if market == "JP" else "/legacy/CN",
    )
    calls = []

    def fallback(path):
        calls.append(path)
        return "/ready/CN"

    monkeypatch.setattr(qlib_paths, "fallback_to_ready_provider_uri", fallback)
    startup = next(
        node
        for node in ast.parse(_build_runner_script()).body
        if isinstance(node, ast.Try)
    )
    namespace = {"os": os}
    exec(
        compile(
            ast.Module(body=[startup], type_ignores=[]), "runner_startup.py", "exec"
        ),
        namespace,
    )
    assert namespace["QLIB_DATA_PATH"] == (
        "/provider/JP" if market == "JP" else "/ready/CN"
    )
    assert calls == ([] if market == "JP" else ["/legacy/CN"])


def test_runner_dates_are_published_cash_sessions(native_publication):
    from backend.services.simulation.jp.runner_context import default_dates

    assert default_dates() == ("2026-09-29", "2026-09-30")


def test_shap_uses_model_pinned_publication_and_asof(
    native_publication, tmp_path, monkeypatch
):
    import glob
    import lightgbm as lgb
    from backend.services.api.routers.research_service import _compute_shap_drivers_sync
    from backend.services.simulation.jp.model_snapshot import read_model_snapshot

    _, version = native_publication
    features = ["KMID", "KLEN", "KMID2", "KUP", "missing_feature"]
    metadata = {
        "context": {"market": "JP"},
        "factor_coverage": {"jp_data_version": version},
        "framework": "lightgbm",
        "feature_columns": features,
        "model_file": "model.txt",
        "fill_values": {"missing_feature": 7},
    }
    row, available = read_model_snapshot(
        metadata, "7203.T", date(2026, 9, 29), features
    )
    assert row.KMID == 0.1 and "missing_feature" not in available
    assert read_model_snapshot(metadata, "JP72030", date(2026, 9, 28), features) is None
    with pytest.raises(ValueError, match="inconsistent"):
        read_model_snapshot(
            {**metadata, "jp_data_version": "another"},
            "JP72030",
            date(2026, 9, 29),
            features,
        )
    rng = np.random.default_rng(12)
    x = rng.normal(size=(128, 5))
    model = lgb.train(
        {
            "objective": "regression",
            "verbosity": -1,
            "num_threads": 1,
            "min_data_in_leaf": 4,
            "num_leaves": 12,
        },
        lgb.Dataset(x, label=x @ np.arange(1, 6), feature_name=features),
        num_boost_round=12,
    )
    model.save_model(str(tmp_path / "model.txt"))
    meta_path = tmp_path / "metadata.json"
    meta_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(glob, "glob", lambda pattern: [str(meta_path)])
    drivers = _compute_shap_drivers_sync("review-model", "JP72030", "2026-09-29", "JP")
    assert len(drivers) == 4 and {d["name"] for d in drivers} == set(features[:4])
    assert {d["name"]: d["value"] for d in drivers}["KMID"] == 0.1


def test_dynamic_position_rejects_missing_volume_without_changing_legacy(monkeypatch):
    from backend.services.engine.qlib_app.services.market_strategy_context import (
        MarketStrategyContext,
        StrategyContextSpec,
    )
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
    from backend.services.engine.qlib_app.services.market_state_service import (
        MarketStateService,
    )

    monkeypatch.delenv("MARKET_CONFIG_URL", raising=False)
    monkeypatch.delenv("MARKET_STATE_CONFIG_URL", raising=False)
    dates = pd.bdate_range("2026-07-01", periods=35)
    frame = pd.DataFrame(
        {"$close": np.arange(35) + 100, "$volume": np.nan},
        index=pd.MultiIndex.from_product(
            [["TOPIX"], dates], names=["instrument", "datetime"]
        ),
    )
    request = QlibBacktestRequest(
        market="JP",
        benchmark="TOPIX",
        dynamic_position=True,
        start_date="2026-07-01",
        end_date="2026-08-18",
        market_state_window=5,
    )
    # Original service rule is deliberately preserved; only the native adapter rejects absent input.
    assert MarketStateService(
        data_provider=SimpleNamespace(features=lambda *a, **kw: frame)
    ).build_risk_degree_series("TOPIX", request.start_date, request.end_date, window=5)[
        0
    ]
    context = object.__new__(MarketStrategyContext)
    context.mapper = lambda s: s
    context.spec = StrategyContextSpec(
        "unused", "us", "version", "unused", required_market_state_fields=("$volume",)
    )
    context.errors = []
    context.provider = SimpleNamespace(features=lambda *a, **kw: frame)
    with pytest.raises(ValueError, match="benchmark.*volume"):
        context.market_state_kwargs(request)
    request.dynamic_position = False
    context.errors.clear()
    assert context.market_state_kwargs(request) == {}


def test_real_qlib_topix_missing_volume_is_explicit_failure(native_publication):
    _, version = native_publication
    code = """
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.simulation.jp.strategy_context import prepare_context
from backend.services.engine.qlib_app.services.market_strategy_context import MarketStrategyContext
import sys
request = QlibBacktestRequest(market='JP', jp_data_version=sys.argv[1], benchmark='TOPIX', dynamic_position=True, start_date='2026-09-29', end_date='2026-09-30')
context = MarketStrategyContext(prepare_context(request))
try:
    context.market_state_kwargs(request)
except ValueError as error:
    assert 'benchmark $volume' in str(error), error
    assert context.errors
    print('REVIEW_MISSING_VOLUME_BLOCKED')
else:
    raise AssertionError('NaN volume must not silently yield successful risk degrees')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, version],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REVIEW_MISSING_VOLUME_BLOCKED" in result.stdout
