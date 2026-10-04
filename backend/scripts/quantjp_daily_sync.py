"""Own a JP source cache, download directly from J-Quants, publish atomically.

Seed from the existing read-only DuckDB snapshot once; never update that source.
Subsequent scheduled runs refresh a trailing date window in our separate cache.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb
import pyarrow as pa

from backend.services.engine.data_platform.jquants_client import JQuantsClient
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import _resolve_quantjp_data_dir
from backend.services.engine.data_platform.jp_file_lock import exclusive_file_lock

_SCHEMAS = {
    "daily_prices": {
        "Date": "DATE",
        "Code": "VARCHAR",
        **dict.fromkeys(["O", "H", "L", "C", "Vo", "Va", "AdjFactor"], "DOUBLE"),
        **dict.fromkeys(["ExRT", "UL", "LL"], "VARCHAR"),
    },
    "master": {
        "Date": "DATE",
        **dict.fromkeys(
            [
                "Code",
                "CoName",
                "CoNameEn",
                "Mkt",
                "MktNm",
                "S17",
                "S33",
                "S33Nm",
                "ScaleCat",
                "ProdCat",
            ],
            "VARCHAR",
        ),
    },
    "topix": {"Date": "DATE", **dict.fromkeys(["O", "H", "L", "C"], "DOUBLE")},
    "calendar": {"Date": "DATE", "HolDiv": "VARCHAR"},
    "valuation": {
        "Date": "DATE",
        "Code": "VARCHAR",
        **dict.fromkeys(["EPS", "BPS", "ROE", "PER", "PBR", "MktCap"], "DOUBLE"),
    },
}
_ENDPOINTS = {
    "daily_prices": "/equities/bars/daily",
    "master": "/equities/master",
    "topix": "/indices/bars/daily/topix",
    "valuation": "/equities/valuation",
}


def _initialize(conn, seed: Path | None):
    conn.execute("CREATE SCHEMA IF NOT EXISTS quantmind_meta")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quantmind_meta.jp_cache_owner (owner VARCHAR)"
    )
    conn.execute(
        "INSERT INTO quantmind_meta.jp_cache_owner SELECT 'QuantMind JP source cache' WHERE NOT EXISTS (SELECT 1 FROM quantmind_meta.jp_cache_owner)"
    )
    conn.execute("CREATE SCHEMA IF NOT EXISTS research")
    if seed:
        existing = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='research'"
        ).fetchone()[0]
        if existing:
            raise ValueError(
                "JP source cache already exists; seed is only for an empty cache"
            )
        if not seed.is_file():
            raise FileNotFoundError(seed)
        escaped = seed.as_posix().replace("'", "''")
        conn.execute(f"ATTACH '{escaped}' AS seed (READ_ONLY)")
        conn.execute("BEGIN TRANSACTION")
        try:
            for table, fields in _SCHEMAS.items():
                exists = conn.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_catalog='seed' AND table_schema='research' AND table_name=?",
                    [table],
                ).fetchone()[0]
                if not exists and table == "valuation":
                    definitions = ",".join(
                        f'"{col}" {kind}' for col, kind in fields.items()
                    )
                    conn.execute(f"CREATE TABLE research.{table} ({definitions})")
                else:
                    columns = ",".join(f'"{column}"' for column in fields)
                    conn.execute(
                        f"CREATE TABLE research.{table} AS SELECT {columns} FROM seed.research.{table}"
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("DETACH seed")
    else:
        for table, fields in _SCHEMAS.items():
            columns = ",".join(f'"{column}" {kind}' for column, kind in fields.items())
            conn.execute(f"CREATE TABLE IF NOT EXISTS research.{table} ({columns})")


def _restore_publication(conn, target: Path):
    """Seed our owned cache from published raw partitions, never discard history."""
    from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub

    from backend.services.engine.data_platform.jp_publication import publication_path

    hub = QuantJPDataHub(publication_path(target, raw=True))
    if not hub.available:
        raise ValueError(
            "Import the historical JP snapshot, or provide --seed, before automatic sync"
        )
    mappings = {
        "daily_prices": (
            "1_kline_data/daily_unadjusted",
            "time AS Date, source_code AS Code, open AS O, high AS H, low AS L, close AS C, volume AS Vo, amount AS Va, adj_factor AS AdjFactor, ex_rights_type AS ExRT, CASE WHEN upper_limit_touched THEN '1' ELSE '0' END AS UL, CASE WHEN lower_limit_touched THEN '1' ELSE '0' END AS LL",
        ),
        "master": (
            "2_base_sector/master",
            "time AS Date, source_code AS Code, stock_name AS CoName, name_en AS CoNameEn, exchange AS Mkt, exchange_name AS MktNm, sector17_code AS S17, industry_code AS S33, industry_name AS S33Nm, scale_category AS ScaleCat, product_category AS ProdCat",
        ),
        "topix": (
            "1_kline_data/index_daily",
            "time AS Date, open AS O, high AS H, low AS L, close AS C",
        ),
        "valuation": (
            "5_technical_derived/valuation",
            "time AS Date, replace(symbol,'.JP','') AS Code, eps AS EPS, bps AS BPS, roe AS ROE, pe_ttm AS PER, pb AS PBR, total_mv/1000000 AS MktCap",
        ),
    }
    root = hub.data_dir  # Immutable version for this cache restoration.
    conn.execute("BEGIN TRANSACTION")
    try:
        for table, (relative, query) in mappings.items():
            files = [str(file) for file in (root / relative).glob("dt=*/*.parquet")]
            if files:
                conn.execute(
                    f"INSERT INTO research.{table} SELECT {query} FROM read_parquet(?, hive_partitioning=false)",
                    [files],
                )
        conn.execute(
            "INSERT INTO research.calendar SELECT CAST(trade_date AS DATE), CAST(holiday_division AS VARCHAR) FROM read_parquet(?)",
            [str(root / "2_base_sector/trading_calendar/calendar.parquet")],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _check_cache_owner(path: Path):
    if not path.exists():
        return
    with duckdb.connect(str(path), read_only=True) as conn:
        exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='quantmind_meta' AND table_name='jp_cache_owner'"
        ).fetchone()[0]
        if (
            not exists
            or conn.execute(
                "SELECT count(*) FROM quantmind_meta.jp_cache_owner WHERE owner='QuantMind JP source cache'"
            ).fetchone()[0]
            != 1
        ):
            raise ValueError(
                "Refusing to modify a database not owned by QuantMind JP sync"
            )


def _replace_day(conn, table: str, day: date, rows: list[dict]):
    if any(row.get("Date") != str(day) for row in rows):
        raise ValueError(f"J-Quants returned an unexpected query date: {table}")
    if not rows:
        raise ValueError(f"Required JP dataset is not published on {day}: {table}")
    fields = _SCHEMAS[table]
    keys = [(row["Date"], row.get("Code")) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Duplicate J-Quants rows: {table} on {day}")
    values = [
        {
            col: row.get(col, "" if kind == "VARCHAR" else None)
            for col, kind in fields.items()
        }
        for row in rows
    ]
    conn.register("incoming_jp", pa.Table.from_pylist(values))
    projections = ",".join(f'CAST("{col}" AS {kind})' for col, kind in fields.items())
    conn.execute(f"DELETE FROM research.{table} WHERE Date=?", [day])
    conn.execute(f"INSERT INTO research.{table} SELECT {projections} FROM incoming_jp")
    conn.unregister("incoming_jp")


def run(
    *,
    days=5,
    datasets=None,
    seed=None,
    cache=None,
    destination=None,
    end=None,
    client=None,
):
    target = Path(destination or _resolve_quantjp_data_dir()).resolve()
    owned = Path(
        cache
        or os.getenv("QM_JQUANTS_CACHE_DB")
        or target.parent / "jquants_jp/source.duckdb"
    ).resolve()
    original = Path(seed).resolve() if seed else None
    if original and (owned == original or owned.is_relative_to(original.parent)):
        raise ValueError(
            "JP cache must be separate from the existing research snapshot"
        )
    if owned.is_relative_to(target) or target.is_relative_to(owned.parent):
        raise ValueError("JP source cache and published output must be separate")
    _check_cache_owner(owned)  # Validate before creating a lock alongside the file.
    with exclusive_file_lock(owned.with_suffix(".lock")):
        return _run(
            days=days,
            datasets=datasets,
            seed=seed,
            cache=owned,
            destination=target,
            end=end,
            client=client,
        )


def _run(
    *,
    days=5,
    datasets=None,
    seed=None,
    cache=None,
    destination=None,
    end=None,
    client=None,
):
    allowed = set(_SCHEMAS) | {
        "daily_unadjusted",
        "daily_forward",
        "index_daily",
        "trading_calendar",
    }
    if datasets and set(datasets) - allowed:
        raise ValueError(
            "Unsupported JP dataset; JP publishes a consistent complete daily bundle"
        )
    if int(days) <= 0:
        raise ValueError("JP sync days must be positive")
    target = Path(destination or _resolve_quantjp_data_dir()).resolve()
    owned = Path(
        cache
        or os.getenv("QM_JQUANTS_CACHE_DB")
        or target.parent / "jquants_jp/source.duckdb"
    ).resolve()
    original = Path(seed).resolve() if seed else None
    if original and (owned == original or owned.is_relative_to(original.parent)):
        raise ValueError(
            "JP cache must be separate from the existing research snapshot"
        )
    if owned.is_relative_to(target) or target.is_relative_to(owned.parent):
        raise ValueError("JP source cache and published output must be separate")
    _check_cache_owner(owned)
    owned.parent.mkdir(parents=True, exist_ok=True)
    if client is None:
        from backend.shared.data_source_config import is_source_enabled

        if not is_source_enabled("JP", "jquants"):
            raise ValueError("JP J-Quants data source is disabled")
    api = client or JQuantsClient()
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    last = end or (now.date() if now.hour >= 20 else now.date() - timedelta(days=1))
    first = last - timedelta(days=int(days) - 1)
    downloaded = 0
    with duckdb.connect(str(owned)) as conn:
        conn.execute("SET threads=1")
        conn.execute("SET memory_limit='4GB'")
        _initialize(conn, original)
        if (
            not original
            and not conn.execute(
                "SELECT count(*) FROM research.daily_prices"
            ).fetchone()[0]
        ):
            _restore_publication(conn, target)
        calendar = api.rows("/markets/calendar")
        if not calendar:
            raise ValueError("J-Quants calendar unavailable")
        sessions = sorted(
            date.fromisoformat(row["Date"])
            for row in calendar
            if row["HolDiv"] in {"1", "2"}
        )
        # Fetch all datasets for a day before replacing any of its cached rows.
        for day in sessions:
            if not first <= day <= last:
                continue
            bundle = {
                table: api.rows(endpoint, {"date": str(day)})
                for table, endpoint in _ENDPOINTS.items()
            }
            conn.execute("BEGIN TRANSACTION")
            try:
                for table, rows in bundle.items():
                    _replace_day(conn, table, day, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            downloaded += 1
        conn.register("incoming_calendar", pa.Table.from_pylist(calendar))
        conn.execute(
            "CREATE OR REPLACE TABLE research.calendar AS SELECT CAST(Date AS DATE) AS Date, CAST(HolDiv AS VARCHAR) AS HolDiv FROM incoming_calendar"
        )
        conn.unregister("incoming_calendar")
        conn.execute("CHECKPOINT")
    report = import_jquants_snapshot(owned, target)
    from backend.services.engine.data_platform.jp_publication import publication_status

    return {
        "downloaded_sessions": downloaded,
        "publication": report,
        "publication_status": publication_status(target),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    # Publication metadata is safe to print; no credential is included.
    import json

    print(json.dumps(run(**vars(args)), ensure_ascii=False, indent=2))
