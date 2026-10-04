"""Publish price-only Alpha158 with the JP snapshot; call in a separate process.

Qlib has process-global configuration. This builder owns its isolated binary
cache and never initializes Qlib inside a running multi-market API process.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from backend.shared.stock_utils import StockCodeUtil
from .jp_file_lock import exclusive_file_lock
from .jquants_import import _copy_partitions
from .quantjp_hub import QuantJPDataHub
from .jp_publication import publication_path, publish_pointer


def build_jp_features_in_process(root: str | Path, *, timeout: int = 3600) -> dict:
    """Run Qlib in a child process, keeping API/worker global state untouched."""
    script = Path(__file__).resolve().parents[3] / "scripts/build_jp_features.py"
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return json.loads(result.stdout.splitlines()[-1])


def _copy_snapshot(source, target):
    # DirEntry carries type information without a stat call per partition on
    # Windows Docker mounts. Data are immutable; no timestamps need copying.
    target.mkdir()
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.name == "l1_factors":
                continue
            original, destination = Path(entry.path), target / entry.name
            if entry.is_symlink():
                raise ValueError("JP publication must not contain symbolic links")
            if entry.is_dir(follow_symlinks=False):
                _copy_snapshot(original, destination)
            elif original.suffix == ".json":
                shutil.copyfile(original, destination)
            else:
                try:
                    os.link(original, destination)
                except OSError:
                    shutil.copyfile(original, destination)


def _evaluate_cache(hub, cache, batch_size, workers, start, end, callback):
    import qlib

    if qlib.__version__ != "0.9.7":
        raise RuntimeError(
            "JP Alpha158 publication requires the verified Qlib 0.9.7 definition"
        )
    # Tens of thousands of small binary files belong on the container's native
    # filesystem; only the final parquet publication uses the data mount.
    with tempfile.TemporaryDirectory(prefix="quantmind-jp-qlib-") as local_cache:
        _evaluate_local_cache(
            hub, Path(local_cache), batch_size, workers, start, end, callback
        )


def _evaluate_local_cache(hub, cache, batch_size, workers, start, end, callback):
    import qlib
    from qlib.data import D
    from qlib.contrib.data.loader import Alpha158DL
    from backend.services.engine.qlib_data_builder import QlibDataBuilder

    QlibDataBuilder(hub, cache, market="JP").build_all()
    # Region is only a Qlib expression-engine setting; all dates and symbols
    # come from our JP provider. No Qlib exchange/account is used here.
    qlib.init(
        provider_uri=str(cache),
        region="us",
        kernels=workers,
        expression_cache=None,
        dataset_cache=None,
    )
    fields, names = Alpha158DL.get_feature_config()
    if len(names) != 158 or len(set(names)) != 158:
        raise RuntimeError("Unexpected Alpha158 feature definition")
    symbols = [
        line.split("\t")[0]
        for line in (cache / "instruments/all.txt").read_text().splitlines()
        if line
    ]
    base = ["open", "high", "low", "close", "volume", "amount"]
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        frame = D.features(
            batch,
            [*fields, *["$" + name for name in base]],
            start_time=str(start),
            end_time=str(end),
        )
        frame.columns = [*names, *base]
        frame = frame.reset_index()
        frame["symbol"] = frame["instrument"].map(
            lambda code: StockCodeUtil.to_suffix(code, market="JP")
        )
        frame["date"] = pd.to_datetime(frame["datetime"]).dt.normalize()
        frame = frame.drop(columns=["instrument", "datetime"])
        numeric = [*names, *base]
        frame[numeric] = (
            frame[numeric].replace([np.inf, -np.inf], np.nan).astype("float32")
        )
        callback(offset // batch_size, frame, names)


def build_jp_features(
    root: str | Path,
    *,
    batch_size=64,
    workers=2,
    start: date | None = None,
    end: date | None = None,
    progress=None,
    evaluator=None,
) -> dict:
    root = Path(root).resolve()
    if batch_size < 1 or workers < 1:
        raise ValueError("Feature batch size and worker count must be positive")
    with exclusive_file_lock(root / ".publish.lock"):
        source = publication_path(root, raw=True)
        if (
            not source.is_relative_to(root / "versions")
            or not (source / "manifest.json").is_file()
        ):
            raise ValueError("Import a complete immutable JP publication first")
        hub = QuantJPDataHub(source)
        dates = hub._partition_dates("1_kline_data/daily_unadjusted", start, end)
        if not dates:
            raise ValueError("No JP sessions in the requested feature window")
        if dates != hub._partition_dates("1_kline_data/daily_unadjusted"):
            raise ValueError(
                "A complete JP research publication requires every raw session; "
                "partial feature windows cannot replace current.json"
            )
        first, last = (
            date.fromisoformat(f"{v[:4]}-{v[4:6]}-{v[6:]}")
            for v in (dates[0], dates[-1])
        )
        version = "features-" + uuid.uuid4().hex
        stage = root / "versions" / (".staging-" + version)
        if progress:
            progress("copy_snapshot", source.name)
        _copy_snapshot(source, stage)
        work = stage / ".feature_work"
        work.mkdir()
        cache = work / "qlib"
        shards = []
        feature_names = None

        def save_batch(index, frame, names):
            nonlocal feature_names
            if feature_names is not None and names != feature_names:
                raise ValueError("JP feature schema changed within a build")
            feature_names = list(names)
            if len(feature_names) != 158 or len(set(feature_names)) != 158:
                raise ValueError(
                    "JP feature schema must contain the 158 Alpha158 fields"
                )
            if frame.duplicated(["symbol", "date"]).any():
                raise ValueError("Duplicate JP feature symbol/date")
            path = work / f"batch-{index:05d}.parquet"
            frame.to_parquet(path, index=False)
            shards.append(str(path))
            if progress:
                progress("batch", index + 1)

        if progress:
            progress("evaluate_features", str(first) + ".." + str(last))
        (evaluator or _evaluate_cache)(
            hub, cache, batch_size, workers, first, last, save_batch
        )
        if not shards:
            raise ValueError("JP feature build produced no batches")
        raw_files = [
            str(source / "1_kline_data/daily_unadjusted" / f"dt={day}/data.parquet")
            for day in dates
        ]
        # Join against observed raw rows to exclude artificial suspension/IPO
        # rows from the stock universe. Missing execution prices remain null.
        with duckdb.connect(
            config={"memory_limit": "6GB", "threads": str(workers)}
        ) as conn:
            conn.execute("SET temp_directory = ?", [str(work / "spill")])
            conn.read_parquet(raw_files, hive_partitioning=True).create_view("raw")
            conn.read_parquet(shards).create_view("features")
            quoted = ",".join(
                'f."' + name.replace('"', '""') + '"'
                for name in [
                    *feature_names,
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                ]
            )
            query = (
                f"SELECT r.symbol, CAST(r.time AS DATE) AS date, r.dt, {quoted} "
                "FROM raw r LEFT JOIN features f ON r.symbol=f.symbol AND CAST(r.time AS DATE)=f.date"
            )
            expected = conn.execute("SELECT count(*) FROM raw").fetchone()[0]
            count = conn.execute(f"SELECT count(*) FROM ({query})").fetchone()[0]
            if count != expected:
                raise ValueError(
                    "JP feature row cardinality does not match source prices"
                )
            missing = conn.execute(
                "SELECT count(*) FROM raw r LEFT JOIN features f ON r.symbol=f.symbol AND CAST(r.time AS DATE)=f.date WHERE f.symbol IS NULL"
            ).fetchone()[0]
            if missing:
                raise ValueError(f"JP features missing {missing} observed price rows")
            partitions = _copy_partitions(
                conn, query, stage / "6_ml_datasets/l1_factors"
            )
        # Only this run's controlled staging work directory is removed.
        if not work.resolve().is_relative_to(stage.resolve()):
            raise ValueError("JP feature cleanup path escaped its staging directory")
        shutil.rmtree(work)
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(version=version, parent_version=source.name)
        manifest["datasets"]["l1_factors"] = {
            "partitions": partitions,
            "rows": count,
            "min_date": str(first),
            "max_date": str(last),
            "feature_names": feature_names,
            "definition": "Qlib 0.9.7 Alpha158; price/volume only",
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        stage.rename(root / "versions" / version)
        publish_pointer(root, version)
        return manifest["datasets"]["l1_factors"] | {"version": version}
