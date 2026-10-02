from datetime import date
import json
import importlib
from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.jp_labels import (
    forward_open_labels,
    last_label_session,
)
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


def fake_evaluator(hub, cache, batch_size, workers, start, end, save):
    frame = hub.fetch_daily_kline_batch(["JP72030", "JP216A0", "JP13370"], start, end)
    frame = frame.rename(columns={"trade_date": "date"})
    names = [f"feature_{i}" for i in range(158)]
    frame = pd.concat(
        [frame, pd.DataFrame(1.0, index=frame.index, columns=names)], axis=1
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


def test_feature_publication_preserves_pinned_prices_and_old_manifest(
    snapshot, tmp_path
):
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    old = QuantJPDataHub(root).data_dir
    original_manifest = (old / "manifest.json").read_bytes()
    result = build_jp_features(root, evaluator=fake_evaluator)
    assert result["rows"] == 7 and result["partitions"] == 3
    assert (old / "manifest.json").read_bytes() == original_manifest
    assert not (old / "6_ml_datasets/l1_factors").exists()
    current = QuantJPDataHub(root).data_dir
    assert current != old
    reader = QuantDBFactorReader(root, market="JP")
    assert reader.describe("l1_factors").ready
    assert (
        len(
            reader.read_range(
                "l1_factors",
                features=["feature_1"],
                start=date(2026, 9, 28),
                end=date(2026, 9, 30),
            )
        )
        == 7
    )
    assert (
        json.loads((current / "manifest.json").read_text())["parent_version"]
        == old.name
    )


def test_feature_failure_does_not_replace_current_snapshot(snapshot, tmp_path):
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    before = (root / "current.json").read_bytes()

    def empty(*args):
        pass

    with pytest.raises(ValueError, match="no batches"):
        build_jp_features(root, evaluator=empty)
    assert (root / "current.json").read_bytes() == before


def test_open_labels_use_cash_sessions_and_do_not_skip_suspensions():
    days = [date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    frame = pd.DataFrame(
        {
            "symbol": ["JP72030"] * 3,
            "trade_date": [days[0], days[2], days[3]],
            "open": [100, 120, 150],
            "volume": [1000] * 3,
        }
    )
    labels = forward_open_labels(frame, days, 1)
    assert pd.isna(
        labels.iloc[0]
    )  # Monday opening is absent; cannot substitute Tuesday.
    assert labels.isna().all()
    complete = pd.DataFrame(
        {
            "symbol": ["JP72030"] * 4,
            "trade_date": days,
            "open": [100, 110, 132, 145.2],
            "close": [500] * 4,
            "volume": [1000] * 4,
        }
    )
    result = forward_open_labels(complete, days, 1)
    assert result.iloc[:2].tolist() == pytest.approx([0.2, 0.1])
    complete.loc[1, "volume"] = 0
    assert pd.isna(forward_open_labels(complete, days, 1).iloc[0])


def test_labels_do_not_mix_symbols_or_allow_duplicate_cash_sessions():
    days = pd.date_range("2026-09-01", periods=4)
    frame = pd.DataFrame(
        {
            "symbol": ["JP72030"] * 4 + ["JP216A0"] * 4,
            "trade_date": list(days) * 2,
            "open": [100, 110, 121, 133.1, 200, 180, 162, 145.8],
            "volume": [1000] * 8,
        }
    )
    labels = forward_open_labels(frame, days, 1)
    assert labels.iloc[0] == pytest.approx(0.1)
    assert labels.iloc[4] == pytest.approx(-0.1)
    with pytest.raises(ValueError, match="Duplicate"):
        forward_open_labels(pd.concat([frame, frame.iloc[:1]]), days, 1)


def test_long_label_horizon_reads_exact_exit_session_beyond_calendar_padding():
    days = pd.bdate_range("2026-01-01", periods=80)
    needed = last_label_session(days[0], days, 30)
    assert needed == days[31]
    assert needed > days[0] + pd.Timedelta(days=33)
    assert last_label_session(days[-1], days, 30) is None


@pytest.mark.parametrize("mode", ["return", "classification"])
def test_actual_training_loader_uses_jp_opens_and_canonical_pool(
    snapshot, tmp_path, monkeypatch, mode
):
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)

    def different_opens(hub, cache, batch_size, workers, start, end, save):
        def change(index, frame, names):
            final = pd.to_datetime(frame["date"]).eq(pd.Timestamp("2026-09-30"))
            frame.loc[final & frame["symbol"].eq("72030.JP"), "open"] = 54
            frame.loc[final & frame["symbol"].eq("216A0.JP"), "open"] = 36
            save(index, frame, names)

        fake_evaluator(hub, cache, batch_size, workers, start, end, change)

    build_jp_features(root, evaluator=different_opens)
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "docker/training")
    )
    loader = importlib.import_module("data.loading")
    args = {
        "train_start": "2026-09-28",
        "train_end": "2026-09-30",
        "features": ["feature_1"],
        "local_dir": str(root),
        "quantdb_dir": str(root),
        "market": "JP",
        "factor_source": "l1_factors",
        "target_mode": mode,
    }
    result, names = loader.load_data(**args, pool_symbols=["7203.T", "JP216A0"])
    assert names == ["feature_1"]
    assert set(result.symbol) == {"JP72030", "JP216A0"}
    assert result.trade_date.eq(pd.Timestamp("2026-09-28")).all()
    labels = result.set_index("symbol").label
    assert labels["JP72030"] == (1 if mode == "classification" else 0.5)
    assert labels["JP216A0"] == 0
    with pytest.raises(ValueError, match="pool has no observations"):
        loader.load_data(**args, pool_symbols=["JP99990"])


