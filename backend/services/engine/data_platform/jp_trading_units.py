"""Validate sourced dated execution units before immutable JP publication."""

import csv
from datetime import date
import io

from backend.shared.stock_utils import StockCodeUtil


def read_trading_units(content):
    rows = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    required = {"symbol", "valid_from", "valid_to", "lot_size", "source"}
    if not required.issubset(set(rows.fieldnames or [])):
        raise ValueError("Historical units require dated units and a source")
    units = {}
    for row in rows:
        symbol = StockCodeUtil.to_prefix(row["symbol"], market="JP")
        first, last = (
            date.fromisoformat(row["valid_from"]),
            date.fromisoformat(row["valid_to"]),
        )
        unit = int(row["lot_size"])
        if first > last or unit <= 0 or not row.get("source", "").strip():
            raise ValueError("Historical units require valid dates, units and a source")
        items = units.setdefault(symbol, [])
        if any(
            first <= item["valid_to"] and last >= item["valid_from"] for item in items
        ):
            raise ValueError(f"Overlapping historical units for {symbol}")
        items.append({"valid_from": first, "valid_to": last, "lot_size": unit})
    return units
