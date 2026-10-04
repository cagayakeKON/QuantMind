"""A candidate score must retain the inputs observed by its consumer."""

from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from backend.services.api.routers import research_service as service
from backend.services.api.routers import research as router
from backend.services.api.routers.research_schemas import BatchFeaturesRequest
from backend.services.engine.inference.pred_merge import merge_signals_into_pred
from backend.tests.test_jp_prediction_provenance import (
    provenance,
    research_publication as research_fixture,
    snapshot as snapshot_fixture,
    two_publications as publication_fixture,
)

snapshot = snapshot_fixture
research_publication = research_fixture
two_publications = publication_fixture


def write_score(pred, version, score):
    source = provenance(version, "2026-09-29", version)
    merge_signals_into_pred(
        pred,
        [
            (
                "2026-09-29",
                [{"symbol": "JP72030", "score": score, "data_provenance": source}],
            )
        ],
        create_if_missing=True,
    )
    return source


def request(score, source):
    return BatchFeaturesRequest(
        symbols=["72030.JP"],
        fields=["feature0"],
        trade_date="2026-09-29",
        market="JP",
        model_id="owned-model",
        observed_predictions=[
            {
                "symbol": "JP72030",
                "score": score,
                "data_provenance": source,
            }
        ],
    )


@pytest.mark.asyncio
async def test_actual_list_then_partial_inference_rejects_mixed_score_features(
    two_publications,
    tmp_path,
    monkeypatch,
):
    _, first, second = two_publications
    pred = tmp_path / "pred.parquet"
    write_score(pred, first, 0.1)
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=(str(tmp_path), "JP", {}))
    )
    old = (
        await service.get_research_universe_by_date(
            "t", "7", "owned-model", "2026-09-29", 100
        )
    )["data"]["items"][0]
    write_score(pred, second, 0.9)
    with pytest.raises(HTTPException) as raised:
        await router.get_batch_features(
            request(old["score"], old["dataProvenance"]),
            {"tenant_id": "t", "user_id": "7"},
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "PREDICTION_SOURCE_CHANGED"
    from backend.shared.error_contract import install_error_contract_handlers

    app = FastAPI()
    app.include_router(router.router)
    install_error_contract_handlers(app)
    app.dependency_overrides[router.get_current_user] = lambda: {
        "tenant_id": "t",
        "user_id": "7",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-test"
    ) as client:
        rejected = await client.post(
            "/api/v1/research/batch-features",
            json=request(old["score"], old["dataProvenance"]).model_dump(),
        )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "HTTP_409"
    assert rejected.json()["detail"]["code"] == "PREDICTION_SOURCE_CHANGED"
    new = (
        await service.get_research_universe_by_date(
            "t", "7", "owned-model", "2026-09-29", 100
        )
    )["data"]["items"][0]
    response = await router.get_batch_features(
        request(new["score"], new["dataProvenance"]), {"tenant_id": "t", "user_id": "7"}
    )
    item = response["data"]["items"][0]
    assert new["score"] == 0.9
    assert item["values"]["feature0"] == 10
    assert item["dataProvenance"] == new["dataProvenance"]


@pytest.mark.asyncio
async def test_replacement_after_validation_keeps_the_observed_immutable_inputs(
    two_publications,
    tmp_path,
    monkeypatch,
):
    _, first, second = two_publications
    pred = tmp_path / "pred.parquet"
    before = write_score(pred, first, 0.1)
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=(str(tmp_path), "JP", {}))
    )
    original = router.get_batch_full_features_service

    async def replace_during_load(*args):
        write_score(pred, second, 0.9)
        return await original(*args)

    monkeypatch.setattr(router, "get_batch_full_features_service", replace_during_load)
    response = await router.get_batch_features(
        request(0.1, before), {"tenant_id": "t", "user_id": "7"}
    )
    item = response["data"]["items"][0]
    assert item["dataProvenance"] == before
    assert item["values"]["feature0"] == 1


@pytest.mark.asyncio
async def test_score_change_is_detected_even_when_source_tags_are_unchanged(
    two_publications,
    tmp_path,
    monkeypatch,
):
    _, first, _ = two_publications
    pred = tmp_path / "pred.parquet"
    before = write_score(pred, first, 0.1)
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=(str(tmp_path), "JP", {}))
    )
    write_score(pred, first, 0.2)
    with pytest.raises(HTTPException) as raised:
        await router.get_batch_features(
            request(0.1, before), {"tenant_id": "t", "user_id": "7"}
        )
    assert raised.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "duplicate", "invalid"])
async def test_observations_must_cover_the_requested_rows_without_alias_duplicates(
    two_publications,
    tmp_path,
    monkeypatch,
    mode,
):
    _, first, _ = two_publications
    before = write_score(tmp_path / "pred.parquet", first, 0.1)
    monkeypatch.setattr(
        service, "_model_market", AsyncMock(return_value=(str(tmp_path), "JP", {}))
    )
    req = request(0.1, before)
    if mode == "missing":
        req.observed_predictions = []
    elif mode == "duplicate":
        req.observed_predictions.append(
            req.observed_predictions[0].model_copy(update={"symbol": "72030.JP"})
        )
    else:
        req.observed_predictions[0].data_provenance = {
            **before,
            "data_version": "../escape",
        }
    with pytest.raises(HTTPException) as raised:
        await router.get_batch_features(req, {"tenant_id": "t", "user_id": "7"})
    assert raised.value.status_code == 422
