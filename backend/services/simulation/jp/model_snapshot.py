"""Model-bound, dated native features for the common individual attribution API."""

import pandas as pd
import pyarrow.parquet as pq

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.fundamental_aligner import FundamentalAligner
from backend.shared.stock_utils import StockCodeUtil


def read_model_snapshot(metadata, symbol, asof, feature_columns):
    if str((metadata.get("context") or {}).get("market") or "").upper() != "JP":
        raise ValueError("Individual JP attribution requires a JP model")
    declared = metadata.get("jp_data_version")
    trained = (metadata.get("factor_coverage") or {}).get("jp_data_version")
    if declared and trained and declared != trained:
        raise ValueError("JP model publication metadata is inconsistent")
    version = declared or trained
    if not version:
        raise ValueError("JP attribution requires the model's immutable publication")
    hub = LOCAL_MARKET_PROVIDERS["JP"].open(version)
    relative = "6_ml_datasets/l1_factors"
    dates = hub._partition_dates(relative, end=asof)
    if not dates:
        return None
    schema = set().union(
        *(
            set(pq.read_schema(path).names)
            for path in (hub.data_dir / relative / f"dt={dates[-1]}").glob("*.parquet")
        )
    )
    available = [column for column in feature_columns if column in schema]
    if not available:
        return None
    frame = hub.fetch_latest_rows(
        "qjp_l1_factors",
        [StockCodeUtil.to_suffix(symbol, market="JP")],
        dt=int(asof.strftime("%Y%m%d")),
        lookback=FundamentalAligner.LOOKBACK_DAYS,
        columns=available,
    )
    if frame.empty:
        return None
    if frame.dt.gt(int(asof.strftime("%Y%m%d"))).any():
        raise ValueError("JP attribution exceeds its requested date")
    row = frame.sort_values("dt").iloc[-1]
    return pd.Series({column: row[column] for column in available}), available