def test_jp_orchestrator_pins_immutable_publication(snapshot, tmp_path, monkeypatch):
    from backend.services.engine.training import local_docker_orchestrator as module
    from backend.shared.training.request import ContextRequest

    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    report = build_jp_features(root, evaluator=fake_evaluator)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    orchestrator = module.LocalDockerOrchestrator.__new__(
        module.LocalDockerOrchestrator
    )
    orchestrator.api_base = "http://quantmind:8000"
    orchestrator.internal_secret = "test"
    payload = {
        "context": ContextRequest(market="JP").cleaned(),
        "features": ["feature_1"],
        "factor_source": "l1_factors",
        "train_start": "2026-09-28",
        "train_end": "2026-09-30",
    }
    config = orchestrator._build_config_yaml("jp-pin-test", payload)
    pinned = config["data"]["quantdb_dir"]
    assert pinned.endswith(report["version"])
    assert config["data"]["factor_coverage"]["jp_data_version"] == report["version"]
    # Publishing a different current version cannot redirect this task.
    build_jp_features(root, evaluator=fake_evaluator)
    assert config["data"]["quantdb_dir"] == pinned
    assert QuantJPDataHub(root).data_dir.name != report["version"]


@pytest.mark.parametrize("feature_failure", [False, True])
def test_saved_sync_updates_features_before_cache_and_reports_failure(
    monkeypatch, tmp_path, feature_failure
):
    from backend.scripts import quantjp_daily_sync
    from backend.services.engine.data_platform import jp_features
    from backend.services.engine import qlib_data_builder
    from backend.services.engine.tasks.market_sync_scheduler import run_market_sync

    calls = []
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(tmp_path))

    def download(**kwargs):
        calls.append("prices")
        return {"downloaded_sessions": 1}

    def features(root, **kwargs):
        calls.append("features")
        assert Path(root) == tmp_path and kwargs["timeout"] > 0
        if feature_failure:
            raise TimeoutError("feature calculation timed out")
        return {"version": "features-test", "rows": 7}

    def cache(**kwargs):
        calls.append("cache")
        assert kwargs["market"] == "JP"
        return "/tmp/jp-cache"

    monkeypatch.setattr(quantjp_daily_sync, "run", download)
    monkeypatch.setattr(jp_features, "build_jp_features_in_process", features)
    monkeypatch.setattr(qlib_data_builder, "ensure_qlib_cache", cache)
    report = run_market_sync("JP", {"with_qlib": True})
    assert report["qlib"]["status"] == ("error" if feature_failure else "ok")
    assert calls == (
        ["prices", "features"] if feature_failure else ["prices", "features", "cache"]
    )
