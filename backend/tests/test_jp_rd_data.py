"""JP historical prices conform to the existing RD-Agent research data contract."""

import duckdb
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.rd_agent.data_pipeline.jp_data import prepare_jp_rd_data
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.mark.parametrize("debug", [False, True])
def test_rd_export_preserves_jp_adjustments_and_delisted_stocks(
    snapshot, tmp_path, debug
):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    output = prepare_jp_rd_data(root, debug=debug)
    data = pd.read_hdf(output, key="data")
    assert data.index.names == ["datetime", "instrument"]
    assert set(data.index.get_level_values("instrument")) == {
        "jp_72030",
        "jp_216a0",
        "jp_13370",
    }
    assert len(data) == 7
    assert data.index.get_level_values("datetime").min() == pd.Timestamp("2026-09-28")
    assert data.index.get_level_values("datetime").max() == pd.Timestamp("2026-09-30")
    toyota = data.xs("jp_72030", level="instrument")
    assert toyota["$close"].tolist() == pytest.approx([45, 45, 45])
    assert toyota["$factor"].tolist() == pytest.approx([0.45, 0.9, 1])
    assert toyota["$volume"].tolist() == [2000, 1000, 1000]
    assert toyota["$amount"].tolist() == [100000, 50000, 45000]
    with pd.HDFStore(output, mode="r") as store:
        assert store.get_storer("data").attrs.market == "JP"
    before = output.stat().st_mtime_ns
    assert prepare_jp_rd_data(root, debug=debug) == output
    assert output.stat().st_mtime_ns == before


def test_rd_export_pins_publication_and_rejects_missing_or_foreign_data(
    snapshot, tmp_path
):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    publication = QuantJPDataHub(root).data_dir
    full = prepare_jp_rd_data(root, publication=publication)
    # A moving current pointer must not change the debug data of this task.
    (root / "current.json").unlink()
    debug = prepare_jp_rd_data(root, debug=True, publication=publication)
    assert debug.parent == full.parent
    with pytest.raises(ValueError, match="complete immutable publication"):
        prepare_jp_rd_data(tmp_path / "missing")
    with pytest.raises(ValueError, match="escapes"):
        prepare_jp_rd_data(root, publication=tmp_path / "foreign")


def test_rd_export_cache_changes_with_source_publication(snapshot, tmp_path):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    old_publication = QuantJPDataHub(root).data_dir
    old_output = prepare_jp_rd_data(root)
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.daily_prices SET Vo=2000 WHERE Code='72030'")
    import_jquants_snapshot(snapshot, root)
    new_output = prepare_jp_rd_data(root)
    assert new_output.parent != old_output.parent
    old_data = pd.read_hdf(old_output, key="data").xs("jp_72030", level="instrument")
    new_data = pd.read_hdf(new_output, key="data").xs("jp_72030", level="instrument")
    assert new_data["$volume"].tolist() == (old_data["$volume"] * 2).tolist()
    assert prepare_jp_rd_data(root, publication=old_publication) == old_output


def test_rd_export_does_not_publish_misaligned_prices(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    read = QuantJPDataHub._read

    def missing_raw(self, category, *args, **kwargs):
        frame = read(self, category, *args, **kwargs)
        if category == "1_kline_data/daily_unadjusted":
            frame = frame.iloc[1:]
        return frame

    monkeypatch.setattr(QuantJPDataHub, "_read", missing_raw)
    with pytest.raises(ValueError, match="do not align"):
        prepare_jp_rd_data(root)
    assert not list((root / ".rd_cache").rglob("*.h5"))


def test_rd_export_preserves_suspension_without_inventing_prices(snapshot, tmp_path):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,Vo=NULL,Va=NULL "
            "WHERE Code='72030' AND Date='2026-09-30'"
        )
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    data = pd.read_hdf(prepare_jp_rd_data(root), key="data")
    suspended = data.loc[(pd.Timestamp("2026-09-30"), "jp_72030"), :]
    assert suspended.drop("$factor").isna().all()
    assert suspended["$factor"] == 1
