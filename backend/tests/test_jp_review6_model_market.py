"""Both registered JP metadata formats must route to the same publication."""

import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from backend.shared.model_metadata import (
    declared_model_market,
    inference_model_market,
)
from backend.services.engine.inference import data_loader, script_runner
from backend.services.engine.inference.templates import inference_parquet as single
from backend.services.engine.inference.templates import (
    inference_ensemble_src as ensemble,
)
from backend.tests.test_jp_model_evaluation import (
    evaluation_publication as publication_fixture,
    snapshot as source_fixture,
    SESSIONS,
)
from backend.tests.test_jp_ensemble_preprocessing import CapturingMember
from backend.services.engine.inference.gap_backfill import _model_market

evaluation_publication = publication_fixture
snapshot = source_fixture


@pytest.mark.parametrize(
    "metadata",
    [
        {"market": "JP"},
        {"context": {"market": "JP"}},
        {"market": "jp", "context": {"market": "CN"}},
        {"context": json.dumps({"market": "JP"})},
    ],
)
def test_supported_jp_declarations(metadata):
    assert declared_model_market(metadata) == "JP"
    assert inference_model_market(metadata) == "JP"
    assert data_loader._is_jp_model(metadata)
    assert _model_market(metadata) == "JP"


@pytest.mark.parametrize("market", ["CN", "US", "HK"])
def test_old_inference_routing_remains_context_only(market):
    assert inference_model_market({"market": market}) == "CN"
    assert inference_model_market({"context": {"market": market}}) == market
    assert inference_model_market({"market": market}, default="A") == "A"


def test_explicit_registry_market_wins_over_context():
    metadata = {"market": "CN", "context": {"market": "JP"}}
    assert declared_model_market(metadata) == "CN"
    assert inference_model_market(metadata) == "CN"
    assert not data_loader._is_jp_model(metadata)
    assert _model_market(metadata) == "CN"


@pytest.mark.parametrize("location", ["market", "context"])
def test_real_publication_evaluation_single_and_ensemble_agree(
    evaluation_publication, location
):
    data = evaluation_publication
    metadata = dict(data.meta)
    if location == "market":
        metadata["market"] = "JP"
        metadata["context"] = {
            key: value for key, value in metadata["context"].items() if key != "market"
        }
    assert data_loader.resolve_data_dir(data.root, metadata) == data.publication
    assert script_runner.InferenceScriptRunner._resolve_primary_active_data_source(
        metadata
    ) == str(data.publication)
    evaluation = data_loader.load_date_data(SESSIONS[3], data.root, metadata)
    inference = single.load_date_data(SESSIONS[3], data.root, metadata)
    assert evaluation is not None and not evaluation.empty
    assert inference is not None and not inference.empty
    pd.testing.assert_frame_equal(evaluation, inference)
    member = CapturingMember()
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
    )

    status = QuantDBFactorReader(data.root, market="JP").describe("l1_factors")
    assert status.ready, status.reason
    raw_day = ensemble.load_day_data(SESSIONS[3], data.root, market="JP")
    scores = ensemble.predict_with_model(member, metadata, raw_day)
    assert len(scores) == len(inference)
    assert member.values.shape == (len(inference), 2)
    # Exercise the real command constructed by the runner, not a fake process
    # that bypasses argparse. Output and artifacts belong to this temp fixture.
    (data.model_dir / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    # The evaluation fixture's predictor deliberately accepts DataFrames; the
    # actual sklearn template contract is ndarray. Use a real persisted model.
    from sklearn.linear_model import LinearRegression

    predictor = LinearRegression().fit([[0, 0], [0, 1], [0, 2]], [0, 1, 2])
    with (data.model_dir / "model.pkl").open("wb") as stream:
        pickle.dump(predictor, stream)
    output = data.model_dir / "cli_prediction.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(single.__file__)),
            "--model-dir",
            str(data.model_dir),
            "--data-dir",
            str(data.publication),
            "--date",
            SESSIONS[3],
            "--output",
            str(output),
            "--market",
            "JP",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    signals = json.loads(output.read_text(encoding="utf-8"))
    assert len(signals) == len(inference)
    assert all(row["symbol"].startswith("JP") for row in signals)


def test_single_jp_cli_and_legacy_no_market_parse(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["inference.py", "--output", "unused.json", "--market", "JP"]
    )
    assert single.parse_args().market == "JP"
    monkeypatch.setattr(sys, "argv", ["inference.py", "--output", "unused.json"])
    assert single.parse_args().market is None


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", ["context", "top", "json_context"])
async def test_real_system_model_record_calendar_scan_and_covering_swap(
    evaluation_publication, monkeypatch, declaration
):
    import copy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from backend.shared.model_registry import (
        ModelRegistryService,
        model_registry_service,
    )
    from backend.services.api.routers import model_training
    from backend.services.engine.qlib_app.services.backtest_service_runtime import (
        QlibBacktestServiceRuntimeMixin,
    )

    data = evaluation_publication
    metadata = copy.deepcopy(data.meta)
    if declaration == "top":
        metadata["market"] = "JP"
        metadata["context"].pop("market")
    elif declaration == "json_context":
        metadata["context"] = json.dumps(metadata["context"])
    (data.model_dir / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    service = ModelRegistryService()
    service.production_models_root = data.production
    asynchronous = await service._resolve_system_model_record(data.model_dir.name)
    synchronous = service._resolve_system_model_record_sync(data.model_dir.name)
    for record in [asynchronous, synchronous]:
        assert declared_model_market(record["metadata_json"]) == "JP"
        assert record["metadata_json"]["context"]["commission_rate"] == 0.002
        assert record["model_file"] == "model.pkl"
    assert model_training._get_model_market(data.model_dir) == "JP"
    assert model_training._get_model_calendar(data.model_dir) == "XTKS"
    monkeypatch.setattr(model_training, "_PRODUCTION_DIR", data.production)
    assert model_training._load_production_models()[0]["market"] == "JP"

    # Use the real covering-model traversal and parquet footer reader. The
    # registry boundary returns only this temporary owner's model; no DB use.
    pd.DataFrame(
        {"trade_date": pd.to_datetime(SESSIONS[:2]), "pred": [1.0, 2.0]}
    ).to_parquet(data.model_dir / "pred.parquet")
    model = {**asynchronous, "metadata_json": metadata}
    monkeypatch.setattr(
        model_registry_service, "list_models", AsyncMock(return_value=[model])
    )
    runtime = QlibBacktestServiceRuntimeMixin()
    for requested, expected in [("JP", True), ("CN", False)]:
        request = SimpleNamespace(
            tenant_id="temporary-owner", user_id="7", market=requested, model_id="old"
        )
        swapped = await runtime._try_swap_to_covering_model(
            request, pd.Timestamp(SESSIONS[0])
        )
        assert swapped is expected
        assert request.model_id == (asynchronous["model_id"] if expected else "old")


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "US", "HK"])
async def test_old_system_formatter_and_calendar_routing_remain_unchanged(
    tmp_path, monkeypatch, market
):
    from backend.shared.model_registry import ModelRegistryService
    from backend.services.api.routers import model_training

    metadata = {"market": market, "context": {"commission_rate": 0.002}}
    directory = tmp_path / "legacy"
    directory.mkdir()
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    service = ModelRegistryService()
    service.production_models_root = tmp_path
    for record in [
        await service._resolve_system_model_record("legacy"),
        service._resolve_system_model_record_sync("legacy"),
    ]:
        assert "market" not in record["metadata_json"]
        assert record["metadata_json"]["context"] == metadata["context"]
    assert model_training._get_model_market(directory) == "CN"
    assert model_training._get_model_calendar(directory) == "SSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "US", "HK"])
