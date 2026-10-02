"""Publish an immutable JP dataset from a read-only J-Quants DuckDB snapshot."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
from .jp_file_lock import exclusive_file_lock

_DATASETS = {
    "daily_unadjusted": "1_kline_data/daily_unadjusted",
    "daily_forward": "1_kline_data/daily_forward",
    "master": "2_base_sector/master",
    "index_daily": "1_kline_data/index_daily",
    "valuation": "5_technical_derived/valuation",
}


def _literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _copy_partitions(conn, query: str, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    conn.execute(
        f"COPY ({query} ORDER BY dt, symbol) TO {_literal(target)} "
        "(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY(dt), "
        "FILENAME_PATTERN 'data', ROW_GROUP_SIZE 10000)"
    )
    partitions = list(target.glob("dt=*"))
    for folder in partitions:
        files = list(folder.glob("*.parquet"))
        if not files:
            raise RuntimeError(f"Empty daily partition: {folder}")
        if len(files) == 1:
            files[0].rename(folder / "data.parquet")
        else:
            merged = folder / "data.parquet.part"
            conn.execute(
                f"COPY (SELECT * FROM read_parquet(?, hive_partitioning=false)) "
                f"TO {_literal(merged)} (FORMAT PARQUET, COMPRESSION ZSTD)",
                [[str(file) for file in files]],
            )
            for file in files:
                file.unlink()
            merged.rename(folder / "data.parquet")
    return len(partitions)


def import_jquants_snapshot(
    source: str | Path,
    destination: str | Path,
    *,
    start: date | None = None,
    end: date | None = None,
    progress=None,
) -> dict:
    source_path, output_path = Path(source).resolve(), Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.is_relative_to(output_path) or output_path.is_relative_to(
        source_path.parent
    ):
        raise ValueError("JP output must be separate from the source snapshot")
    with exclusive_file_lock(Path(destination).resolve() / ".publish.lock"):
        return _import_jquants_snapshot(
            source, destination, start=start, end=end, progress=progress
        )


def _import_jquants_snapshot(
    source: str | Path,
    destination: str | Path,
    *,
    start: date | None = None,
    end: date | None = None,
    progress=None,
) -> dict:
    """Export ordinary domestic stocks and atomically publish a complete version.

    A failure leaves the previous current.json untouched. All output belongs to
    this run's new staging directory; source data is never modified.
    """
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.is_relative_to(destination) or destination.is_relative_to(source.parent):
        raise ValueError("JP output must be separate from the source snapshot")
    if start and end and start > end:
        raise ValueError("start must not be after end")
    version = "snapshot-" + uuid.uuid4().hex
    versions = destination / "versions"
    stage = versions / (".staging-" + version)
    stage.mkdir(parents=True)
    bounds = []
    if start:
        bounds.append(f"Date >= DATE '{start.isoformat()}'")
    if end:
        bounds.append(f"Date <= DATE '{end.isoformat()}'")
    date_filter = " AND ".join(bounds) or "TRUE"
    report = {
        "version": version,
        "source": str(source),
        "source_bytes": source.stat().st_size,
        "source_mtime_ns": source.stat().st_mtime_ns,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market": "JP",
        "currency": "JPY",
        "product_category": "011",
        "return_basis": "price_only",
        "datasets": {},
    }
    # COPY may write files while the attached source remains strictly read-only.
    with duckdb.connect() as conn:
        conn.execute("SET threads=1")
        conn.execute("SET memory_limit='4GB'")
        conn.execute(f"ATTACH {_literal(source)} AS source (READ_ONLY)")
        invalid_codes = conn.execute(
            "SELECT count(*) FROM source.research.master "
            "WHERE Code IS NULL OR NOT regexp_full_match(Code, '[0-9][A-Z0-9]{3}[0-9]')"
        ).fetchone()[0]
        if invalid_codes:
            raise ValueError(f"Invalid source security codes: {invalid_codes}")
        duplicates = conn.execute(
            "SELECT count(*) FROM (SELECT Date, Code FROM source.research.daily_prices "
            "GROUP BY Date, Code HAVING count(*) > 1)"
        ).fetchone()[0]
        if duplicates:
            raise ValueError("Source daily prices contain duplicate Date/Code rows")
        duplicate_master = conn.execute(
            "SELECT count(*) FROM (SELECT Date,Code FROM source.research.master "
            "GROUP BY Date,Code HAVING count(*) > 1)"
        ).fetchone()[0]
        if duplicate_master:
            raise ValueError("Source master contains duplicate Date/Code rows")
        missing_master = conn.execute(
            "SELECT count(*) FROM source.research.daily_prices p "
            "ANTI JOIN source.research.master m USING (Date,Code)"
        ).fetchone()[0]
        if missing_master:
            raise ValueError("Source daily prices have no dated master")
        bad_calendar = conn.execute(
            "SELECT count(*) FROM source.research.daily_prices p "
            "LEFT JOIN source.research.calendar c USING (Date) "
            "WHERE c.HolDiv IS NULL OR c.HolDiv NOT IN ('1','2')"
        ).fetchone()[0]
        if bad_calendar:
            raise ValueError("Source prices are outside the JP cash-equity calendar")
        bad_quotes = conn.execute(
            "SELECT count(*) FROM source.research.daily_prices WHERE "
            "(O IS NULL OR H IS NULL OR L IS NULL OR C IS NULL OR Vo IS NULL OR Va IS NULL) "
            "AND NOT (O IS NULL AND H IS NULL AND L IS NULL AND C IS NULL AND Vo IS NULL AND Va IS NULL) "
            "OR O <= 0 OR H < L OR H < greatest(O,C) OR L > least(O,C) OR Vo < 0 OR Va < 0"
        ).fetchone()[0]
        if bad_quotes:
            raise ValueError("Source contains inconsistent raw OHLCV/amount")
        invalid_factors = conn.execute(
            "SELECT count(*) FROM source.research.daily_prices "
            "WHERE AdjFactor IS NULL OR NOT isfinite(AdjFactor) OR AdjFactor <= 0"
        ).fetchone()[0]
        if invalid_factors:
            raise ValueError("Source contains invalid adjustment factors")
        conn.execute(
            "CREATE TEMP TABLE jp_prices AS "
            "SELECT p.Date, p.Code, p.O, p.H, p.L, p.C, p.Vo, p.Va, "
            "p.AdjFactor, p.ExRT, p.UL, p.LL, "
            "coalesce(exp(sum(ln(p.AdjFactor)) OVER (PARTITION BY p.Code "
            "ORDER BY p.Date ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)),1) "
            "AS price_factor, "
            "coalesce(exp(sum(ln(CASE WHEN p.ExRT='3' THEN 1 ELSE p.AdjFactor END)) "
            "OVER (PARTITION BY p.Code ORDER BY p.Date "
            "ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)),1) AS volume_factor "
            "FROM source.research.daily_prices p "
            "JOIN source.research.master m USING (Date, Code) WHERE m.ProdCat='011'"
        )
        count, first, last = conn.execute(
            f"SELECT count(*), min(Date), max(Date) FROM jp_prices WHERE {date_filter}"
        ).fetchone()
        if not count:
            raise ValueError("No ordinary-stock prices in the requested range")
        report.update(rows=count, start_date=str(first), end_date=str(last))
        common = (
            "Date AS time, Code || '.JP' AS symbol, Code AS source_code, "
            "AdjFactor AS adj_factor, ExRT AS ex_rights_type, "
            "UL='1' AS upper_limit_touched, LL='1' AS lower_limit_touched, "
            "price_factor, volume_factor, 'jquants' AS source, 'JPY' AS currency, "
            "strftime(Date, '%Y%m%d')::INTEGER AS dt"
        )
        queries = {
            "daily_unadjusted": (
                f"SELECT {common}, O AS open, H AS high, L AS low, C AS close, "
                f"Vo AS volume, Va AS amount FROM jp_prices WHERE {date_filter}"
            ),
            "daily_forward": (
                f"SELECT {common}, O*price_factor AS open, H*price_factor AS high, "
                "L*price_factor AS low, C*price_factor AS close, "
                f"Vo/volume_factor AS volume, Va AS amount FROM jp_prices "
                f"WHERE {date_filter}"
            ),
            "master": (
                "SELECT Date AS time, Code || '.JP' AS symbol, Code AS source_code, "
                "CoName AS stock_name, CoNameEn AS name_en, Mkt AS exchange, "
                "MktNm AS exchange_name, S17 AS sector17_code, S33 AS industry_code, "
                "S33Nm AS industry_name, ScaleCat AS scale_category, "
                "ProdCat AS product_category, "
                "strftime(Date, '%Y%m%d')::INTEGER AS dt "
                f"FROM source.research.master WHERE ProdCat='011' AND {date_filter}"
            ),
            "index_daily": (
                "SELECT Date AS time, 'TOPIX.JP' AS symbol, O AS open, H AS high, "
                "L AS low, C AS close, 'JPY' AS currency, "
                "strftime(Date, '%Y%m%d')::INTEGER AS dt "
                f"FROM source.research.topix WHERE {date_filter}"
            ),
        }
        has_valuation = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog='source' AND table_schema='research' "
            "AND table_name='valuation'"
        ).fetchone()[0]
        if has_valuation:
            queries["valuation"] = (
                "SELECT v.Date AS time, v.Code || '.JP' AS symbol, "
                "v.PER AS pe_ttm, v.PBR AS pb, v.ROE AS roe, v.EPS AS eps, "
                "v.BPS AS bps, v.MktCap*1000000 AS total_mv, "
                "strftime(v.Date, '%Y%m%d')::INTEGER AS dt "
                "FROM source.research.valuation v "
                "JOIN jp_prices p USING (Date,Code) "
                f"WHERE {date_filter}"
            )
        for name, query in queries.items():
            partitions = _copy_partitions(conn, query, stage / _DATASETS[name])
            report["datasets"][name] = {"partitions": partitions}
            if callable(progress):
                progress(name, partitions)
        calendar_dir = stage / "2_base_sector/trading_calendar"
        calendar_dir.mkdir(parents=True)
        conn.execute(
            "COPY (SELECT Date AS trade_date, HolDiv AS holiday_division, "
            "HolDiv IN ('1','2') AS is_open FROM source.research.calendar "
            f"ORDER BY Date) TO {_literal(calendar_dir / 'calendar.parquet')} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        conn.execute("DETACH source")
    current_stat = source.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (
        report["source_bytes"],
        report["source_mtime_ns"],
    ):
        raise RuntimeError("Source snapshot changed during import")
    (stage / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    published = versions / version
    stage.rename(published)
    pointer = destination / f".current-{uuid.uuid4().hex}.json"
    pointer.write_text(
        json.dumps({"version": version, "path": f"versions/{version}"}),
        encoding="utf-8",
    )
    pointer.replace(destination / "current.json")
    return report
