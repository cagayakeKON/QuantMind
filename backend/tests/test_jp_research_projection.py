"""Real small JP publications for the public research skeleton/projection."""

from datetime import date
from contextlib import asynccontextmanager
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import duckdb
import pandas as pd
import pytest

from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture
def publications(snapshot, tmp_path, monkeypatch):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.jp_publication import publish_pointer

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.master SET CoName='Historical Toyota', S33Nm='Historical sector' WHERE Date='2026-09-28' AND Code='72030'"
        )
        conn.execute(
            "CREATE TABLE research.valuation(Date DATE, Code VARCHAR, PER DOUBLE, PBR DOUBLE, ROE DOUBLE, EPS DOUBLE, BPS DOUBLE, MktCap DOUBLE)"
        )
        conn.execute(
            "INSERT INTO research.valuation VALUES ('2026-09-28','72030',12,1.5,0.15,4,30,123)"
        )
    root = tmp_path / "published"
    raw = import_jquants_snapshot(snapshot, root)
    for version, value in [("research-v1", 0.1), ("research-v2", 9)]:
        folder = root / "versions" / version
        shutil.copytree(root / "versions" / raw["version"], folder)
        factors = folder / "6_ml_datasets/l1_factors/dt=20260929"
        factors.mkdir(parents=True)
        pd.DataFrame(
            {
                "symbol": ["72030.JP"],
                "time": [pd.Timestamp("2026-09-29")],
                "dt": [20260929],
                "KMID": [value],
            }
        ).to_parquet(factors / "part.parquet")
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest["version"] = version
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    publish_pointer(root, "research-v2")
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root


def test_native_normalization_units_asof_and_version_are_kept(
    publications, monkeypatch
):
    from backend.services.api.routers import research_features_service as service

    monkeypatch.setattr(service, "_get_hub", lambda: pytest.fail("JP must not read CN"))
    assert service.normalize_symbols(
        ["JP72030", "72030.JP", "JP216A0", "SH600036"], "JP"
    ) == ["72030.JP", "216A0.JP"]
    fields = [
        "closePrice",
        "amount",
        "pe",
        "pb",
        "roe",
        "totalMv",
        "KMID",
        "latestChange",
        "return1d",
        "return3d",
        "flowNetAmount",
    ]
    result = service.get_batch_full_features_sync(
        ["JP72030"], fields, "2026-09-29", "JP", "research-v1"
    )
    row = result["data"]["items"][0]
    assert row["symbol"] == "72030.JP" and row["tradeDate"] == "2026-09-29"
    values = row["values"]
    assert values["closePrice"] == 50
    assert values["amount"] == pytest.approx(50000 / 1e8)
    assert values["totalMv"] == pytest.approx(1.23)
    assert values["pe"] == 12 and values["pb"] == 1.5 and values["roe"] == 0.15
    assert values["KMID"] == 0.1  # current points to V2; model V1 stays fixed.
    assert values["latestChange"] == pytest.approx(0)
    assert values["return1d"] == pytest.approx(0)
    assert "return3d" not in values and "flowNetAmount" not in values
    newest = service.get_batch_full_features_sync(
        ["JP72030"], ["KMID"], "2026-09-29", "JP"
    )
    assert newest["data"]["items"][0]["values"]["KMID"] == 9
    retired = service.get_batch_full_features_sync(
        ["JP13370"], ["closePrice"], "2026-09-29", "JP", "research-v1"
    )
    assert retired["data"]["items"] == []  # no stale bar presented as this day's price


