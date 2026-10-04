"""Source of a prediction, independent of the model's training publication."""

from datetime import date
import json
import math
from pathlib import Path


def prediction_source(value):
    """Validate explicit input provenance; absence is a legacy capability gap."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        return None
    value = value.get("data_provenance", value)
    if not isinstance(value, dict) or value.get("market") != "JP":
        return None
    version = str(value.get("data_version") or "")
    run_id = str(value.get("run_id") or "")
    if (
        not version
        or version in {".", ".."}
        or "/" in version
        or "\\" in version
        or Path(version).name != version
        or not run_id
    ):
        raise ValueError("Prediction source lacks its immutable publication or run")
    first = date.fromisoformat(str(value.get("data_trade_date") or ""))
    raw_prediction = value.get("prediction_trade_date")
    prediction = date.fromisoformat(str(raw_prediction)) if raw_prediction else None
    if (prediction is None and not run_id.startswith("training:")) or (
        prediction and prediction <= first
    ):
        raise ValueError("Prediction source dates are inconsistent")
    return {
        "market": "JP",
        "data_version": version,
        "data_trade_date": first.isoformat(),
        "prediction_trade_date": prediction.isoformat() if prediction else None,
        "run_id": run_id,
    }


def read_pred_sources(parquet_file, day, symbol=None):
    """Read exact input-day rows with their source from the authoritative file.

    The score and source are selected together, including legacy rows with no
    provenance. No latest-publication or training-version inference is allowed.
    """
    import duckdb
    from backend.shared.stock_utils import StockCodeUtil

    path = Path(parquet_file)
    if not path.is_file():
        return []
    with duckdb.connect() as db:
        cols = {
            row[0]
            for row in db.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
            ).fetchall()
        }
        if not {"symbol", "trade_date", "pred"} <= cols:
            return []
        source_sql = '"data_provenance"' if "data_provenance" in cols else "NULL"
        records = db.execute(
            f"SELECT symbol, pred, {source_sql} FROM read_parquet(?) WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)",
            [str(path), str(day)],
        ).fetchall()
    rows = []
    seen = set()
    for code, score, raw in records:
        if score is None or not math.isfinite(float(score)):
            continue
        prefix = StockCodeUtil.to_prefix(str(code), market="JP")
        if symbol and prefix != StockCodeUtil.to_prefix(symbol, market="JP"):
            continue
        if prefix in seen:
            raise ValueError("Prediction source rows require unique symbols")
        seen.add(prefix)
        source = prediction_source(raw)
        if source and source["data_trade_date"] != str(day):
            raise ValueError("Prediction row differs from its recorded input day")
        rows.append(
            {"symbol": prefix, "score": float(score), "data_provenance": source}
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return [{**row, "rank": rank} for rank, row in enumerate(rows, 1)]


def stamp_training_predictions(frame, publication_data_dir, run_id):
    """Record actual training inputs while emitting a new prediction artifact."""
    from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
    import pandas as pd

    hub = QuantJPDataHub(publication_data_dir)
    if Path(publication_data_dir).resolve() != hub.data_dir.resolve():
        raise ValueError("Training prediction requires an already pinned publication")
    if not run_id or str(run_id) == "unknown":
        raise ValueError("Training prediction requires its actual run ID")
    sessions = sorted(pd.to_datetime(hub.fetch_calendar().trade_date).dt.date)
    next_days = {
        day: sessions[i + 1].isoformat() if i + 1 < len(sessions) else None
        for i, day in enumerate(sessions)
    }
    result = frame.copy()

    def source(day):
        if day not in next_days:
            raise ValueError(
                "Training prediction input is outside its publication calendar"
            )
        return json.dumps(
            prediction_source(
                {
                    "market": "JP",
                    "data_version": hub.data_dir.name,
                    "data_trade_date": day.isoformat(),
                    "prediction_trade_date": next_days[day],
                    "run_id": f"training:{run_id}",
                }
            ),
            sort_keys=True,
        )

    days = pd.to_datetime(result.trade_date).dt.date
    recorded = {day: source(day) for day in days.unique()}
    result["data_provenance"] = days.map(recorded)
    return result
