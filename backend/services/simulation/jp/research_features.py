"""Dated native fields for the existing research projection API."""

from datetime import date
import math

import pandas as pd

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


def model_publication(metadata):
    declared = metadata.get("jp_data_version")
    trained = (metadata.get("factor_coverage") or {}).get("jp_data_version")
    if declared and trained and declared != trained:
        raise ValueError("JP model publication metadata is inconsistent")
    version = declared or trained
    if not version:
        raise ValueError("JP research requires the model's immutable publication")
    return version


def dated_metadata(hub, day):
    master = hub.fetch_stock_list(as_of=day)
    return {
        StockCodeUtil.to_prefix(row["symbol"], market="JP"): {
            "stock_name": row.get("stock_name")
            if isinstance(row.get("stock_name"), str)
            else "",
            "industry": row.get("industry_name")
            if isinstance(row.get("industry_name"), str)
            else "",
        }
        for row in master.to_dict("records")
    }


def read_projection(symbols, wanted, trade_date=None, data_version=None):
    # Freeze one registered publication for this request, including future labels.
    hub = LOCAL_MARKET_PROVIDERS["JP"].open(data_version)
    day = date.fromisoformat(trade_date[:10]) if trade_date else None
    if day is None:
        days = hub._partition_dates("1_kline_data/daily_unadjusted", end=date.today())
        if not days:
            return {}
        day = pd.Timestamp(days[-1]).date()
    raw = hub.fetch_daily_kline_batch(symbols, day, day, adjust="none")
    valuation = hub.fetch_latest_rows(
        "qjp_valuation", symbols, dt=int(day.strftime("%Y%m%d"))
    )
    factors = hub.fetch_l1_factors(start=day, end=day)
    frames = [
        ("daily_unadjusted", raw),
        ("valuation", valuation),
        ("l1_factors", factors),
    ]
    aliases = {"close": "closePrice", "pe_ttm": "pe", "total_mv": "totalMv"}
    scales = {"totalMv": 1e-8, "amount": 1e-8}
    result = {}
    for symbol in symbols:
        values, sources = {}, []
        for source, frame in frames:
            if frame.empty:
                continue
            selected = frame.loc[frame.symbol.eq(symbol)]
            if selected.empty:
                continue
            sources.append(source)
            for column, value in selected.iloc[-1].items():
                parts = column.split("_")
                name = aliases.get(column) or parts[0] + "".join(
                    p.title() for p in parts[1:]
                )
                if name not in wanted or name in values:
                    continue
                if (
                    isinstance(value, bool)
                    or not pd.api.types.is_number(value)
                    or pd.isna(value)
                ):
                    continue
                value = float(value)
                if not math.isfinite(value):
                    continue
                values[name] = value * scales.get(name, 1)
        result[symbol] = {
            "symbol": symbol,
            "tradeDate": day.isoformat(),
            "sources": sources,
            "values": values,
        }

    # Returns are based on split-adjusted research prices, never past momentum
    # substituted for a requested future-return label. Missing horizons stay absent.
    return_fields = {f"return{n}d": n for n in (1, 3, 5, 10, 20, 60)}
    if "latestChange" in wanted or wanted.intersection(return_fields):
        sessions = pd.to_datetime(hub.fetch_calendar().trade_date).dt.date.tolist()
        if day not in sessions:
            return result
        pos = sessions.index(day)
        horizon = max(
            (return_fields[name] for name in wanted.intersection(return_fields)),
            default=0,
        )
        history = hub.fetch_daily_kline_batch(
            symbols,
            sessions[max(0, pos - 1)],
            sessions[min(len(sessions) - 1, pos + horizon)],
            adjust="qfq",
        )
        if not history.empty:
            for symbol in symbols:
                bars = history.loc[history.symbol.eq(symbol)].sort_values("trade_date")
                prices = pd.Series(
                    pd.to_numeric(bars.close, errors="coerce").to_numpy(),
                    index=pd.to_datetime(bars.trade_date).dt.date,
                )
                closes = prices.reindex(sessions).tolist()
                base = closes[pos]
                if not base or not pd.notna(base):
                    continue
                target = result[symbol]["values"]
                if "latestChange" in wanted and pos > 0 and closes[pos - 1] > 0:
                    target["latestChange"] = (base / closes[pos - 1] - 1) * 100
                for name in wanted.intersection(return_fields):
                    future = pos + return_fields[name]
                    if future < len(closes) and pd.notna(closes[future]):
                        target[name] = (closes[future] / base - 1) * 100
    return result
