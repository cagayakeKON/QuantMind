"""JP inference stays on its market, target session and immutable publication."""

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import duckdb
import pandas as pd
import pytest
from sqlalchemy import text

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.inference import gap_backfill, script_runner
from backend.shared.model_registry import ResolvedModel
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_market_hosted_execution import (
    boundary as boundary_fixture,
    cash_setup as cash_setup_fixture,
    hosted as hosted_fixture,
    pg as pg_fixture,
    pipeline as pipeline_fixture,
    published as published_fixture,
)

snapshot = source_fixture
boundary = boundary_fixture
cash_setup = cash_setup_fixture
hosted = hosted_fixture
pg = pg_fixture
pipeline = pipeline_fixture
published = published_fixture


@pytest.fixture
def research_publication(snapshot, tmp_path, monkeypatch):
    from backend.services.engine.data_platform import jp_calendar

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 4, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(jp_calendar, "datetime", Clock)
    root = tmp_path / "research"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    with duckdb.connect(str(snapshot)) as db:
        db.execute(
            "INSERT INTO research.calendar VALUES ('2026-10-01','1'),('2026-10-02','1')"
        )
        for day in ("2026-10-01", "2026-10-02"):
            for table in ("master", "daily_prices", "topix"):
                db.execute(
                    f"INSERT INTO research.{table} SELECT * REPLACE(DATE '{day}' AS Date) FROM research.{table} WHERE Date='2026-09-30'"
                )
    import_jquants_snapshot(snapshot, root)
    first = build_jp_features(root, evaluator=fake_evaluator)["version"]
    return snapshot, root, root / "versions" / first


def test_coverage_uses_published_jp_sessions_in_chinese_holiday(
    research_publication, tmp_path, monkeypatch
):
    _, _, version = research_publication
    model = tmp_path / "model"
    model.mkdir()
    pd.DataFrame(
        {
            "trade_date": [date(2026, 9, 28), date(2026, 9, 30)],
            "symbol": ["JP72030"] * 2,
            "pred": [0.7, 0.8],
        }
    ).to_parquet(model / "pred.parquet")
    monkeypatch.setattr(
        gap_backfill,
        "latest_trading_date",
        lambda: pytest.fail("JP must not use legacy XSHG latest date"),
    )
    result = gap_backfill.compute_coverage(
        model_id="jp", storage_path=str(model), metadata={"market": "JP"}
    )
    assert result["latest_trade_date"] == result["data_cutoff_date"] == "2026-10-02"
    assert result["gap_dates"] == ["2026-09-29", "2026-10-01", "2026-10-02"]
    assert not result["is_up_to_date"]
    assert LOCAL_MARKET_PROVIDERS["JP"].open().data_dir == version


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["JP", "CN"])
async def test_failed_backfill_never_copies_jp_date_typed_predictions(
    tmp_path, monkeypatch, market
):
    model = tmp_path / market
    model.mkdir()
    (model / "metadata.json").write_text(json.dumps({"context": {"market": market}}))
    pred = model / "pred.parquet"
    pd.DataFrame(
        {
            "trade_date": [date(2026, 9, 30)],
            "symbol": ["JP72030" if market == "JP" else "SH600036"],
            "pred": [0.8],
        }
    ).to_parquet(pred)
    original = pred.read_bytes()
    monkeypatch.setattr(
        script_runner,
        "InferenceScriptRunner",
        lambda **kw: SimpleNamespace(
            execute=lambda **kw: SimpleNamespace(
                success=False, error="controlled model failure"
            )
        ),
    )
    monkeypatch.setattr(gap_backfill, "_mark_failed_run", AsyncMock())
    monkeypatch.setattr(gap_backfill, "_mark_success_run", AsyncMock())
    monkeypatch.setattr(
        gap_backfill,
        "_resolve_backfill_model_lineage",
        lambda **kw: (market, "user_default"),
    )
    # Deliberately omit registry metadata: actual JP model artifacts still
    # cannot enable copied output, even when the old flag is explicitly true.
    result = await gap_backfill.backfill_model_gaps(
        tenant_id="test",
        user_id="7",
        model_id=market,
        storage_path=str(model),
        gaps=["2026-10-01"],
        allow_template_copy=True,
    )
    if market == "JP":
        assert (
            result["status"] == "failed"
            and result["failed"] == 1
            and result["appended"] == 0
        )
        assert pred.read_bytes() == original
        assert gap_backfill.read_pred_dates(pred) == ["2026-09-30"]
    else:
        assert result["status"] == "completed" and result["appended"] == 1
        frame = pd.read_parquet(pred)
        assert frame["pred"].tolist() == [0.8, 0.8]
        assert gap_backfill.read_pred_dates(pred) == ["2026-09-30", "2026-10-01"]