@pytest.mark.asyncio
async def test_real_pred_skeleton_uses_historical_master_and_pins_batch(
    publications, tmp_path, monkeypatch
):
    from backend.services.api.routers import research_service as service
    from backend.services.api.routers.research import get_batch_features
    from backend.services.api.routers.research_schemas import BatchFeaturesRequest

    model = tmp_path / "model"
    model.mkdir()
    pd.DataFrame(
        {"symbol": ["JP72030"], "trade_date": [date(2026, 9, 28)], "pred": [0.2]}
    ).to_parquet(model / "pred.parquet")
    metadata = {"context": {"market": "JP"}, "jp_data_version": "research-v1"}
    # Only external model ownership/DB lookup is isolated; pred/master/features are real.
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=(str(model), "JP", metadata))
    )
    monkeypatch.setattr(
        service,
        "_get_quantdb_stock_names",
        lambda: pytest.fail("JP must not read CN names"),
    )
    monkeypatch.setattr(
        service,
        "_load_quantdb_labels",
        lambda: pytest.fail("JP must not read CN labels"),
    )
    service._UNIVERSE_CACHE.clear()
    payload = await service.get_research_universe_by_date(
        "t", "7", "jp-model", "2026-09-28", 100
    )
    data = payload["data"]
    assert (
        data["market"] == "JP"
        and data["dataVersion"] == "research-v1"
        and data["currency"] == "JPY"
    )
    row = data["items"][0]
    assert row["code"] == "JP72030" and row["score"] == 0.2
    assert row["name"] == "Historical Toyota" and row["sector"] == "Historical sector"
    assert row["indexTags"] == [] and row["conceptTags"] == [] and not row["isHs300"]
    response = await get_batch_features(
        BatchFeaturesRequest(
            symbols=[row["code"]],
            fields=["closePrice", "pe"],
            trade_date="2026-09-28",
            market=data["market"],
            data_version=data["dataVersion"],
        ),
        {"tenant_id": "t", "user_id": "7"},
    )
    assert response["data"]["items"][0]["values"] == {"closePrice": 100.0, "pe": 12.0}


def test_native_missing_publication_never_uses_other_market(publications):
    from backend.services.api.routers.research_features_service import (
        get_batch_full_features_sync,
    )
    from backend.services.simulation.jp.research_features import model_publication

    with pytest.raises(ValueError, match="immutable publication"):
        model_publication({"context": {"market": "JP"}})
    with pytest.raises(ValueError, match="inconsistent"):
        model_publication(
            {
                "jp_data_version": "research-v1",
                "factor_coverage": {"jp_data_version": "research-v2"},
            }
        )
    with pytest.raises((ValueError, FileNotFoundError)):
        get_batch_full_features_sync(
            ["JP72030"], ["closePrice"], "2026-09-29", "JP", "missing"
        )


@pytest.mark.asyncio
async def test_snapshot_only_jp_model_keeps_pin_and_historical_skeleton(
    publications, monkeypatch
):
    from backend.services.api.routers import research_service as service
    from backend.services.api.routers.research_features_service import (
        get_batch_full_features_sync,
    )

    metadata = {"context": {"market": "JP"}, "jp_data_version": "research-v1"}
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=("", "JP", metadata))
    )
    monkeypatch.setattr(
        service, "_best_snapshot_run_for_date", AsyncMock(return_value="saved-run")
    )
    execute = AsyncMock(return_value=SimpleNamespace(all=lambda: [("JP72030", 0.2, 1)]))

    @asynccontextmanager
    async def session(**kwargs):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(service, "get_session", session)
    monkeypatch.setattr(
        service,
        "get_research_universe",
        lambda *args: pytest.fail("JP must not use the unpinned legacy fallback"),
    )
    service._UNIVERSE_CACHE.clear()
    result = await service.get_research_universe_by_date(
        "t", "7", "snapshot-only", "2026-09-28", 100
    )
    data = result["data"]
    assert data["market"] == "JP" and data["dataVersion"] == "research-v1"
    assert data["items"][0]["name"] == "Historical Toyota"
    assert data["items"][0]["runId"] == "saved-run"
    assert "amount" not in data["items"][0]  # no CN financial-unit projection
    assert execute.call_args.args[1] == {
        "tid": "t",
        "uid": "7",
        "mid": "snapshot-only",
        "rid": "saved-run",
        "day": "2026-09-28",
    }
    enriched = get_batch_full_features_sync(
        ["JP72030"],
        ["pe", "closePrice"],
        "2026-09-28",
        data["market"],
        data["dataVersion"],
    )
    assert enriched["data"]["items"][0]["values"] == {"pe": 12, "closePrice": 100}


def test_old_normalization_and_projection_contract_is_preserved(monkeypatch):
    from backend.services.api.routers import research_features_service as service

    assert service.normalize_symbols(["SH600036", "600036.SH", "JP72030"]) == [
        "600036.SH"
    ]

    def loader(symbols, wanted, dt):
        return {
            "600036.SH": {
                "sources": ["original"],
                "values": {"pe": 12},
                "symbol": "600036.SH",
            }
        }

    monkeypatch.setattr(service, "_load_projected_features", loader)
    assert service.get_batch_full_features_sync(["SH600036"], ["pe"], "2026-09-29")[
        "data"
    ]["items"][0]["sources"] == ["original"]
