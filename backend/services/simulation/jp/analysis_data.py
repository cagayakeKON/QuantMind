"""Map recorded JP cash results to the existing analysis data contracts."""

import numpy as np
import pandas as pd

from backend.shared.stock_utils import StockCodeUtil


def read_benchmark_prices(result, benchmark_id, start_date, end_date):
    """Expose saved TOPIX levels to the shared price-return calculation.

    benchmark_value is initial capital times the TOPIX price-index ratio,
    rather than a raw closing price. Its constant scale preserves pct_change.
    """
    config = result.config or {}
    if (result.market or config.get("market")) != "JP":
        raise ValueError("TOPIX analysis requires a Japanese-market backtest")
    if str(benchmark_id).upper() != "TOPIX" or result.benchmark_symbol != "TOPIX":
        raise ValueError("Requested benchmark does not match the recorded JP result")
    versions = {
        value
        for value in (
            config.get("jp_data_version"),
            config.get("data_version"),
            result.data_version,
        )
        if value
    }
    if not versions:
        raise ValueError("Recorded JP benchmark data version is unavailable")
    if len(versions) != 1:
        raise ValueError("Recorded JP benchmark data versions do not match")
    frame = pd.DataFrame(result.equity_curve or [])
    if frame.empty or not {"date", "benchmark_value"}.issubset(frame.columns):
        raise ValueError("Recorded JP benchmark prices are unavailable")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    values = pd.to_numeric(frame["benchmark_value"], errors="coerce")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError("Recorded JP benchmark dates are invalid")
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Recorded JP benchmark prices are invalid")
    frame["$close"] = values
    frame = frame.sort_values("date")
    frame = frame[frame.date.between(pd.Timestamp(start_date), pd.Timestamp(end_date))]
    frame["instrument"] = "jp_topix"
    return frame.set_index(["instrument", "date"])[["$close"]].rename_axis(
        index={"date": "datetime"}
    )


def public_trades(fills):
    """Add the public action field to copies, preserving ledger quantities/times."""
    return [
        {
            **fill,
            "symbol": StockCodeUtil.to_prefix(fill["symbol"], market="JP"),
            "action": fill["side"].lower(),
        }
        for fill in fills
    ]
