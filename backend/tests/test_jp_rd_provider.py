"""Research providers retain JP source identity across publication changes."""

import json

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.qlib_data_builder import QlibDataBuilder
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator

snapshot = source_fixture


def test_versioned_provider_preserves_source_and_does_not_rebuild_cached_data(
    snapshot, tmp_path, monkeypatch
):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    publication = QuantJPDataHub(root).data_dir
    provider = prepare_jp_rd_provider(root)
    assert provider.parent.name == publication.name
    metadata = json.loads((provider / "research_source.json").read_text())
    assert metadata["market"] == "JP"
    assert metadata["data_version"] == publication.name
    symbols = (provider / "instruments/all.txt").read_text()
    assert "jp_13370\t2026-09-28\t2026-09-28" in symbols
    assert "jp_216a0" in symbols
    assert "jp_topix" not in symbols
    assert (provider / "features/jp_topix/close.day.bin").is_file()
    _, close = QlibDataBuilder._read_bin_file(
        provider / "features/jp_72030/close.day.bin"
    )
    _, factor = QlibDataBuilder._read_bin_file(
        provider / "features/jp_72030/factor.day.bin"
    )
    assert (close / factor).tolist() == pytest.approx([100, 50, 45])

    monkeypatch.setattr(
        QlibDataBuilder, "build_all", lambda *a, **kw: pytest.fail("cache rebuilt")
    )
    (root / "current.json").unlink()
    assert prepare_jp_rd_provider(root, publication=publication) == provider
    assert not (publication / "research_source.json").exists()


def test_new_publication_uses_a_new_research_provider(snapshot, tmp_path):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    old_publication = QuantJPDataHub(root).data_dir
    old = prepare_jp_rd_provider(root)
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.daily_prices SET Vo=2000 WHERE Code='72030'")
    import_jquants_snapshot(snapshot, root)
    # Raw-only sync keeps the usable research version and its cache identity.
    assert prepare_jp_rd_provider(root) == old
    assert QuantJPDataHub(root).data_dir == old_publication
    build_jp_features(root, evaluator=fake_evaluator)
    new = prepare_jp_rd_provider(root)
    assert old != new
    _, old_volume = QlibDataBuilder._read_bin_file(
        old / "features/jp_72030/volume.day.bin"
    )
    _, new_volume = QlibDataBuilder._read_bin_file(
        new / "features/jp_72030/volume.day.bin"
    )
    assert new_volume.tolist() == (old_volume * 2).tolist()
    assert prepare_jp_rd_provider(root, publication=old_publication) == old


def test_raw_cash_view_is_separate_from_unchanged_research_provider(snapshot, tmp_path):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    adjusted = prepare_jp_rd_provider(root)
    raw = prepare_jp_rd_provider(root, price_basis="raw")
    assert adjusted.name == "qlib_v3" and raw.name == "qlib_raw_v3"
    identity = json.loads((raw / "research_source.json").read_text())
    assert identity["price_basis"] == "raw" and identity["contract_version"] == 3
    assert "price_basis" not in json.loads(
        (adjusted / "research_source.json").read_text()
    )
    _, prices = QlibDataBuilder._read_bin_file(raw / "features/jp_72030/close.day.bin")
    _, factors = QlibDataBuilder._read_bin_file(
        raw / "features/jp_72030/factor.day.bin"
    )
    _, volumes = QlibDataBuilder._read_bin_file(
        raw / "features/jp_72030/volume.day.bin"
    )
    assert prices.tolist() == [100, 50, 45]
    assert factors.tolist() == [1, 1, 1]
    assert volumes.tolist() == [1000, 1000, 1000]
    assert prepare_jp_rd_provider(root, price_basis="raw") == raw
    assert prepare_jp_rd_provider(root) == adjusted


@pytest.mark.parametrize("damage", ["source", "features", "calendar", "benchmark"])
def test_incomplete_or_mismatched_research_cache_is_rejected(
    snapshot, tmp_path, damage
):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    provider = prepare_jp_rd_provider(root)
    path = {
        "source": "research_source.json",
        "features": "features/jp_216a0/open.day.bin",
        "calendar": "calendars/day.txt",
        "benchmark": "features/jp_topix/close.day.bin",
    }[damage]
    (provider / path).unlink()
    with pytest.raises(ValueError):
        prepare_jp_rd_provider(root)


def test_failed_build_never_publishes_partial_provider(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)

    def fail(builder):
        (builder.qlib_dir / "features").mkdir(parents=True)
        raise RuntimeError("test build failure")

    monkeypatch.setattr(QlibDataBuilder, "build_all", fail)
    with pytest.raises(RuntimeError, match="test build failure"):
        prepare_jp_rd_provider(root)
    cache = root / ".rd_cache" / QuantJPDataHub(root).data_dir.name
    assert not (cache / "qlib").exists()
    assert not list(cache.glob(".qlib-*"))


def test_missing_or_foreign_research_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="complete immutable publication"):
        prepare_jp_rd_provider(tmp_path / "missing")
    with pytest.raises(ValueError, match="escapes"):
        prepare_jp_rd_provider(tmp_path / "root", publication=tmp_path / "outside")


def test_research_provider_cannot_redirect_outside_cache(snapshot, tmp_path):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    cache = root / ".rd_cache" / QuantJPDataHub(root).data_dir.name
    cache.mkdir(parents=True)
    outside = tmp_path / "outside-provider"
    outside.mkdir()
    (cache / "qlib_v3").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes its cache"):
        prepare_jp_rd_provider(root)
    assert list(outside.iterdir()) == []
