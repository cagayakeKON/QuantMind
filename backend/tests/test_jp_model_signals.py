"""Retain saved-model provenance checks after removing private JP order APIs."""

import json
from types import SimpleNamespace

import pytest

from backend.services.simulation.jp import model_signals
from backend.services.simulation.jp.rules import RuleDataMissing


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting", [False, True])
async def test_model_publication_metadata_missing_or_conflicting_is_rejected(
    tmp_path, monkeypatch, conflicting
):
    # Preserve the independent metadata regression from the removed order API
    # suite. Model resolution still uses the original owned registry entry.
    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "context": {"market": "JP"},
    }
    if conflicting:
        meta.update(
            jp_data_version="original-publication",
            factor_coverage={"jp_data_version": "another-publication"},
        )
    (tmp_path / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    async def resolve(**kwargs):
        assert kwargs["market"] == "JP" and kwargs["model_id"] == "saved-model"
        assert kwargs["user_id"] == "alice" and kwargs["tenant_id"] == "tenant-a"
        return SimpleNamespace(
            fallback_used=False,
            effective_model_id="saved-model",
            storage_path=str(tmp_path),
        )

    monkeypatch.setattr(
        model_signals.model_registry_service, "resolve_effective_model", resolve
    )
    with pytest.raises(RuleDataMissing, match="metadata"):
        await model_signals.resolve_model("tenant-a", "alice", "saved-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["top_level", "trainer_coverage"])
async def test_saved_model_publication_is_resolved_from_both_existing_layouts(
    tmp_path, monkeypatch, layout
):
    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "context": {"market": "JP"},
    }
    if layout == "top_level":
        meta["jp_data_version"] = "saved-publication"
    else:
        meta["factor_coverage"] = {"jp_data_version": "saved-publication"}
    (tmp_path / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    async def resolve(**kwargs):
        assert kwargs["market"] == "JP" and kwargs["model_id"] == "saved-model"
        assert kwargs["user_id"] == "alice" and kwargs["tenant_id"] == "tenant-a"
        return SimpleNamespace(
            fallback_used=False,
            effective_model_id="saved-model",
            storage_path=str(tmp_path),
        )

    monkeypatch.setattr(
        model_signals.model_registry_service, "resolve_effective_model", resolve
    )
    directory, resolved = await model_signals.resolve_model(
        "tenant-a", "alice", "saved-model"
    )
    assert directory == tmp_path
    assert resolved == {**meta, "jp_data_version": "saved-publication"}
    assert json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8")) == meta