@pytest.mark.asyncio
@pytest.mark.parametrize("market,ready", [("JP", True), ("JP", False), ("US", True)])
async def test_activation_passes_market_resolved_model_and_never_global_jp_default(
    research_publication, tmp_path, monkeypatch, market, ready
):
    from backend.services.engine.qlib_app.api import user_strategies as activation
    from backend.services.engine.inference import router_service
    from backend.shared import model_registry

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 2, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(activation, "datetime", Clock)

    model = tmp_path / "model"
    model.mkdir()
    resolved = ResolvedModel(
        effective_model_id="jp-default" if ready else None,
        model_source="user_default" if ready else "none",
        fallback_used=False,
        fallback_reason="",
        storage_path=str(model) if ready else "",
        model_file="",
        status="ready" if ready else "not_found",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(
        model_registry.model_registry_service, "resolve_effective_model", resolver
    )
    recorded = []
    monkeypatch.setattr(
        router_service,
        "InferenceRouterService",
        lambda: SimpleNamespace(
            run_daily_inference_script=lambda **kw: (
                recorded.append(kw) or SimpleNamespace(success=True)
            )
        ),
    )
    monkeypatch.setattr(
        activation,
        "get_redis_sentinel_client",
        lambda: SimpleNamespace(set=lambda *a, **kw: True),
    )
    await activation._trigger_inference_after_activate(
        strategy_id="2", tenant_id="test", user_id="7", market=market
    )
    if market == "JP":
        resolver.assert_awaited_once_with(
            tenant_id="test", user_id="7", strategy_id="2", market="JP"
        )
        if ready:
            assert recorded[0]["resolved_model"] == resolved.to_dict()
        else:
            assert recorded == []
    else:
        resolver.assert_not_awaited()
        assert "resolved_model" not in recorded[0]


@pytest.mark.parametrize("ensemble", [False, True])
def test_execution_pins_publication_through_subprocess_and_reference_price(
    research_publication, tmp_path, monkeypatch, ensemble
):
    source, root, first = research_publication
    model = tmp_path / "model"
    model.mkdir()
    (model / "metadata.json").write_text(
        json.dumps(
            {
                "context": {"market": "JP"},
                "data_source": "quantdb_factors",
                "factor_source": "l1_factors",
                "features": ["feature_0"],
                "is_ensemble": ensemble,
            }
        )
    )
    (model / "inference.py").write_text("# controlled subprocess fixture")
    runner = script_runner.InferenceScriptRunner(
        primary_model_dir=str(model), primary_model_id="jp"
    )
    writes = []
    db = SimpleNamespace(
        execute=lambda stmt, args: writes.append((str(stmt), args)),
        commit=lambda: None,
        close=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr(
        script_runner,
        "create_engine",
        lambda *a, **kw: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(script_runner, "sessionmaker", lambda **kw: lambda: db)
    from backend.services.engine.inference import position_signal

    # This case verifies publication-bound reference prices. Keep unrelated
    # calibration reads outside the controlled inference persistence probe.
    monkeypatch.setattr(position_signal, "compute_position_scores", lambda *a: [])
    monkeypatch.setattr(position_signal, "batch_update_quality", lambda *a, **kw: None)
    original_readiness = runner._query_quantdb_readiness
    pins = []

    def readiness(day=None, **kw):
        pins.append(kw["publication_data_dir"])
        return original_readiness(**kw)

    monkeypatch.setattr(runner, "_query_quantdb_readiness", readiness)

    def subprocess_run(cmd, **kw):
        path = Path(kw["env"]["MODEL_TRAINING_DATA_DIR"])
        assert path == first
        assert cmd[-2:] == ["--market", "JP"]
        with duckdb.connect(str(source)) as conn:
            conn.execute(
                "UPDATE research.daily_prices SET O=O+900,H=H+900,L=L+900,C=C+900"
            )
        import_jquants_snapshot(source, root)
        build_jp_features(root, evaluator=fake_evaluator)
        assert LOCAL_MARKET_PROVIDERS["JP"].open().data_dir != first
        if ensemble:
            from backend.services.engine.inference.templates.inference_ensemble_src import (
                load_day_data,
            )

            assert not load_day_data("2026-09-30", path, market="JP").empty
        else:
            from backend.services.engine.inference.templates.inference_parquet import (
                _quantdb_reader,
            )

            assert (
                _quantdb_reader(
                    {
                        "context": {"market": "JP"},
                        "data_source": "quantdb_factors",
                        "factor_source": "l1_factors",
                    },
                    path,
                ).data_dir
                == first
            )
        Path(cmd[cmd.index("--output") + 1]).write_text(
            json.dumps([{"symbol": "JP72030", "score": 0.7}])
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(script_runner.subprocess, "run", subprocess_run)
    original_persist = runner._persist_and_publish

    def persist(run_id, prediction, tenant, user, signals, **kw):
        assert kw["publication_data_dir"] == str(first)
        return original_persist(run_id, prediction, tenant, user, signals, **kw)

    monkeypatch.setattr(runner, "_persist_and_publish", persist)
    result = runner.execute("2026-09-30", tenant_id="test", user_id="7")
    assert result.success and result.active_data_source == str(first)
    assert pins == [str(first)]
    assert result.prediction_trade_date == "2026-10-01"
    score_rows = next(
        args for sql, args in writes if "INSERT INTO engine_signal_scores" in sql
    )
    assert score_rows[0]["expected_price"] == 45
    candidates = next(
        args
        for sql, args in writes
        if "INSERT INTO qm_research_candidate_snapshot" in sql
    )
    assert candidates[0]["expected_price"] == 45
    assert (
        script_runner._load_close_price_map("2026-09-30", market="JP")["JP72030"] == 945
    )


def test_jp_writer_cannot_reopen_mutable_current_without_execution_pin(
    tmp_path, monkeypatch
):
    (tmp_path / "metadata.json").write_text(json.dumps({"context": {"market": "JP"}}))
    runner = script_runner.InferenceScriptRunner(
        primary_model_dir=str(tmp_path), primary_model_id="jp"
    )
    monkeypatch.setattr(
        script_runner,
        "create_engine",
        lambda *a, **kw: pytest.fail("pin required before DB connection"),
    )
    with pytest.raises(ValueError, match="pinned publication"):
        runner._persist_and_publish(
            "no-pin",
            "2026-10-01",
            "test",
            "7",
            [{"symbol": "JP72030", "score": 0.7}],
            data_trade_date="2026-09-30",
        )


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PG opt-in")
@pytest.mark.asyncio
async def test_hosted_exact_session_selects_ready_older_batch_and_not_latest(hosted):
    pipe = hosted
    async with pipe.pg.sessions() as db:
        await db.execute(
            text(
                "INSERT INTO qm_model_inference_runs SELECT 'newer-run',tenant_id,user_id,model_id,status,data_trade_date + 1,prediction_trade_date + 1,signals_count,created_at + INTERVAL '1 minute',model_source,effective_model_id,fallback_used FROM qm_model_inference_runs WHERE run_id='native-run'"
            )
        )
        await db.commit()
    selected = await pipe.service.get_default_model_hosted_status(
        tenant_id="test",
        user_id="00000007",
        market="JP",
        trade_date=pipe.context.trade_date,
    )
    assert selected["available"] and selected["latest_run_id"] == "native-run"
    latest = await pipe.service._load_latest_default_model_inference_run(
        tenant_id="test", user_id="00000007", model_id="model-jp"
    )
    assert latest["run_id"] == "newer-run"
    missing = await pipe.service.get_default_model_hosted_status(
        tenant_id="test", user_id="00000007", market="JP", trade_date=date(2026, 10, 1)
    )
    assert not missing["available"] and missing["reason_code"] == "missing_latest_run"
