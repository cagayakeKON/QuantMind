"""Load a pinned daily JP snapshot and optional sourced historical units."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pandas as pd

from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.shared.stock_utils import StockCodeUtil
from .rules import RuleDataMissing, TradingCalendar


class JPExecutionData:
    def __init__(self, hub: QuantJPDataHub, units_path: str | Path | None = None):
        self.hub = hub
        calendar = hub.fetch_calendar()
        if calendar.empty:
            raise RuleDataMissing("JP cash-equity calendar is unavailable")
        self.calendar = TradingCalendar(
            pd.to_datetime(calendar.trade_date).dt.date.tolist()
        )
        self.units: dict[str, list[dict]] = {}
        if units_path and Path(units_path).is_file():
            with Path(units_path).open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    symbol = StockCodeUtil.to_prefix(row["symbol"], market="JP")
                    first, last = (
                        date.fromisoformat(row["valid_from"]),
                        date.fromisoformat(row["valid_to"]),
                    )
                    unit = int(row["lot_size"])
                    if first > last or unit <= 0 or not row.get("source", "").strip():
                        raise RuleDataMissing(
                            "Historical units require valid dates, units and a source"
                        )
                    items = self.units.setdefault(symbol, [])
                    if any(
                        first <= item["valid_to"] and last >= item["valid_from"]
                        for item in items
                    ):
                        raise RuleDataMissing(
                            f"Overlapping historical units for {symbol}"
                        )
                    items.append(
                        {"valid_from": first, "valid_to": last, "lot_size": unit}
                    )

    def day(self, day: date, symbols: list[str], held_symbols: list[str] | None = None):
        canonical = [StockCodeUtil.to_prefix(s, market="JP") for s in symbols]
        prices = self.hub.fetch_daily_kline_batch(canonical, day, day, adjust="none")
        master = self.hub.fetch_stock_list(day)
        if master.empty or pd.to_datetime(master.time).dt.date.max() != day:
            raise RuleDataMissing(f"Exact dated JP master unavailable on {day}")
        master = master[
            master.symbol.isin(
                [StockCodeUtil.to_suffix(s, market="JP") for s in canonical]
            )
        ]
        meta = {}
        for row in master.to_dict("records"):
            symbol = StockCodeUtil.to_prefix(row["symbol"], market="JP")
            meta[symbol] = row
            for interval in self.units.get(symbol, []):
                if interval["valid_from"] <= day <= interval["valid_to"]:
                    row["lot_size"] = interval["lot_size"]
        bars = {}
        for row in prices.to_dict("records"):
            row = {k: None if pd.isna(value) else value for k, value in row.items()}
            bars[StockCodeUtil.to_prefix(row["symbol"], market="JP")] = row
        # Previous raw close is used only for limit estimation, never as a fill.
        index = self.calendar.sessions.index(day)
        if index:
            previous = self.calendar.sessions[index - 1]
            prev = self.hub.fetch_daily_kline_batch(
                canonical, previous, previous, adjust="none"
            )
            for row in prev.to_dict("records"):
                symbol = StockCodeUtil.to_prefix(row["symbol"], market="JP")
                if symbol in meta and not pd.isna(row["close"]):
                    meta[symbol]["previous_close"] = row["close"]
        for symbol in held_symbols or []:
            if symbol not in meta:
                raise RuleDataMissing(
                    f"Delisting/transfer treatment is required for held {symbol} on {day}"
                )
        return bars, meta

    def latest_price_date(self) -> date:
        dates = self.hub._partition_dates("1_kline_data/daily_unadjusted")
        if not dates:
            raise RuleDataMissing("JP raw daily bars are unavailable")
        return date.fromisoformat(f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}")
