"""Adapt immutable J-Quants publications to RD-Agent's common HDF contract."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from backend.services.engine.data_platform.quantjp_hub import (
    QuantJPDataHub,
    _resolve_quantjp_data_dir,
)
from backend.shared.stock_utils import StockCodeUtil


def prepare_jp_rd_data(
    data_dir: str | Path | None = None,
    *,
    debug: bool = False,
    publication: Path | None = None,
) -> Path:
    root = Path(data_dir or _resolve_quantjp_data_dir())
    publication = (publication or QuantJPDataHub(root).data_dir).resolve()
    if not publication.is_relative_to(root.resolve()):
        raise ValueError("JP research publication escapes its source root")
    if not (publication / "manifest.json").is_file():
        raise ValueError("JP research requires a complete immutable publication")
    hub = QuantJPDataHub(publication)
    days = hub._partition_dates("1_kline_data/daily_forward")
    if not days:
        raise ValueError("JP adjusted daily research data is unavailable")

    cache = root / ".rd_cache" / publication.name
    output = cache / ("daily_pv_debug.h5" if debug else "daily_pv_all.h5")
    if output.is_file():
        with pd.HDFStore(output, mode="r") as store:
            attrs = store.get_storer("data").attrs
            if (
                attrs.market == "JP"
                and attrs.data_version == publication.name
                and getattr(attrs, "contract_version", None) == 1
            ):
                return output

    symbols = None
    if debug:
        periods = hub.fetch_instrument_periods()
        symbols = sorted(periods["symbol"].unique())[:50]
    months: dict[str, list[str]] = defaultdict(list)
    for day in days:
        months[day[:6]].append(day)
    cache.mkdir(parents=True, exist_ok=True)
    staging = cache / (".daily-" + uuid.uuid4().hex + ".h5")
    try:
        rows = 0
        with pd.HDFStore(staging, mode="w") as store:
            for month in sorted(months):
                start = datetime.strptime(months[month][0], "%Y%m%d").date()
                end = datetime.strptime(months[month][-1], "%Y%m%d").date()
                adjusted = hub._normalize_kline(
                    hub._read("1_kline_data/daily_forward", start, end, symbols)
                )
                if adjusted.empty:
                    continue
                raw = hub._normalize_kline(
                    hub._read("1_kline_data/daily_unadjusted", start, end, symbols)
                )
                frame = adjusted.merge(
                    raw[["symbol", "trade_date", "close"]].rename(
                        columns={"close": "raw_close"}
                    ),
                    on=["symbol", "trade_date"],
                    how="left",
                    validate="one_to_one",
                    indicator=True,
                )
                factor = frame["price_factor"]
                suspended = frame["close"].isna() & frame["raw_close"].isna()
                aligned = suspended | (
                    frame["close"].gt(0)
                    & frame["raw_close"].gt(0)
                    & np.isclose(frame["close"], frame["raw_close"] * factor)
                )
                if not (
                    frame["_merge"].eq("both")
                    & np.isfinite(factor)
                    & factor.gt(0)
                    & aligned
                ).all():
                    raise ValueError("JP raw and adjusted research prices do not align")
                data = {
                    "$" + name: frame[name].to_numpy(dtype="float64")
                    for name in ("open", "high", "low", "close", "volume", "amount")
                }
                data["$factor"] = factor.to_numpy(dtype="float64")
                combined = pd.DataFrame(
                    data,
                    index=pd.MultiIndex.from_arrays(
                        [
                            pd.to_datetime(frame["trade_date"]).to_numpy(
                                dtype="datetime64[ns]"
                            ),
                            [
                                StockCodeUtil.to_qlib(s, market="JP")
                                for s in frame.symbol
                            ],
                        ],
                        names=["datetime", "instrument"],
                    ),
                ).sort_index()
                store.append(
                    "data", combined, format="table", min_itemsize={"instrument": 32}
                )
                rows += len(combined)
            if not rows:
                raise ValueError("JP research export contains no observations")
            attrs = store.get_storer("data").attrs
            attrs.market = "JP"
            attrs.data_version = publication.name
            attrs.contract_version = 1
        staging.replace(output)
        return output
    finally:
        staging.unlink(missing_ok=True)
