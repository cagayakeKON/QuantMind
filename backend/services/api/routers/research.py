"""投研平台聚合接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import backend.services.api.routers.research_service as _research_service
from backend.services.api.routers.research_schemas import (
    BatchFeaturesRequest,
    PoolAddRequest,
    SingleStockPredictionRequest,
    SymbolsFeaturesRequest,
    WatchlistAddRequest,
)
from backend.services.api.routers.research_features_service import (
    get_batch_full_features as get_batch_full_features_service,
)
from backend.services.api.routers.research_service import (
    add_to_research_pool as add_to_research_pool_service,
    add_to_watchlist as add_to_watchlist_service,
    get_available_models as get_available_models_service,
    get_inference_runs as get_inference_runs_service,
    get_research_overview as get_research_overview_service,
    get_research_universe as get_research_universe_service,
    get_research_universe_by_date as get_research_universe_by_date_service,
    get_stock_kline as get_stock_kline_service,
    get_symbols_features as get_symbols_features_service,
    get_user_research_pool as get_user_research_pool_service,
    get_user_watchlist as get_user_watchlist_service,
    predict_single_stock as predict_single_stock_service,
    remove_from_research_pool as remove_from_research_pool_service,
    remove_from_watchlist as remove_from_watchlist_service,
    sync_watchlist_positions_service,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

router = APIRouter(prefix="/api/v1/research", tags=["Research"])

# 向后兼容：保留测试与历史调用使用的私有符号
_format_candidate_record = _research_service._format_candidate_record  # noqa: SLF001


async def _do_get_overview(  # noqa: SLF001
    tid: str, uid: str, model_id: str | None, run_id: str | None, limit: int, offset: int
):
    original_get_session = _research_service.get_session
    _research_service.get_session = get_session
    try:
        return await _research_service._do_get_overview(tid, uid, model_id, run_id, limit, offset)  # noqa: SLF001
    finally:
        _research_service.get_session = original_get_session


@router.get("/models")
async def get_available_models(
    market: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_available_models_service(tid, uid, market)


@router.get("/runs")
async def get_inference_runs(model_id: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_inference_runs_service(tid, uid, model_id)


@router.get("/overview")
async def get_research_overview(
    model_id: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
    market: str | None = Query(None),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    if market is not None:
        return await get_research_overview_service(tid, uid, model_id, run_id, limit, offset, market)
    return await get_research_overview_service(tid, uid, model_id, run_id, limit, offset)


@router.get("/universe")
async def get_research_universe(
    run_id: str | None = Query(None),
    model_id: str | None = Query(None),
    date: str | None = Query(None, description="数据日 T（pred.parquet 口径），与 model_id 搭配直读全市场分数"),
    limit: int = Query(2000),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    if model_id and date:
        return await get_research_universe_by_date_service(tid, uid, model_id, date, limit, offset)
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id 或 model_id+date 必填")
    return await get_research_universe_service(tid, uid, run_id, limit, offset)


@router.get("/watchlist")
async def get_user_watchlist(
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_user_watchlist_service(tid, uid, limit, offset)


@router.post("/watchlist/sync-positions")
async def sync_watchlist_positions(request: Request, current_user: dict = Depends(get_current_user)):
    """模拟盘持仓自动加入自选。放 {symbol} 路由之前，避免被当作 symbol 捕获。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    return await sync_watchlist_positions_service(
        str(current_user["tenant_id"]), str(current_user["user_id"]), auth
    )


