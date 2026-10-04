"""Prediction source survives publication and partial inference updates."""

from datetime import date
import json
from pathlib import Path
from unittest.mock import AsyncMock
import os

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
import pytest_asyncio

from backend.services.engine.inference.pred_merge import merge_signals_into_pred
from backend.services.engine.inference.prediction_provenance import read_pred_sources
from backend.tests.test_jp_inference_continuity import (
    research_publication as research_fixture,
    snapshot as snapshot_fixture,
)

snapshot = snapshot_fixture
research_publication = research_fixture


@pytest_asyncio.fixture
async def pg():
    """Writer creates its own ordinary tables inside an empty UUID schema."""
    from backend.shared.database_manager_v2 import DatabaseConfig
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from types import SimpleNamespace
    from uuid import uuid4

    if os.getenv("QM_JP_TEST_PG") != "1":
        pytest.skip("UUID PostgreSQL opt-in")
    schema = "jp_prediction_test_" + uuid4().hex
    admin = create_async_engine(DatabaseConfig().get_master_url())
    engine = None
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            DatabaseConfig().get_master_url(),
            connect_args={"server_settings": {"search_path": schema}},
        )
        yield SimpleNamespace(
            sessions=async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def provenance(version, day, run="real-run"):
    return {
        "market": "JP",
        "data_version": version,
        "data_trade_date": day,
        "prediction_trade_date": "2026-10-06" if day == "2026-10-05" else "2026-09-30",
        "run_id": run,
    }


@pytest.fixture
def two_publications(research_publication):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.jp_features import build_jp_features

    source, root, first = research_publication
    with duckdb.connect(str(source)) as db:
        db.execute(
            "INSERT INTO research.calendar VALUES ('2026-10-05','1'),('2026-10-06','1')"
        )
        for table in ("master", "daily_prices", "topix"):
            db.execute(
                f"INSERT INTO research.{table} SELECT * REPLACE(DATE '2026-10-05' AS Date) FROM research.{table} WHERE Date='2026-10-02'"
            )
        db.execute(
            "UPDATE research.daily_prices SET O=123,H=124,L=122,C=123 WHERE Date='2026-10-05'"
        )
    import_jquants_snapshot(source, root)

    def evaluator(hub, cache, batch_size, workers, start, end, save):
        frame = hub.fetch_daily_kline_batch(
            ["JP72030", "JP216A0", "JP13370"], start, end
        ).rename(columns={"trade_date": "date"})
        names = [f"feature_{i}" for i in range(158)]
        frame = pd.concat(
            [
                frame,
                pd.DataFrame(
                    {n: [10 * (i + 1)] * len(frame) for i, n in enumerate(names)},
                    index=frame.index,
                ),
            ],
            axis=1,
        )
        save(
            0,
            frame[
                [
                    "symbol",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    *names,
                ]
            ],
            names,
        )

    second = build_jp_features(root, evaluator=evaluator)["version"]
    return root, first.name, second


@pytest.mark.asyncio
async def test_new_day_and_partial_rows_keep_actual_publication(
    two_publications, tmp_path, monkeypatch
):
    from backend.services.api.routers import research_service as service
    from backend.services.api.routers.research import get_batch_features
    from backend.services.api.routers.research_schemas import BatchFeaturesRequest
    from backend.services.engine.data_platform.jp_publication import publish_pointer

    root, first, second = two_publications
    model = tmp_path / "model"
    model.mkdir()
    pred = model / "pred.parquet"
    old = provenance(first, "2026-09-29", "old")
    new = provenance(second, "2026-09-29", "partial")
    latest = provenance(second, "2026-10-05", "new")
    merge_signals_into_pred(
        pred,
        [
            (
                "2026-09-29",
                [
                    {"symbol": "JP72030", "score": 0.1, "data_provenance": old},
                    {"symbol": "JP216A0", "score": 0.2, "data_provenance": old},
                ],
            )
        ],
        create_if_missing=True,
    )
    merge_signals_into_pred(
        pred,
        [
            (
                "2026-09-29",
                [{"symbol": "JP216A0", "score": 0.9, "data_provenance": new}],
            ),
            (
                "2026-10-05",
                [{"symbol": "JP72030", "score": 1.23, "data_provenance": latest}],
            ),
        ],
    )
    part = pd.read_parquet(model / "pred_daily/dt=20260929/data.parquet")
    assert set(part.symbol) == {"JP72030", "JP216A0"}
    assert (
        json.loads(part.loc[part.symbol.eq("JP72030")].iloc[0].data_provenance) == old
    )
    monkeypatch.setattr(
        service,
        "_model_market",
        AsyncMock(return_value=(str(model), "JP", {"jp_data_version": first})),
    )
    monkeypatch.setattr(
        service, "_get_quantdb_stock_names", lambda: pytest.fail("JP must not use CN")
    )
    service._UNIVERSE_CACHE.clear()
    saved = await service.get_research_universe_by_date(
        "t", "7", "model", "2026-10-05", 100
    )
    assert saved["data"]["dataVersion"] == second
    assert saved["data"]["items"][0]["score"] == 1.23
    publish_pointer(root, first)
    request = BatchFeaturesRequest(
        symbols=["JP72030"],
        fields=["closePrice"],
        trade_date="2026-10-05",
        market="JP",
        model_id="model",
    )
    response = await get_batch_features(request, {"tenant_id": "t", "user_id": "7"})
    assert response["data"]["items"][0]["values"]["closePrice"] == 123
    assert response["data"]["items"][0]["dataVersion"] == second
    mixed = await service.get_research_universe_by_date(
        "t", "7", "model", "2026-09-29", 100
    )
    assert mixed["data"]["dataVersion"] is None
    batch = await get_batch_features(
        BatchFeaturesRequest(
            symbols=["JP72030", "JP216A0"],
            fields=["feature0"],
            trade_date="2026-09-29",
            market="JP",
            model_id="model",
        ),
        {"tenant_id": "t", "user_id": "7"},
    )
    assert {r["symbol"]: r["values"]["feature0"] for r in batch["data"]["items"]} == {
        "72030.JP": 1,
        "216A0.JP": 10,
    }
    # A cached same-day view must reflect a second partial update immediately.
    merge_signals_into_pred(
        pred,
        [("2026-09-29", [{"symbol": "JP72030", "score": 0.8, "data_provenance": new}])],
    )
    refreshed = await service.get_research_universe_by_date(
        "t", "7", "model", "2026-09-29", 100
    )
    assert refreshed["data"]["dataVersion"] == second


def test_shap_maps_logical_columns_in_model_order_and_exact_day(
    two_publications, tmp_path
):
    from backend.services.simulation.jp.model_snapshot import read_model_snapshot
    from backend.services.api.routers.research_service import _compute_shap_drivers_sync

    _, first, second = two_publications
    columns = ["logical_d", "logical_b", "logical_a", "logical_c"]
    mappings = dict(
        zip(columns, ["feature_3", "feature_1", "feature_0", "feature_2"], strict=False)
    )
    metadata = {
        "context": {"market": "JP"},
        "jp_data_version": first,
        "factor_source": "l1_factors",
        "factor_field_sources": mappings,
        "feature_columns": columns,
        "framework": "lightgbm",
        "model_file": "model.txt",
    }
    source = provenance(second, "2026-10-05")
    row, available = read_model_snapshot(
        metadata, "JP72030", date(2026, 10, 5), columns, provenance=source
    )
    assert available == columns and row.tolist() == [40, 20, 10, 30]
    assert (
        read_model_snapshot(
            metadata, "JP13370", date(2026, 10, 5), columns, provenance=source
        )
        is None
    )
    rng = np.random.default_rng(81)
    x = rng.normal(size=(128, 4))
    booster = lgb.train(
        {
            "objective": "regression",
            "verbosity": -1,
            "num_threads": 1,
            "min_data_in_leaf": 3,
        },
        lgb.Dataset(x, label=x @ np.arange(1, 5), feature_name=columns),
        num_boost_round=20,
    )
    booster.save_model(str(tmp_path / "model.txt"))
    (tmp_path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    drivers = _compute_shap_drivers_sync(
        "model",
        "JP72030",
        "2026-10-05",
        "JP",
        provenance=source,
        model_storage_path=str(tmp_path),
    )
    assert {r["name"]: r["value"] for r in drivers} == dict(
        zip(columns, [40, 20, 10, 30], strict=False)
    )
    assert (
        _compute_shap_drivers_sync(
            "model", "JP72030", "2026-10-05", "JP", model_storage_path=str(tmp_path)
        )
        is None
    )
    with pytest.raises(ValueError, match="input date"):
        read_model_snapshot(
            metadata, "JP72030", date(2026, 10, 2), columns, provenance=source
        )
    with pytest.raises(ValueError, match="missing an inference feature"):
        read_model_snapshot(
            {**metadata, "factor_field_sources": {**mappings, "logical_a": "missing"}},
            "JP72030",
            date(2026, 10, 5),
            columns,
            provenance=source,
        )


def test_shap_uses_actual_full_day_cross_section_preprocessing(two_publications):
    from backend.services.simulation.jp.model_snapshot import read_model_snapshot
    from backend.services.engine.inference.templates.inference_parquet import (
        load_date_data,
        preprocess,
    )

    root, _, version = two_publications
    metadata = {
        "context": {"market": "JP"},
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "feature_columns": ["feature_0", "feature_1", "feature_2", "feature_3"],
        "preprocessing": {"enabled": True, "winsorize": False},
    }
    source = provenance(version, "2026-10-05")
    actual = load_date_data("2026-10-05", root / "versions" / version, metadata)
    x, symbols = preprocess(actual, metadata)
    row, columns = read_model_snapshot(
        metadata,
        "JP72030",
        date(2026, 10, 5),
        metadata["feature_columns"],
        provenance=source,
    )
    assert columns == list(x.columns)
    assert row.tolist() == x.iloc[symbols.index("JP72030")].tolist()
    assert row.tolist() != [10, 20, 30, 40]


@pytest.mark.asyncio
async def test_legacy_scores_are_visible_without_invented_inputs(
    two_publications, tmp_path, monkeypatch
):
    from backend.services.api.routers import research_service as service
    from backend.services.api.routers.research import get_batch_features
    from backend.services.api.routers.research_schemas import BatchFeaturesRequest

    _, first, _ = two_publications
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0", "JP13370"],
            "trade_date": ["2026-09-29"] * 3,
            "pred": [0.5, None, np.nan],
        }
    ).to_parquet(tmp_path / "pred.parquet")
    assert len(read_pred_sources(tmp_path / "pred.parquet", "2026-09-29")) == 1
    monkeypatch.setattr(
        service,
        "_model_market",
        AsyncMock(return_value=(str(tmp_path), "JP", {"jp_data_version": first})),
    )
    service._UNIVERSE_CACHE.clear()
    payload = await service.get_research_universe_by_date(
        "t", "7", "legacy", "2026-09-29", 100
    )
    assert payload["data"]["items"][0]["score"] == 0.5
    assert payload["data"]["items"][0]["dataVersion"] is None
    result = await get_batch_features(
        BatchFeaturesRequest(
            symbols=["JP72030"],
            fields=["closePrice"],
            trade_date="2026-09-29",
            market="JP",
            model_id="legacy",
        ),
        {"tenant_id": "t", "user_id": "7"},
    )
    assert result["data"]["items"] == [] and result["data"]["sourceWarnings"]


