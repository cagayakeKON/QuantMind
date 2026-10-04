"""Read exact JP test predictions without resolving another market's default."""

import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from backend.shared.model_registry import model_registry_service
from backend.shared.stock_utils import StockCodeUtil
from .rules import RuleDataMissing


async def resolve_model(tenant_id, user_id, model_id, *, strategy_id=None):
    resolved = await model_registry_service.resolve_effective_model(
        tenant_id=tenant_id,
        user_id=user_id,
        model_id=model_id,
        strategy_id=strategy_id,
        market="JP",
    )
    if resolved.fallback_used or (model_id and resolved.effective_model_id != model_id):
        raise LookupError("Requested JP model is unavailable")
    if not resolved.effective_model_id or not resolved.storage_path:
        raise ValueError("Select a registered JP model or configure its market default")
    directory = Path(resolved.storage_path)
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if str((meta.get("context") or {}).get("market") or "").upper() != "JP":
        raise ValueError("Select a registered Japanese-market model")
    # The standard trainer records publication provenance under factor_coverage.
    coverage_version = (meta.get("factor_coverage") or {}).get("jp_data_version")
    declared_version = meta.get("jp_data_version")
    if declared_version and coverage_version and declared_version != coverage_version:
        raise RuleDataMissing("JP model publication metadata is inconsistent")
    version = declared_version or coverage_version
    if (
        meta.get("data_source") != "quantdb_factors"
        or meta.get("factor_source") != "l1_factors"
        or not version
    ):
        raise RuleDataMissing("JP model requires versioned l1_factors metadata")
    return directory, {
        **meta,
        "jp_data_version": version,
        **(
            {
                "effective_model_id": resolved.effective_model_id,
                "model_source": resolved.model_source,
            }
            if not model_id
            else {}
        ),
    }


def prediction_path(directory: Path) -> Path:
    for path in (directory / "pred.parquet", directory / "pred/pred.parquet"):
        if path.is_file():
            return path
    raise RuleDataMissing(
        "JP predictions are unavailable; run historical inference first"
    )


def replay_prediction_snapshot(directory: Path, expected_digest=None) -> Path:
    """Capture one inference file into a content-addressed replay artifact.

    A legacy session may be captured only while its saved whole-file digest
    still matches. Never reconstruct old predictions from a changed source.
    """
    artifacts = directory / "replay_predictions"
    if expected_digest is not None:
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)
        ):
            raise RuleDataMissing("Invalid JP replay prediction snapshot digest")
        pinned = artifacts / f"{expected_digest}.parquet"
        if pinned.is_file():
            return pinned
    source_path = prediction_path(directory)
    artifacts.mkdir(exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".capturing-", dir=artifacts)
    try:
        digest = hashlib.sha256()
        with os.fdopen(handle, "wb") as target, source_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
        value = digest.hexdigest()
        if expected_digest is not None and value != expected_digest:
            raise RuleDataMissing(
                "JP replay predictions changed before their saved snapshot was captured"
            )
        pinned = artifacts / f"{value}.parquet"
        # Link publishes a complete artifact without replacing an existing one;
        # a concurrent session with this digest reuses exactly the same bytes.
        try:
            os.link(temporary, pinned)
        except FileExistsError:
            pass
        return pinned
    finally:
        Path(temporary).unlink(missing_ok=True)


def labels_available_on(meta, calendar, signal_day):
    from backend.services.engine.data_platform.jp_labels import last_label_session

    cutoff = max(str(meta.get("train_end") or ""), str(meta.get("val_end") or ""))
    if not cutoff:
        raise RuleDataMissing("JP model requires its training/validation cutoff")
    known = last_label_session(
        cutoff, calendar.sessions, int(meta.get("target_horizon_days") or 1)
    )
    if known is None or signal_day < known.date():
        raise ValueError(
            "JP signal precedes the availability of training/validation labels"
        )
    return known.date()


def read_test_scores(path: Path, first: date, last: date) -> tuple[dict, str]:
    # Inference replaces the source atomically. Read one open-file snapshot.
    with tempfile.TemporaryDirectory(prefix="quantmind-jp-pred-") as temp:
        frozen = Path(temp) / "pred.parquet"
        with path.open("rb") as source, frozen.open("wb") as target:
            shutil.copyfileobj(source, target)
        digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
        with duckdb.connect() as conn:
            columns = {
                item[0]
                for item in conn.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(frozen)]
                ).fetchall()
            }
            if not {"symbol", "trade_date", "pred", "split"} <= columns:
                raise RuleDataMissing("JP requires dated, test-split model predictions")
            frame = conn.execute(
                "SELECT symbol, trade_date, pred FROM read_parquet(?) "
                "WHERE CAST(trade_date AS DATE) BETWEEN ? AND ? AND split = 'test'",
                [str(frozen), first, last],
            ).fetchdf()
    result = {}
    for row in frame.itertuples():
        if not StockCodeUtil.is_jp_symbol(row.symbol) or not math.isfinite(
            float(row.pred)
        ):
            raise ValueError(
                "JP prediction contains invalid securities or non-finite scores"
            )
        day = pd.Timestamp(row.trade_date).date()
        result.setdefault(day, []).append(
            {"symbol": StockCodeUtil.to_prefix(row.symbol), "score": float(row.pred)}
        )
    for rows in result.values():
        if len({row["symbol"] for row in rows}) != len(rows):
            raise ValueError("Duplicate JP date/security prediction")
    return result, digest