@pytest.mark.parametrize("encoded", [False, True])
async def test_jp_context_cannot_replace_explicit_foreign_registration(
    tmp_path, market, encoded, monkeypatch
):
    from backend.shared.model_registry import ModelRegistryService
    from backend.services.api.routers import model_training
    from backend.services.engine.qlib_app.services.backtest_service_runtime import (
        QlibBacktestServiceRuntimeMixin,
    )

    context = {"market": "JP", "commission_rate": 0.002}
    metadata = {
        "market": market,
        "context": json.dumps(context) if encoded else context,
    }
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    service = ModelRegistryService()
    service.production_models_root = tmp_path
    for record in [
        await service._resolve_system_model_record("artifact"),
        service._resolve_system_model_record_sync("artifact"),
    ]:
        assert declared_model_market(record["metadata_json"]) == market
        assert record["metadata_json"]["context"] == context
    assert model_training._get_model_market(directory) == market
    assert (
        model_training._get_model_calendar(directory)
        == {"CN": "SSE", "US": "NYSE", "HK": "HKEX"}[market]
    )
    assert (
        QlibBacktestServiceRuntimeMixin._infer_model_market({"metadata_json": metadata})
        == market
    )
    assert inference_model_market(metadata) == market
    assert not data_loader._is_jp_model(metadata)
    monkeypatch.setattr(model_training, "_PRODUCTION_DIR", tmp_path)
    assert model_training._load_production_models()[0]["market"] == market


@pytest.mark.parametrize("top", ["CN", "US", "HK"])
@pytest.mark.parametrize("context", ["CN", "US", "HK"])
def test_pure_legacy_conflicting_declarations_keep_context_routing(
    top, context, tmp_path
):
    from backend.services.api.routers import model_training
    from backend.services.engine.qlib_app.services.backtest_service_runtime import (
        QlibBacktestServiceRuntimeMixin,
    )

    metadata = {"market": top, "context": {"market": context}}
    (tmp_path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert inference_model_market(metadata) == context
    assert model_training._get_model_market(tmp_path) == context
    assert (
        QlibBacktestServiceRuntimeMixin._infer_model_market({"metadata_json": metadata})
        == context
    )
