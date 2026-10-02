"""JP labels use exact cash sessions and tradable adjusted opens, never row shifts."""

import numpy as np
import pandas as pd


def last_label_session(signal_end, sessions, horizon: int, lag: int = 1):
    """Last needed exit date; None when the supplied calendar ends too early."""
    days = pd.DatetimeIndex(pd.to_datetime(sorted(set(sessions)))).normalize()
    position = days.searchsorted(pd.Timestamp(signal_end), side="right") - 1
    exit_position = position + max(1, int(horizon)) + lag
    return days[exit_position] if position >= 0 and exit_position < len(days) else None


def label_formula(horizon: int, mode: str = "return") -> str:
    raw = f"adjusted_open(T+{1 + max(1, int(horizon))}) / adjusted_open(T+1) - 1; JP cash sessions; price-only"
    return raw + (
        "; binary(return>0)"
        if str(mode).lower() == "classification"
        else "; daily cross-sectional rank(pct=True)-0.5"
    )


def forward_open_labels(frame: pd.DataFrame, sessions, horizon: int, lag: int = 1):
    if horizon < 1 or lag < 1:
        raise ValueError("JP labels require positive holding horizon and execution lag")
    days = pd.DatetimeIndex(pd.to_datetime(sorted(set(sessions)))).normalize()
    dates = pd.DatetimeIndex(pd.to_datetime(frame["trade_date"])).normalize()
    symbols = frame["symbol"].astype(str).to_numpy()
    keys = pd.MultiIndex.from_arrays([symbols, dates])
    if keys.has_duplicates:
        raise ValueError("Duplicate JP symbol/session in label input")
    opens = pd.to_numeric(frame["open"], errors="coerce").to_numpy(dtype=float)
    volume = pd.to_numeric(frame["volume"], errors="coerce").to_numpy(dtype=float)
    valid_prices = np.isfinite(opens) & (opens > 0) & np.isfinite(volume) & (volume > 0)
    lookup = pd.Series(np.where(valid_prices, opens, np.nan), index=keys)
    positions = days.get_indexer(dates)
    usable = (positions >= 0) & (positions + lag + horizon < len(days))
    result = np.full(len(frame), np.nan)
    if usable.any():
        entry_keys = pd.MultiIndex.from_arrays(
            [symbols[usable], days[positions[usable] + lag]]
        )
        exit_keys = pd.MultiIndex.from_arrays(
            [symbols[usable], days[positions[usable] + lag + horizon]]
        )
        entry = lookup.reindex(entry_keys).to_numpy()
        exit_price = lookup.reindex(exit_keys).to_numpy()
        result[usable] = exit_price / entry - 1
    return pd.Series(result, index=frame.index, name="label")
