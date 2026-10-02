"""Japanese data hub: raw execution prices and split-adjusted research prices."""

from __future__ import annotations

import os
import json
import threading
from datetime import date
from pathlib import Path

import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.stock_utils import StockCodeUtil


def _resolve_quantjp_data_dir() -> Path:
    configured = os.getenv("QM_QUANTJP_DATA_DIR", "").strip()
    if configured:
        # An explicit JP path must never resolve to a different market.
        return Path(configured).expanduser()
    container = Path("/data/quantjp")
    if container.is_dir():
        return container
    return Path(__file__).resolve().parents[4] / "data" / "quantjp"


class QuantJPDataHub(QuantDBDataHub):
    """Read JP partitions without inheriting CN dataset views or conversions."""

    _instance: QuantJPDataHub | None = None
    _instance_lock = threading.Lock()
    _VIEW_REL_MAP = {
        "qjp_daily_unadjusted": "1_kline_data/daily_unadjusted",
        "qjp_daily_forward": "1_kline_data/daily_forward",
        "qjp_index_daily": "1_kline_data/index_daily",
        "qjp_valuation": "5_technical_derived/valuation",
        "qjp_l1_factors": "6_ml_datasets/l1_factors",
        "qjp_master": "2_base_sector/master",
    }

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._publication_root = Path(data_dir or _resolve_quantjp_data_dir())
        super().__init__(self._publication_root)

    @property
    def data_dir(self) -> Path:
        pointer = self._publication_root / "current.json"
        selected = self._publication_root
        if pointer.is_file():
            metadata = json.loads(pointer.read_text(encoding="utf-8"))
            selected = (self._publication_root / metadata["path"]).resolve()
            if not selected.is_relative_to(self._publication_root.resolve()):
                raise ValueError("JP dataset pointer escapes its publication root")
            if not (selected / "manifest.json").is_file():
                raise RuntimeError("JP dataset publication is incomplete")
        if selected != self._data_dir:
            with self._dir_lock:
                if selected != self._data_dir:
                    self._data_dir = selected
                    self._dir_generation += 1
                    self._views_mounted_per_conn.clear()
        return self._data_dir

    @property
    def available(self) -> bool:
        return bool(self._partition_dates("1_kline_data/daily_unadjusted"))

    def _mount_views(self, conn, force: bool = False) -> None:
        if id(conn) in self._views_mounted_per_conn and not force:
            return
        for view, relative in self._VIEW_REL_MAP.items():
            root = self.data_dir / relative
            if not root.is_dir() or not any(root.glob("dt=*/*.parquet")):
                continue
            pattern = (root / "dt=*" / "*.parquet").as_posix().replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM "
                f"read_parquet('{pattern}', hive_partitioning=true, union_by_name=true)"
            )
        self._views_mounted_per_conn.add(id(conn))

    def _read(
        self,
        relative: str,
        start: date | None = None,
        end: date | None = None,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        dates = self._partition_dates(relative, start, end)
        files = [
            str(file)
            for day in dates
            for file in sorted(
                (self.data_dir / relative / f"dt={day}").glob("*.parquet")
            )
        ]
        if not files:
            return pd.DataFrame()
        sql = (
            "SELECT * FROM read_parquet(?, hive_partitioning=true, union_by_name=true)"
        )
        params: list = [files]
        if symbols is not None:
            if not symbols:
                return pd.DataFrame()
            sql += " WHERE symbol IN (SELECT unnest(?))"
            params.append(symbols)
        sql += " ORDER BY dt, symbol"
        return self._exact_conn().execute(sql, params).fetchdf()

    def fetch_daily_kline(
        self, symbol: str, start=None, end=None, *, adjust: str = "qfq"
    ) -> pd.DataFrame:
        return self.fetch_daily_kline_batch([symbol], start, end, adjust=adjust)

    def fetch_daily_kline_batch(
        self, symbols: list[str], start=None, end=None, *, adjust: str = "qfq"
    ) -> pd.DataFrame:
        if adjust not in {"qfq", "none", "raw", "unadjusted"}:
            raise ValueError(f"Unsupported JP adjustment: {adjust}")
        relative = (
            "1_kline_data/daily_forward"
            if adjust == "qfq"
            else "1_kline_data/daily_unadjusted"
        )
        wanted = [StockCodeUtil.to_suffix(s, market="JP") for s in symbols]
        return self._normalize_kline(self._read(relative, start, end, wanted))

    def fetch_index_kline(self, symbol: str, start=None, end=None) -> pd.DataFrame:
        if str(symbol).upper() not in {"TOPIX", "TOPIX.JP", "JPTOPIX", "JP_TOPIX"}:
            raise ValueError("JP currently provides the TOPIX price index")
        return self._normalize_kline(
            self._read("1_kline_data/index_daily", start, end, ["TOPIX.JP"])
        )

    def fetch_stock_list(self, as_of: date | None = None) -> pd.DataFrame:
        days = self._partition_dates("2_base_sector/master", end=as_of)
        if not days:
            return pd.DataFrame()
        day = date.fromisoformat(f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}")
        return self._read("2_base_sector/master", day, day)

    def fetch_calendar(self, start=None, end=None) -> pd.DataFrame:
        frame = super().fetch_calendar(start, end)
        if frame.empty:
            return frame
        # OSE holiday trading (HolDiv=3) is not a cash-equity session.
        return frame[frame["is_open"].eq(True)].reset_index(drop=True)

    def fetch_instrument_periods(self) -> pd.DataFrame:
        """Use observed historical prices, including delisted securities."""
        dates = self._partition_dates("1_kline_data/daily_unadjusted")
        files = [
            str(
                self.data_dir / "1_kline_data/daily_unadjusted" / f"dt={d}/data.parquet"
            )
            for d in dates
        ]
        if not files:
            return pd.DataFrame()
        return (
            self._exact_conn()
            .execute(
                "SELECT symbol, min(CAST(time AS DATE)) AS start_date, "
                "max(CAST(time AS DATE)) AS end_date "
                "FROM read_parquet(?, hive_partitioning=true) "
                "GROUP BY symbol ORDER BY symbol",
                [files],
            )
            .fetchdf()
        )

    def fetch_valuation(self, symbol=None, start=None, end=None) -> pd.DataFrame:
        wanted = [StockCodeUtil.to_suffix(symbol, market="JP")] if symbol else None
        return self._normalize_columns(
            self._read("5_technical_derived/valuation", start, end, wanted)
        )

    def fetch_l1_factors(self, symbol=None, start=None, end=None) -> pd.DataFrame:
        wanted = [StockCodeUtil.to_suffix(symbol, market="JP")] if symbol else None
        return self._normalize_columns(
            self._read("6_ml_datasets/l1_factors", start, end, wanted)
        )
