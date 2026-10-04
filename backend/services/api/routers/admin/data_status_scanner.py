"""市场感知的数据状态扫描器。

供 API 实时调用（`/admin/models/data-status`）和 Celery 后台预热
（`engine.tasks.get_data_status_task`）共享，避免双方扫描逻辑漂移。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import exchange_calendars as xcals
except Exception:
    xcals = None

from backend.shared.trading_calendar import calendar_service

from .model_management_utils import _scan_feature_snapshots_status


# 市场 → Qlib 子目录
# parquet 单源市场（A股/HK/US/期货）读各自 .qlib_cache；crypto 仍用 legacy 5min
_QDB_DATA_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", str(Path(os.getcwd()) / "data" / "quantdb")))
_QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(Path(os.getcwd()) / "data" / "quanthk"))
)
_QUANTUS_DATA_DIR = Path(
    os.getenv("QM_QUANTUS_DATA_DIR", str(Path(os.getcwd()) / "data" / "quantus"))
)
_QUANTFUTURES_DATA_DIR = Path(
    os.getenv("QM_QUANTFUTURES_DATA_DIR", str(Path(os.getcwd()) / "data" / "quantfutures"))
)
def _resolve_cn_qlib_dir() -> str:
    """A 股 Qlib 目录统一走 qlib_paths 解析。"""
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri
        return resolve_qlib_provider_uri("CN")
    except Exception:
        return str(_QDB_DATA_DIR / ".qlib_cache" / "cn_data")


def _resolve_market_qlib_dir(market: str, fallback: Path) -> Path:
    """各市场 Qlib 目录统一走 qlib_paths（固定目录 /data/qlib/{sub} 优先）。"""
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri
        return Path(resolve_qlib_provider_uri(market))
    except Exception:
        return fallback


_MARKET_QLIB_DIRS: dict[str, Path] = {
    "a_share": _resolve_market_qlib_dir("CN", _QDB_DATA_DIR / ".qlib_cache" / "cn_data"),
    "crypto": _resolve_market_qlib_dir(
        "CRYPTO", Path(os.getcwd()) / "db" / "qlib_data" / "crypto_data"
    ),
    "hong_kong": _resolve_market_qlib_dir("HK", _QUANTHK_DATA_DIR / ".qlib_cache" / "hk_data"),
    "us_stock": _resolve_market_qlib_dir("US", _QUANTUS_DATA_DIR / ".qlib_cache" / "us_data"),
    "futures": _resolve_market_qlib_dir(
        "FUTURES", _QUANTFUTURES_DATA_DIR / ".qlib_cache" / "futures_data"
    ),
}

# 市场 → 交易日历服务 market 代码
_CALENDAR_MARKET_MAP: dict[str, str] = {
    "a_share": "SSE",
    "crypto": "SSE",  # 7x24，用 A 股日历近似
    "hong_kong": "HKEX",
    "us_stock": "NYSE",
}

# 市场 → xcals 日历代码（Celery 同步路径用）
_XCALS_MARKET_MAP: dict[str, str] = {
    "a_share": "XSHG",
    "crypto": "XSHG",
    "hong_kong": "XHKG",
    "us_stock": "XNYS",
}


def _resolve_qlib_dir(market: str) -> Path:
    if _is_jp(market):
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        # Match the registered builder; the global legacy QLIB_PROVIDER_URI
        # may point at CN and cannot describe the new market's data status.
        return QlibDataBuilder.for_market("JP").qlib_dir
    return _MARKET_QLIB_DIRS.get(market, _MARKET_QLIB_DIRS["a_share"])


def _resolve_calendar_market(market: str) -> str:
    if _is_jp(market):
        return "XTKS"
    return _CALENDAR_MARKET_MAP.get(market, "SSE")


def _is_jp(market: str) -> bool:
    return str(market).lower() in {"jp", "japan"}


def _jp_trade_date() -> str:
    from backend.services.engine.data_platform.jp_calendar import resolve_cash_session

    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    direction = "previous" if now.hour < 9 else "on_or_before"
    return resolve_cash_session(now.date(), direction=direction).isoformat()


def resolve_trade_date_sync(market: str) -> str:
    """同步解析交易日，用于 Celery worker（避免跨 loop asyncpg 池冲突）。

    优先使用 exchange_calendars；不可用时回退到当前日期。
    """
    if _is_jp(market):
        return _jp_trade_date()
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    if xcals is None:
        return now_local.date().isoformat()

    try:
        cal_code = _XCALS_MARKET_MAP.get(market, "XSHG")
        cal = xcals.get_calendar(cal_code)
        if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
            trade_date_obj = cal.previous_session(now_local.date()).date()
        else:
            if cal.is_session(now_local.date()):
                trade_date_obj = now_local.date()
            else:
                trade_date_obj = cal.previous_session(now_local.date()).date()
        return trade_date_obj.isoformat()
    except Exception:
        return now_local.date().isoformat()


async def _resolve_trade_date(market: str, tenant_id: str, user_id: str) -> str:
    """根据市场日历返回当前应参照的交易日 ISO 字符串。"""
    if _is_jp(market):
        return _jp_trade_date()
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    cal_market = _resolve_calendar_market(market)

    if now_local.time() < datetime.strptime("09:30", "%H:%M").time():
        trade_date_obj = await calendar_service.prev_trading_day(
            market=cal_market,
            trade_date=now_local.date(),
            tenant_id=tenant_id,
            user_id=user_id,
        )
    else:
        is_td = await calendar_service.is_trading_day(
            market=cal_market,
            trade_date=now_local.date(),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if is_td:
            trade_date_obj = now_local.date()
        else:
            trade_date_obj = await calendar_service.prev_trading_day(
                market=cal_market,
                trade_date=now_local.date(),
                tenant_id=tenant_id,
                user_id=user_id,
            )
    return trade_date_obj.isoformat()


def _scan_qlib_info(qlib_data_dir: Path, market: str) -> dict[str, Any]:
    """扫描指定市场的 Qlib 目录元数据。"""
    calendar_files: list[str] = []
    cal_dir = qlib_data_dir / "calendars"
    if cal_dir.exists():
        for f in cal_dir.iterdir():
            if f.suffix == ".txt":
                calendar_files.append(f.name)

    cal_file = (
        "5min.txt"
        if market == "crypto" and (cal_dir / "5min.txt").exists()
        else "day.txt"
    )
    calendars_path = qlib_data_dir / "calendars" / cal_file
    instruments_all_path = qlib_data_dir / "instruments" / "all.txt"
    features_root = qlib_data_dir / "features"

    qlib_info: dict[str, Any] = {
        "qlib_dir": str(qlib_data_dir),
        "exists": qlib_data_dir.exists() and qlib_data_dir.is_dir(),
        "calendar_total_days": 0,
        "calendar_start_date": None,
        "calendar_last_date": None,
        "calendar_files": calendar_files,
        "instruments": {"total": 0, "sh": 0, "sz": 0, "bj": 0, "other": 0},
        "feature_dirs_total": 0,
        "feature_dirs_sh_sz_bj": 0,
        "latest_date_coverage": {
            "target_date": None,
            "at_target_count": 0,
            "older_count": 0,
            "invalid_count": 0,
        },
    }

    if calendars_path.exists():
        try:
            calendar = [
                x.strip()
                for x in calendars_path.read_text(encoding="utf-8").splitlines()
                if x.strip()
            ]
            if calendar:
                qlib_info["calendar_total_days"] = len(calendar)
                qlib_info["calendar_start_date"] = calendar[0]
                qlib_info["calendar_last_date"] = calendar[-1]
                qlib_info["latest_date_coverage"]["target_date"] = calendar[-1]
        except Exception:
            pass

    if instruments_all_path.exists():
        try:
            for line in instruments_all_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                code = line.split()[0].strip().upper()
                qlib_info["instruments"]["total"] += 1
                if code.startswith("SH"):
                    qlib_info["instruments"]["sh"] += 1
                elif code.startswith("SZ"):
                    qlib_info["instruments"]["sz"] += 1
                elif code.startswith("BJ"):
                    qlib_info["instruments"]["bj"] += 1
                else:
                    qlib_info["instruments"]["other"] += 1
        except Exception:
            pass

    if features_root.exists() and features_root.is_dir():
        feature_dirs = [p for p in features_root.iterdir() if p.is_dir()]
        qlib_info["feature_dirs_total"] = len(feature_dirs)
        qlib_info["sync_partial"] = True

    return qlib_info


async def scan_data_status(
    market: str = "a_share",
    tenant_id: str = "default",
    user_id: str = "admin",
    trade_date: str | None = None,
) -> dict[str, Any]:
    """市场感知的数据状态扫描。

    返回结构与原 `/admin/models/data-status` 响应保持一致，供 API 直接序列化、
    Celery worker 直接写入 Redis。

    Parameters
    ----------
    trade_date : str, optional
        预解析的交易日 ISO 字符串。传 None 则异步调用 calendar_service 自动解析。
        Celery worker 应传同步解析的日期以避免跨事件循环的 asyncpg 冲突。
    """
    now_local = datetime.now(ZoneInfo("Asia/Tokyo" if _is_jp(market) else "Asia/Shanghai"))
    if trade_date is None:
        trade_date = await _resolve_trade_date(market, tenant_id, user_id)

    qlib_data_dir = _resolve_qlib_dir(market)
    qlib_info = _scan_qlib_info(qlib_data_dir, market)
    if _is_jp(market):
        feature_snapshots_info = _scan_registered_features(trade_date)
    else:
        feature_snapshots_info = _scan_feature_snapshots_status(
            target_date=trade_date,
            topn=20,
            market=market,
        )

    return {
        "checked_at": now_local.isoformat(),
        "trade_date": trade_date,
        "market": market,
        "qlib_data": qlib_info,
        "feature_snapshots": feature_snapshots_info,
    }


def _scan_registered_features(target_date: str) -> dict[str, Any]:
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )
    import pyarrow.parquet as pq

    hub = LOCAL_MARKET_PROVIDERS["JP"].open()
    folder = hub.data_dir / "6_ml_datasets" / "l1_factors"
    files = sorted(folder.glob("dt=*/data.parquet"))
    days = [
        date.fromisoformat(
            f"{p.parent.name[3:7]}-{p.parent.name[7:9]}-{p.parent.name[9:11]}"
        ).isoformat()
        for p in files
    ]
    coverage = {
        "target_date": target_date,
        "at_target_count": 0,
        "older_count": 0,
        "invalid_count": 0,
    }
    eligible = [day for day in days if day <= target_date]
    if eligible:
        latest = eligible[-1]
        last_day = date.fromisoformat(latest)
        last = hub.fetch_l1_factors(start=last_day, end=last_day)
        count = int(last.symbol.nunique()) if "symbol" in last else 0
        coverage["at_target_count" if latest == target_date else "older_count"] = count
    return {
        "exists": bool(files),
        "snapshot_dir": str(folder),
        "data_version": hub.data_dir.name,
        "file_count": len(files),
        "scanned_files": len(files),
        "failed_files": 0,
        "total_rows": sum(pq.read_metadata(p).num_rows for p in files),
        "min_date": days[0] if days else None,
        "max_date": days[-1] if days else None,
        "metadata_files": [],
        "latest_date_coverage": coverage,
        "topn_samples": {"sample_size": 20, "older_samples": [], "invalid_samples": []},
        "suggested_periods": None,
    }