@router.post("/watchlist/{symbol}")
async def add_to_watchlist(symbol: str, req: WatchlistAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await add_to_watchlist_service(tid, uid, symbol, req.run_id, req.stock_name, req.features_snapshot)


@router.delete("/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await remove_from_watchlist_service(tid, uid, symbol)


@router.get("/pool")
async def get_user_research_pool(
    status: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_user_research_pool_service(tid, uid, status, limit, offset)


@router.post("/pool/{symbol}")
async def add_to_research_pool(symbol: str, req: PoolAddRequest, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await add_to_research_pool_service(
        tid,
        uid,
        symbol,
        req.run_id,
        req.stock_name,
        req.model_id,
        req.fusion_score,
        req.thesis_summary,
        req.features_snapshot,
    )


@router.delete("/pool/{symbol}")
async def remove_from_research_pool(symbol: str, current_user: dict = Depends(get_current_user)):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await remove_from_research_pool_service(tid, uid, symbol)


@router.post("/symbols/features")
async def get_symbols_features(
    req: SymbolsFeaturesRequest,
    lite: bool = Query(False, description="保留兼容参数；CN 已不再读 stock_daily_latest"),
    current_user: dict = Depends(get_current_user),
):
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await get_symbols_features_service(tid, uid, req.symbols, lite)


@router.get("/kline/{symbol}")
async def get_stock_kline(symbol: str, days: int = Query(60), end_date: str | None = Query(None, description="K线截止日YYYY-MM-DD（含当日），缺省最新；指标口径按基准日截断防前视泄露"), start_date: str | None = Query(None, description="K线起始日YYYY-MM-DD（含当日）；传入后返回[起始日,截止日]全窗口，供图表展示基准日后实际走势验证预测"), current_user: dict = Depends(get_current_user)):
    _ = current_user
    return await get_stock_kline_service(symbol, days, end_date=end_date, start_date=start_date)


@router.post("/batch-features")
async def get_batch_features(
    req: BatchFeaturesRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量 QuantDB 特征投影：按 fields 返回指定字段（按需加载）。"""
    if req.market.upper() == "JP":
        if not req.model_id or not req.trade_date:
            raise HTTPException(422, "JP features require the selected model and input date")
        from backend.shared.stock_utils import StockCodeUtil

        tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
        rows = await _research_service.selected_prediction_rows(
            tid, uid, req.model_id, req.trade_date, req.run_id
        )
        if req.observed_predictions is not None:
            from backend.services.engine.inference.prediction_provenance import (
                prediction_source,
            )

            current = {row["symbol"]: row for row in rows}
            observed = {}
            for prediction in req.observed_predictions:
                prefix = StockCodeUtil.to_prefix(prediction.symbol, market="JP")
                if prefix in observed:
                    raise HTTPException(422, "Duplicate observed prediction")
                observed[prefix] = prediction
            for symbol in req.symbols:
                prefix = StockCodeUtil.to_prefix(symbol, market="JP")
                before = observed.get(prefix)
                if before is None:
                    raise HTTPException(
                        422, "Requested symbol lacks its observed prediction"
                    )
                now = current.get(prefix)
                try:
                    before_source = prediction_source(before.data_provenance)
                except ValueError as exc:
                    raise HTTPException(422, "Invalid observed prediction source") from exc
                if (
                    now is None
                    or before.score != now["score"]
                    or before_source != now.get("data_provenance")
                ):
                    raise HTTPException(
                        409,
                        {
                            "code": "PREDICTION_SOURCE_CHANGED",
                            "message": "Prediction changed since the candidate list was read",
                        },
                    )
        sources = {row["symbol"]: row.get("data_provenance") for row in rows}
        groups, warnings = {}, {}
        for symbol in req.symbols:
            prefix = StockCodeUtil.to_prefix(symbol, market="JP")
            source = sources.get(prefix)
            if not source:
                warnings[prefix] = "Prediction has no recorded input publication"
                continue
            if req.data_version and req.data_version != source["data_version"]:
                raise HTTPException(409, "Requested publication differs from the prediction source")
            groups.setdefault(source["data_version"], []).append(prefix)
        items = []
        for version, symbols in groups.items():
            payload = await get_batch_full_features_service(
                symbols, req.fields, req.trade_date, "JP", version
            )
            for item in payload["data"]["items"]:
                prefix = StockCodeUtil.to_prefix(item["symbol"], market="JP")
                item.update(dataVersion=version, dataProvenance=sources[prefix])
                items.append(item)
        return {"code": 200, "data": {"items": items, "sourceWarnings": warnings}}
    return await get_batch_full_features_service(req.symbols, req.fields, req.trade_date)


@router.post("/predict-stock")
async def predict_single_stock(
    req: SingleStockPredictionRequest,
    current_user: dict = Depends(get_current_user),
):
    """个股未来预测与 10%-50%-90% 置信区间分位数推理。"""
    tid, uid = str(current_user["tenant_id"]), str(current_user["user_id"])
    return await predict_single_stock_service(
        tid,
        uid,
        symbol=req.symbol,
        model_id=req.model_id,
        target_date=req.date,
        horizon=req.horizon,
        market=req.market,
        consensus_model_ids=req.consensus_model_ids,
        execute=req.execute,
    )
