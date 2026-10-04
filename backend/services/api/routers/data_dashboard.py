"""
数据看板 API
============

为前端数据看板提供全市场全字段数据访问。
复用 FieldAggregator 路由/聚合能力，暴露以下端点：

GET /api/v1/data-dashboard/fields       — 按市场列出可用字段
GET /api/v1/data-dashboard/field-data   — 获取任意字段数据
GET /api/v1/data-dashboard/realtime     — 实时行情
GET /api/v1/data-dashboard/sectors      — 行业板块
GET /api/v1/data-dashboard/meta         — 股票基本信息
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.api.market_analysis.quantdb_realtime import (
    QuantDBRealtimeUnavailable,
    get_snapshots as get_quantdb_snapshots,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/data-dashboard", tags=["DataDashboard"])


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid date: {s}")


_AGG_CACHE = None


def _registered_provider(market: str):
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )

    return LOCAL_MARKET_PROVIDERS.get(market.upper())


_NATIVE_FIELDS = (
    "daily_kline", "stock_list", "sector", "valuation", "calendar", "l1_factors"
)


def _native_field_status(provider):
    """Describe the actual immutable schemas, including unavailable datasets."""
    import pyarrow.parquet as pq

    raw, research = provider.open_raw(), provider.open()
    paths = {
        "daily_kline": "1_kline_data/daily_unadjusted",
        "stock_list": "2_base_sector/master",
        "sector": "2_base_sector/master",
        "valuation": "5_technical_derived/valuation",
        "calendar": "2_base_sector/trading_calendar",
        "l1_factors": "6_ml_datasets/l1_factors",
    }
    fields = []
    for field, relative in paths.items():
        hub = research if field == "l1_factors" else raw
        published = (hub.data_dir / "manifest.json").is_file()
        files = (
            sorted((hub.data_dir / relative).glob("**/*.parquet")) if published else []
        )
        fields.append(
            {
                "field": field,
                "tier": "T1",
                "primary": provider.source,
                "fallbacks": [],
                "consensus": False,
                "cleanup": False,
                "available": bool(files),
                "data_version": hub.data_dir.name if published else None,
                "columns": pq.read_schema(files[-1]).names if files else [],
            }
        )
    return fields


def _fetch_field(market: str, field: str, symbol: str, start=None, end=None):
    """Use one registered raw publication, preserving old aggregator routes."""
    provider = _registered_provider(market)
    if provider is None:
        return _get_aggregator().fetch(
            market=market, field=field, symbol=symbol, start=start, end=end
        )
    if field not in _NATIVE_FIELDS:
        raise HTTPException(status_code=422, detail=f"当前市场未发布字段: {field}")
    from types import SimpleNamespace
    from backend.shared.stock_utils import StockCodeUtil

    hub = provider.open() if field == "l1_factors" else provider.open_raw()
    if field == "daily_kline":
        frame = hub.fetch_daily_kline(symbol, start, end, adjust="none")
    elif field == "valuation":
        frame = hub.fetch_valuation(symbol, start, end)
    elif field == "calendar":
        frame = hub.fetch_calendar(start, end)
    elif field == "l1_factors":
        frame = hub.fetch_l1_factors(symbol, start, end)
        if frame.empty:
            raise HTTPException(
                status_code=422, detail="该区间没有已发布的原生 L1 特征"
            )
    else:
        frame = hub.fetch_stock_list(as_of=end)
        if not frame.empty and symbol:
            wanted = StockCodeUtil.to_suffix(symbol, market=market)
            frame = frame[frame.symbol.eq(wanted)].copy()
        if field == "sector":
            columns = [
                c
                for c in (
                    "symbol",
                    "trade_date",
                    "stock_name",
                    "industry_code",
                    "industry_name",
                )
                if c in frame
            ]
            frame = frame[columns].copy()
    if not frame.empty and "symbol" in frame:
        frame = frame.copy()
        frame["symbol"] = frame.symbol.map(
            lambda value: StockCodeUtil.to_prefix(value, market=market)
        )
    return SimpleNamespace(
        data=frame,
        source_used=provider.source,
        fallbacks_tried=[],
        data_version=hub.data_dir.name,
    )


def _get_aggregator():
    global _AGG_CACHE
    if _AGG_CACHE is None:
        from backend.services.engine.data_platform.adapters import register_all
        from backend.services.engine.data_platform.aggregator import (
            FieldAggregator,
            FieldRoutingTable,
        )
        from backend.services.engine.data_platform.cleaner import DataCleaner
        from backend.services.engine.data_platform.monitor import get_monitor
        from backend.services.engine.data_platform.registry import get_registry

        register_all()
        _AGG_CACHE = FieldAggregator(
            registry=get_registry(),
            routing=FieldRoutingTable(),
            monitor=get_monitor(),
            cleaner=DataCleaner(),
        )
    return _AGG_CACHE


def _get_routing():
    from backend.services.engine.data_platform.aggregator import FieldRoutingTable
    return FieldRoutingTable()


# ---------------------------------------------------------------------------
# GET /fields — 按市场列出所有可用字段
# ---------------------------------------------------------------------------
@router.get("/fields")
async def list_fields(
    market: str = Query("A", description="A / HK / US"),
    current_user: dict = Depends(get_current_user),
):
    provider = _registered_provider(market)
    if provider:
        import asyncio

        fields = await asyncio.to_thread(_native_field_status, provider)
        return {
            "success": True, "market": market.upper(),
            "fields": fields, "count": len(fields),
        }
    routing = _get_routing()
    fields = routing.list_fields(market.upper())
    result = []
    for f in fields:
        try:
            route = routing.get_route(market.upper(), f)
            result.append({
                "field": f,
                "tier": route.tier,
                "primary": route.primary,
                "fallbacks": route.fallbacks,
                "consensus": route.consensus,
                "cleanup": route.cleanup,
            })
        except Exception:
            result.append({"field": f, "tier": "T1", "primary": "", "fallbacks": []})
    return {
        "success": True,
        "market": market.upper(),
        "fields": result,
        "count": len(result),
    }


# ---------------------------------------------------------------------------
# GET /field-data — 获取任意字段数据（通用）
# ---------------------------------------------------------------------------
@router.get("/field-data")
async def get_field_data(
    market: str = Query(..., description="A / HK / US"),
    field: str = Query(..., description="字段名，如 daily_kline, financial_report, dividend"),
    symbol: str = Query(..., description="股票代码"),
    start: str | None = Query(None, description="YYYY-MM-DD"),
    end: str | None = Query(None, description="YYYY-MM-DD"),
    days: int = Query(365, ge=1, le=2000),
    current_user: dict = Depends(get_current_user),
):
    sd = _parse_date(start)
    ed = _parse_date(end)
    if not (sd and ed):
        ed = ed or date.today()
        sd = sd or (ed - timedelta(days=days))

    provider = _registered_provider(market)
    fetch = _fetch_field if provider else _get_aggregator().fetch
    try:
        import asyncio
        result = await asyncio.to_thread(
            fetch,
            market=market.upper(),
            field=field,
            symbol=symbol.upper(),
            start=sd,
            end=ed,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("field-data fetch failed: market=%s field=%s symbol=%s err=%s",
                        market, field, symbol, exc)
        raise HTTPException(status_code=503, detail=f"数据获取失败: {exc}")

    df = result.data
    # 将 DataFrame 转为 records，处理 NaN/NaT
    records = df.replace({float("nan"): None}).to_dict("records")
    # 转换 date/datetime 对象为字符串
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, (date, datetime)):
                rec[k] = v.isoformat()

    return {
        "success": True,
        "market": market.upper(),
        "field": field,
        "symbol": symbol.upper(),
        "source_used": result.source_used,
        "fallbacks_tried": result.fallbacks_tried,
        "count": len(records),
        "columns": list(df.columns),
        "data": records,
        **({"data_version": result.data_version} if hasattr(result, "data_version") else {}),
    }


# ---------------------------------------------------------------------------
# GET /realtime — 实时行情
# ---------------------------------------------------------------------------
@router.get("/realtime")
async def get_realtime(
    market: str = Query("A", description="A / HK / US"),
    symbol: str = Query(..., description="股票代码"),
    current_user: dict = Depends(get_current_user),
):
    """A 股实时行情由 QuantDB Redis DB 3 提供；不再回退为静态元信息。"""
    if market.upper() not in {"A", "CN"}:
        raise HTTPException(status_code=404, detail="非 A 股市场暂不支持")

    try:
        payload = await get_quantdb_snapshots([symbol])
    except QuantDBRealtimeUnavailable as exc:
        raise HTTPException(status_code=503, detail="QuantDB 实时行情源不可用") from exc
    if not payload["items"]:
        raise HTTPException(status_code=404, detail="实时快照不存在或已过期")
    quote = payload["items"][0]
    return {
        "success": True, "market": "CN", "symbol": quote["symbol"],
        "source_used": "quantdb_redis_db3", "quote": quote,
    }

def _is_nan(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float):
        import math
        return math.isnan(v) or math.isinf(v)
    return False


# ---------------------------------------------------------------------------
# GET /sectors — 行业板块
# ---------------------------------------------------------------------------
@router.get("/sectors")
async def get_sectors(
    market: str = Query("A", description="A / HK / US"),
    symbol: str | None = Query(None, description="任意一个该市场的股票代码（用于获取行业信息）"),
    current_user: dict = Depends(get_current_user),
):
    if _registered_provider(market):
        return await get_field_data(
            market=market, field="sector", symbol=symbol or "",
            start=None, end=None, days=365, current_user=current_user,
        )
    symbol = "000001.SZ" if symbol is None else symbol
    agg = _get_aggregator()
    try:
        import asyncio
        result = await asyncio.to_thread(
            agg.fetch,
            market=market.upper(),
            field="sector",
            symbol=symbol.upper(),
        )
    except Exception as exc:
        logger.warning("sector fetch failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"行业数据获取失败: {exc}")

    df = result.data
    records = df.replace({float("nan"): None}).to_dict("records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, (date, datetime)):
                rec[k] = v.isoformat()

    return {
        "success": True,
        "market": market.upper(),
        "source_used": result.source_used,
        "count": len(records),
        "data": records,
    }


# ---------------------------------------------------------------------------
# GET /meta — 股票基本信息（F10）
# ---------------------------------------------------------------------------
@router.get("/meta")
async def get_meta(
    market: str = Query("A", description="A / HK / US"),
    symbol: str = Query(..., description="股票代码"),
    current_user: dict = Depends(get_current_user),
):
    """股票基本信息 — A 股从 QuantDB stock_list 获取。"""
    if _registered_provider(market):
        return await get_field_data(
            market=market, field="stock_list", symbol=symbol,
            start=None, end=None, days=365, current_user=current_user,
        )
    agg = _get_aggregator()
    try:
        import asyncio
        result = await asyncio.to_thread(
            agg.fetch,
            market=market.upper(),
            field="stock_list",
            symbol=symbol.upper(),
        )
    except Exception as exc:
        logger.warning("meta/stock_list fetch failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"基本信息获取失败: {exc}")

    df = result.data
    records = df.replace({float("nan"): None}).to_dict("records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, (date, datetime)):
                rec[k] = v.isoformat()

    return {
        "success": True,
        "market": market.upper(),
        "symbol": symbol.upper(),
        "source_used": result.source_used,
        "data": records,
    }


# ---------------------------------------------------------------------------
# GET /search — 股票搜索（复用 stocks_index）
# ---------------------------------------------------------------------------
@router.get("/search")
async def search_stocks(
    keyword: str = Query(..., description="搜索关键词"),
    market: str | None = Query(None, description="A / HK / US，不指定则全市场"),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    if market and _registered_provider(market):
        import asyncio
        from backend.shared.stock_utils import StockCodeUtil

        provider = _registered_provider(market)
        try:
            frame = await asyncio.to_thread(provider.open_raw().fetch_stock_list)
            keyword_key = keyword.strip().lower()
            results = []
            for row in frame.to_dict("records"):
                code = StockCodeUtil.to_prefix(row["symbol"], market=market.upper())
                name = str(row.get("stock_name") or row.get("name_en") or code)
                if any(keyword_key in value.lower() for value in (code, str(row["symbol"]), name)):
                    results.append({
                        "symbol": code, "code": code,
                        "name": name, "market": market.upper(),
                    })
                    if len(results) >= limit:
                        break
            return {
                "success": True, "keyword": keyword, "market": market.upper(),
                "results": results, "count": len(results),
            }
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"注册市场搜索失败: {exc}") from exc
    # HK/US: stocks_index 只有 A 股，直接走本地 security_master（中文名）
    if market and market.upper() in ("HK", "US"):
        try:
            from backend.scripts.market_cn_names import _read_security_master

            mapping = _read_security_master(market.upper())
            if mapping:
                k = keyword.strip().lower()
                results = []
                for sym, name in mapping.items():
                    if k in sym.lower() or k in name.lower():
                        results.append({
                            "symbol": sym,
                            "code": sym,
                            "name": name,
                            "market": market.upper(),
                        })
                        if len(results) >= limit:
                            break
                if results:
                    return {
                        "success": True,
                        "keyword": keyword,
                        "market": market,
                        "results": results,
                        "count": len(results),
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning("security_master search failed: %s", exc)

    # 优先使用 stocks_search 的 JSON 索引
    try:
        from backend.services.api.routers.stocks_search import stock_index_store
        results = stock_index_store.search(keyword=keyword, limit=limit)
        # 按 market 过滤
        if market:
            m = market.upper()
            if m == "A":
                results = [r for r in results if r.get("market") in ("SH", "SZ", "BJ")]
            elif m == "HK":
                results = [r for r in results if r.get("market") == "HK"]
            elif m == "US":
                results = [r for r in results if r.get("market") == "US"]
        return {
            "success": True,
            "keyword": keyword,
            "market": market,
            "results": results,
            "count": len(results),
        }
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("stock index search failed: %s", exc)

    # 回退：从 data_platform 的 fetch_meta 搜索
    try:
        agg = _get_aggregator()
        from backend.services.engine.data_platform.registry import get_registry
        reg = get_registry()
        # 用 baostock(A) / yahoo_finance(HK/US) 的 fetch_meta
        m = (market or "A").upper()
        if m == "A":
            adapter = reg.get("baostock")
        elif m == "HK":
            adapter = reg.get("yahoo_finance")
        else:
            adapter = reg.get("yahoo_finance")

        meta_df = adapter.fetch_meta(m)
        k = keyword.strip().lower()
        mask = (
            meta_df["symbol"].str.lower().str.contains(k, na=False)
            | meta_df["name"].str.lower().str.contains(k, na=False)
        )
        matched = meta_df[mask].head(limit)
        results = []
        for _, r in matched.iterrows():
            results.append({
                "symbol": r.get("symbol", ""),
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "market": r.get("exchange", m),
            })
        return {
            "success": True,
            "keyword": keyword,
            "market": market,
            "results": results,
            "count": len(results),
        }
    except Exception as exc:
        logger.error("search fallback failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"搜索失败: {exc}")
