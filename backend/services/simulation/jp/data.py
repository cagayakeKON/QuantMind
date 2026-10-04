"""Load a pinned daily JP snapshot and optional sourced historical units."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from functools import lru_cache

import pandas as pd

from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.shared.stock_utils import StockCodeUtil
from backend.services.simulation.services.local_market_data import DailyBar
from .rules import RuleDataMissing, TradingCalendar, lot_size
from backend.services.engine.data_platform.jp_publication import publication_path


@lru_cache(maxsize=64)
def _execution_history_digest(pinned_root, relative, processed_through):
    """Bounded per-session reads; cache keys identify an immutable publication."""
    hub = QuantJPDataHub(pinned_root)
    digest = hashlib.sha256()
    for partition in hub._partition_dates(relative, end=processed_through):
        day = date.fromisoformat(f"{partition[:4]}-{partition[4:6]}-{partition[6:]}")
        frame = hub._read(relative, day, day)
        frame = frame.drop(columns=["price_factor", "volume_factor"], errors="ignore")
        frame = frame.reindex(sorted(frame.columns), axis=1)
        if not frame.empty:
            frame = frame.sort_values(["dt", "symbol"]).reset_index(drop=True)

        def encode(value):
            if pd.isna(value):
                return None
            if isinstance(value, float):
                return ["float64", value.hex()]
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return value

        records = [
            {key: encode(value) for key, value in row.items()}
            for row in frame.to_dict("records")
        ]
        content = json.dumps(
            records, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        digest.update(partition.encode())
        digest.update(hashlib.sha256(content.encode()).digest())
    return digest.hexdigest()


def to_daily_bar(
    day: date, symbol: str, raw: dict, metadata: dict, *, suspended: bool = False
) -> DailyBar:
    """Project raw JPY values for the common opening matcher.

    The accompanying dated rules inspect raw missing values and limit flags;
    zero placeholders in the existing DailyBar contract are never fill prices.
    VWAP and generic percentage limits are not execution substitutes.
    """
    return DailyBar(
        symbol=StockCodeUtil.to_suffix(symbol, market="JP"),
        trade_date=day,
        open=raw.get("open") or 0,
        high=raw.get("high") or 0,
        low=raw.get("low") or 0,
        close=raw.get("close") or 0,
        volume=raw.get("volume") or 0,
        amount=raw.get("amount") or 0,
        vwap=0,
        pre_close=0,
        limit_up=float("inf"),
        limit_down=0,
        is_st=False,
        suspended=suspended,
        lot_size=lot_size(day, metadata),
    )


def open_execution_data(version: str | None = None) -> JPExecutionData:
    """Pin the same publication used by the former session-local factory."""
    hub = QuantJPDataHub()
    root = hub._publication_root.resolve()
    if version:
        pinned = (root / "versions" / version).resolve()
        if (
            not pinned.is_relative_to(root / "versions")
            or not (pinned / "manifest.json").is_file()
        ):
            raise RuleDataMissing("Pinned JP data version is unavailable")
        hub = QuantJPDataHub(pinned)
    else:
        hub = QuantJPDataHub(publication_path(root, raw=True))
    if not (hub.data_dir / "manifest.json").is_file():
        raise RuleDataMissing("JP immutable publication is unavailable")
    manifest = json.loads((hub.data_dir / "manifest.json").read_text("utf-8"))
    units = manifest.get("trading_units")
    units_path = None
    if units:
        units_path = (hub.data_dir / units["path"]).resolve()
        if (
            not units_path.is_relative_to(hub.data_dir.resolve())
            or not units_path.is_file()
            or hashlib.sha256(units_path.read_bytes()).hexdigest() != units["sha256"]
        ):
            raise RuleDataMissing(
                "Published JP trading units fail integrity validation"
            )
    return JPExecutionData(hub, units_path)


class JPExecutionData:
    execution_data_errors = (RuleDataMissing,)

    def prove_history_extension(self, old_version, processed_through):
        """Compare consumed execution inputs, never adjusted research prices.

        Later splits can change forward-adjusted history without changing past
        raw execution. Revisions to raw prices/master/calendar/units block use.
        """
        old = open_execution_data(old_version)
        if self.latest_price_date() < old.latest_price_date():
            raise RuleDataMissing("JP publication cannot roll back its price coverage")
        if old.units_sha256 != self.units_sha256:
            raise RuleDataMissing("JP publication changed pinned trading units")
        original_calendar = pd.read_parquet(
            old.hub.data_dir / "2_base_sector/trading_calendar/calendar.parquet"
        )
        cutoff = pd.to_datetime(original_calendar.trade_date).dt.date.max()
        if [s for s in self.calendar.sessions if s <= cutoff] != old.calendar.sessions:
            raise RuleDataMissing("JP publication revised its existing calendar")
        proofs = {}
        for relative in ("1_kline_data/daily_unadjusted", "2_base_sector/master"):
            hashes = [
                _execution_history_digest(
                    reader.hub.data_dir.resolve(), relative, processed_through
                )
                for reader in (old, self)
            ]
            if hashes[0] != hashes[1]:
                raise RuleDataMissing(
                    "JP publication revised consumed execution inputs: " + relative
                )
            proofs[relative] = hashes[0]
        return {
            "from_version": old_version,
            "to_version": self.data_version,
            "processed_through": str(processed_through),
            "execution_history_sha256": proofs,
            "trading_units_sha256": self.units_sha256,
            "calendar_sha256": hashlib.sha256(
                "\n".join(map(str, old.calendar.sessions)).encode()
            ).hexdigest(),
        }

    def __init__(self, hub: QuantJPDataHub, units_path: str | Path | None = None):
        self.hub = hub
        calendar = hub.fetch_calendar()
        if calendar.empty:
            raise RuleDataMissing("JP cash-equity calendar is unavailable")
        self.calendar = TradingCalendar(
            pd.to_datetime(calendar.trade_date).dt.date.tolist()
        )
        self.units: dict[str, list[dict]] = {}
        self.units_sha256 = (
            hashlib.sha256(Path(units_path).read_bytes()).hexdigest()
            if units_path
            else None
        )
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

    @property
    def data_version(self) -> str:
        return self.hub.data_dir.name

    def load_date(self, trade_date: date, symbols: list[str] | None = None):
        """Existing LocalMarketData read contract, with exact dated JP units."""
        if symbols is None:
            master = self.hub.fetch_stock_list(trade_date)
            if master.empty or pd.to_datetime(master.time).dt.date.max() != trade_date:
                raise RuleDataMissing(
                    f"Exact dated JP master unavailable on {trade_date}"
                )
            symbols = master.loc[master.product_category == "011", "symbol"].tolist()
        if not symbols:
            return {}
        bars, metadata = self.day(trade_date, symbols)
        projected = {}
        for symbol, raw in bars.items():
            if symbol not in metadata:
                raise RuleDataMissing(
                    f"Missing dated JP master/units: {symbol} on {trade_date}"
                )
            bar = to_daily_bar(
                trade_date,
                symbol,
                raw,
                metadata[symbol],
                suspended=any(
                    raw.get(field) is None or raw[field] <= 0
                    for field in ("open", "close", "volume")
                ),
            )
            projected[bar.symbol] = bar
        return projected

    def get_bar(self, symbol: str, trade_date: date):
        return self.load_date(trade_date, [symbol]).get(
            StockCodeUtil.to_suffix(symbol, market="JP")
        )

    def latest_trade_date(self, on_or_before: date | None = None):
        dates = self.hub._partition_dates(
            "1_kline_data/daily_unadjusted", end=on_or_before
        )
        if not dates:
            return None
        return date.fromisoformat(f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}")

    def matching_rules(self, symbol: str, trade_date: date, *, used_volume: int = 0):
        from .matching_rules import JapanDailyMatchRules

        if (
            isinstance(used_volume, bool)
            or not isinstance(used_volume, int)
            or used_volume < 0
        ):
            raise ValueError("Observed used volume must be a nonnegative integer")
        canonical = StockCodeUtil.to_prefix(symbol, market="JP")
        bars, metadata = self.day(trade_date, [canonical])
        if canonical not in metadata:
            raise RuleDataMissing(
                f"Missing dated JP master/units: {canonical} on {trade_date}"
            )
        return JapanDailyMatchRules(
            metadata[canonical], bars.get(canonical, {}), used_volume
        )