def test_training_emitter_stamps_actual_pin_and_does_not_invent_signal_day(
    two_publications, tmp_path
):
    import ast
    from backend.services.engine.inference.prediction_provenance import (
        stamp_training_predictions,
    )
    from backend.services.api.routers.research_service import _compute_shap_drivers_sync

    root, first, _ = two_publications
    frame = pd.DataFrame(
        {
            "symbol": ["JP72030"] * 2,
            "trade_date": [date(2026, 9, 29), date(2026, 10, 2)],
            "pred": [0.5, 0.6],
        }
    )
    stamped = stamp_training_predictions(frame, root / "versions" / first, "job-uuid")
    stamped.to_parquet(tmp_path / "pred.parquet")
    early = read_pred_sources(tmp_path / "pred.parquet", "2026-09-29")[0][
        "data_provenance"
    ]
    last = read_pred_sources(tmp_path / "pred.parquet", "2026-10-02")[0][
        "data_provenance"
    ]
    assert early["data_version"] == first and early["run_id"] == "training:job-uuid"
    assert last["prediction_trade_date"] is None
    assert "data_provenance" not in frame.columns
    assert (
        _compute_shap_drivers_sync(
            "model",
            "JP72030",
            "2026-09-29",
            "JP",
            provenance=early,
            model_storage_path=str(tmp_path),
        )
        is None
    )
    tree = ast.parse(Path("docker/training/train.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "stamp_training_predictions"
    ]
    assert len(calls) == 2  # single and multi-model actual artifact emitters
    with pytest.raises(ValueError, match="already pinned"):
        stamp_training_predictions(frame, root, "job-uuid")


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PostgreSQL opt-in")
@pytest.mark.asyncio
async def test_real_writer_jsonb_and_owned_snapshot_exact_date(
    pg, two_publications, tmp_path, monkeypatch
):
    from sqlalchemy import text
    from backend.services.engine.inference.script_runner import InferenceScriptRunner
    from backend.services.api.routers import research_service as service
    from contextlib import asynccontextmanager

    root, first, second = two_publications
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    model = tmp_path / "model"
    model.mkdir()
    (model / "metadata.json").write_text(json.dumps({"context": {"market": "JP"}}))
    runner = InferenceScriptRunner(
        primary_model_dir=str(model), primary_model_id="model"
    )
    async with pg.sessions() as db:
        await db.execute(
            text("""CREATE TABLE engine_signal_scores (
        id SERIAL PRIMARY KEY, run_id TEXT, tenant_id TEXT,user_id TEXT,trade_date DATE,symbol TEXT,
        model_version TEXT,feature_version TEXT,light_score FLOAT,tft_score FLOAT,fusion_score FLOAT,
        risk_weight FLOAT,regime TEXT,signal_side TEXT,expected_price FLOAT,quality JSONB,score_rank INTEGER,created_at TIMESTAMPTZ,
        UNIQUE(tenant_id,user_id,trade_date,symbol,model_version,feature_version,run_id))""")
        )
        await db.execute(
            text(
                "CREATE TABLE engine_feature_runs (run_id TEXT,tenant_id TEXT,user_id TEXT,trade_date DATE,source TEXT,feature_version TEXT)"
            )
        )
        await db.execute(
            text("""CREATE TABLE qm_research_candidate_snapshot (
        tenant_id TEXT,user_id TEXT,run_id TEXT,model_id TEXT,data_trade_date DATE,prediction_trade_date DATE,
        symbol TEXT,fusion_score FLOAT,score_rank INTEGER,signal_side TEXT,expected_price FLOAT,
        universe_tag TEXT,confidence_level TEXT,created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ,
        UNIQUE(tenant_id,user_id,run_id,symbol))""")
        )
        await db.execute(
            text(
                "CREATE TABLE qm_user_models(tenant_id TEXT,user_id TEXT,model_id TEXT,storage_path TEXT,metadata_json JSONB)"
            )
        )
        await db.execute(
            text("CREATE TABLE qm_model_inference_runs(run_id TEXT,model_id TEXT)")
        )
        await db.execute(
            text(
                "INSERT INTO qm_user_models VALUES ('test','7','model',:path,CAST(:meta AS JSONB))"
            ),
            {
                "path": str(model),
                "meta": json.dumps(
                    {"context": {"market": "JP"}, "jp_data_version": first}
                ),
            },
        )
        await db.execute(
            text("INSERT INTO qm_model_inference_runs VALUES ('actual','model')")
        )
        schema = (await db.execute(text("SELECT current_schema()"))).scalar_one()
        assert schema.startswith("jp_prediction_test_")
        await db.commit()
        # Production inference uses psycopg2, not an asyncpg run_sync adapter.
        # Restrict that real synchronous writer to the same temporary UUID schema.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        import asyncio

        sync_engine = create_engine(
            pg.sessions.kw["bind"].url.set(drivername="postgresql+psycopg2"),
            connect_args={"options": f"-csearch_path={schema}"},
        )

        def write():
            with Session(sync_engine) as sync:
                assert (
                    sync.execute(text("SELECT current_schema()")).scalar_one() == schema
                )
                runner._persist_locked(
                    db=sync,
                    run_id="actual",
                    prediction_trade_date="2026-10-06",
                    tenant_id="test",
                    user_id="7",
                    signals=[{"symbol": "JP72030", "score": 1.23}],
                    symbols=["JP72030"],
                    scores=[1.23],
                    feature_dim=4,
                    model_name="model",
                    feature_version="native",
                    inference_date="2026-10-05",
                    signal_sides=["BUY"],
                    partial=True,
                    publication_data_dir=str(root / "versions" / second),
                )

        try:
            await asyncio.to_thread(write)
        finally:
            sync_engine.dispose()
        quality, price = (
            await db.execute(
                text("SELECT quality,expected_price FROM engine_signal_scores")
            )
        ).one()
        assert quality["data_provenance"] == provenance(second, "2026-10-05", "actual")
        assert price == 123
        await db.commit()

    @asynccontextmanager
    async def sessions(**kwargs):
        async with pg.sessions() as db:
            yield db

    monkeypatch.setattr(service, "get_session", sessions)
    rows = await service.selected_prediction_rows(
        "test", "7", "model", "2026-10-05", "actual"
    )
    assert rows[0]["data_provenance"] == quality["data_provenance"]
    assert (
        await service.selected_prediction_rows("test", "7", "model", "2026-10-05")
        == rows
    )
    monkeypatch.setattr(
        service,
        "get_available_models",
        AsyncMock(
            return_value={
                "data": {
                    "models": [
                        {"modelId": "model", "name": "native", "modelType": "lightgbm"}
                    ]
                }
            }
        ),
    )
    from backend.services.engine.data_platform.jp_publication import publish_pointer

    publish_pointer(root, first)
    stock = await service.predict_single_stock(
        "test", "7", "JP72030", model_id="model", target_date="2026-10-05", market="JP"
    )
    assert stock["data"]["predicted_score"] == 1.23
    assert stock["data"]["current_price"] == 123
    assert stock["data"]["dataVersion"] == second
    assert stock["data"]["as_of_date"] == "2026-10-05"
    with pytest.raises(ValueError, match="not Japanese"):
        await service.selected_prediction_rows(
            "test", "foreign", "model", "2026-10-05", "actual"
        )
    for key, value in [("run_id", "other"), ("data_trade_date", "2026-10-02")]:
        async with pg.sessions() as db:
            bad = {"data_provenance": {**quality["data_provenance"], key: value}}
            await db.execute(
                text("UPDATE engine_signal_scores SET quality=CAST(:quality AS JSONB)"),
                {"quality": json.dumps(bad)},
            )
            await db.commit()
        with pytest.raises(ValueError, match="recorded prediction source"):
            await service._jp_snapshot_scores(
                "test", "7", "model", "actual", "2026-10-05"
            )
