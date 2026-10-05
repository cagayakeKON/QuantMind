"""Exact inference inputs for the common individual attribution API."""

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.services.engine.inference.prediction_provenance import prediction_source
from backend.shared.stock_utils import StockCodeUtil


def read_model_snapshot(metadata, symbol, asof, feature_columns, *, provenance=None):
    from backend.shared.model_metadata import declared_model_market

    if declared_model_market(metadata) != "JP":
        raise ValueError("Individual JP attribution requires a JP model")
    source = prediction_source(provenance)
    if not source:
        raise ValueError("JP attribution requires the selected prediction's source")
    if source["data_trade_date"] != asof.isoformat():
        raise ValueError("JP attribution differs from the prediction input date")
    hub = LOCAL_MARKET_PROVIDERS["JP"].open(source["data_version"])
    reader = QuantDBFactorReader(hub.data_dir, market="JP")
    dataset = str(metadata.get("factor_source") or "l1_factors")
    mappings = metadata.get("factor_field_sources") or {}
    columns = set(reader.describe(dataset).columns)
    available = [
        column for column in feature_columns if mappings.get(column, column) in columns
    ]
    if available != list(feature_columns):
        raise ValueError("JP attribution is missing an inference feature")
    if asof.isoformat() not in reader.available_dates(dataset):
        return None
    frame = reader.read_day(
        dataset,
        features=available,
        feature_sources=mappings,
        trade_date=asof,
    )
    prep = metadata.get("preprocessing")
    if isinstance(prep, dict) and prep.get("enabled"):
        from backend.services.engine.inference.templates.inference_parquet import (
            filter_untradable_rows,
            preprocess,
        )

        # Single-stock/pool inference filters output after full-day preprocessing.
        # Normalize that same full tradable cross-section before selecting a row.
        frame = filter_untradable_rows(frame)
        x, symbols = preprocess(frame.copy(), metadata)
        frame = frame.loc[:, ["symbol", "trade_date"]].copy()
        frame[feature_columns] = x.to_numpy()
        if frame.symbol.tolist() != symbols:
            raise ValueError("JP preprocessing reordered attribution symbols")
    frame = frame.loc[frame.symbol.eq(StockCodeUtil.to_prefix(symbol, market="JP"))]
    if frame.empty:
        return None
    if len(frame) != 1 or not frame.trade_date.dt.date.eq(asof).all():
        raise ValueError("JP attribution requires one exact input-day feature row")
    # The shared reader applies inference mappings; retain the model's order.
    return frame.iloc[0].loc[available], available
