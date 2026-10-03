"""Admin view: automatic trade history and upcoming planned executions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.live_trading.services.admin_order_view import (
    classify_auto_source,
    display_remarks,
    enum_value,
    isoformat_dt,
    planned_dedup_key,
)
from backend.services.live_trading.services.risk_trigger_service import (
    iter_active_strategy_payloads,
)
from backend.services.simulation.models.order import SimOrder
from backend.services.simulation.models.rebalance_job import SimulationRebalanceJob
from backend.services.simulation.services.simulation_hosted_scheduler import (
    _next_scheduled_trigger,
    _normalize_live_trade_config,
    _parse_started_at,
)
from backend.services.simulation.services.market_schedule import (
    ScheduleDataUnavailable,
    hosted_schedule_market,
    open_registered_schedule_context,
)
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.redis_client import redis_client
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text

router = APIRouter(dependencies=[Depends(require_admin)])
_SH_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def _ensure_redis():
    if redis_client.client is None:
        redis_client.connect()
    return redis_client


def _ok(data, message: str = "success"):
    return {"success": True, "code": 200, "message": message, "data": data}


def _history_item(
    *,
    row_id: str,
    mode: str,
    source: str,
    tenant_id: str,
    user_id: str,
    strategy_id: Any,
    symbol: str,
    side: str,
    quantity: float,
    price: float | None,
    average_price: float | None,
    status: str,
    created_at: datetime | None,
    filled_at: datetime | None,
    remarks: str | None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "mode": mode,
        "source": source,
        "tenant_id": tenant_id,
        "user_id": str(user_id),
        "strategy_id": str(strategy_id) if strategy_id not in (None, "") else None,
        "symbol": symbol,
        "side": side.upper(),
        "quantity": quantity,
        "price": price,
        "average_price": average_price,
        "status": status,
        "created_at": isoformat_dt(created_at),
        "filled_at": isoformat_dt(filled_at),
        "remarks": display_remarks(source, remarks),
    }


@router.get("/orders/history")
async def list_auto_order_history(
    mode: str | None = Query(None, description="SIMULATION / REAL"),
    source: str | None = Query(None, description="hosted / risk"),
    user_id: str | None = Query(None),
    symbol: str | None = Query(None),
    limit: int = Query(80, ge=1, le=300),
    current_user: dict = Depends(require_admin),
):
    mode_filter = str(mode or "").strip().upper() or None
    source_filter = str(source or "").strip().lower() or None
    items: list[dict[str, Any]] = []

    async with get_session(read_only=True) as db:
        if mode_filter in (None, "SIMULATION"):
            stmt = select(SimOrder).order_by(SimOrder.created_at.desc()).limit(limit * 2)
            if user_id:
                if str(user_id).isdigit():
                    stmt = stmt.where(SimOrder.user_id == int(user_id))
            if symbol:
                stmt = stmt.where(SimOrder.symbol == symbol)
            rows = list((await db.execute(stmt)).scalars().all())
            for row in rows:
                tagged = classify_auto_source(row.strategy_id, row.remarks)
                if tagged is None:
                    continue
                if source_filter and tagged != source_filter:
                    continue
                items.append(
                    _history_item(
                        row_id=f"sim:{row.id}",
                        mode="SIMULATION",
                        source=tagged,
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        strategy_id=row.strategy_id,
                        symbol=row.symbol,
                        side=enum_value(row.side),
                        quantity=float(row.quantity or 0),
                        price=row.price,
                        average_price=row.average_price,
                        status=enum_value(row.status),
                        created_at=row.created_at,
                        filled_at=row.filled_at,
                        remarks=row.remarks,
                    )
                )

        if mode_filter in (None, "REAL"):
            stmt = select(Order).order_by(Order.created_at.desc()).limit(limit * 2)
            if user_id:
                stmt = stmt.where(Order.user_id == str(user_id))
            if symbol:
                stmt = stmt.where(Order.symbol == symbol)
            rows = list((await db.execute(stmt)).scalars().all())
            for row in rows:
                tagged = classify_auto_source(row.strategy_id, row.remarks)
                if tagged is None:
                    continue
                if source_filter and tagged != source_filter:
                    continue
                items.append(
                    _history_item(
                        row_id=f"real:{row.id}",
                        mode=enum_value(row.trading_mode).upper() or "REAL",
                        source=tagged,
                        tenant_id=row.tenant_id,
                        user_id=row.user_id,
                        strategy_id=row.strategy_id,
                        symbol=row.symbol,
                        side=enum_value(row.side),
                        quantity=float(row.quantity or 0),
                        price=row.price,
                        average_price=row.average_price,
                        status=enum_value(row.status),
                        created_at=row.created_at,
                        filled_at=row.filled_at,
                        remarks=row.remarks,
                    )
                )

    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return _ok(items[:limit])


@router.get("/orders/planned")
async def list_planned_orders(
    current_user: dict = Depends(require_admin),
):
    now = datetime.now(_SH_TZ).replace(microsecond=0)
    planned: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with get_session(read_only=True) as db:
        job_stmt = (
            select(SimulationRebalanceJob)
            .where(SimulationRebalanceJob.status.in_(("pending", "ready", "running")))
            .order_by(SimulationRebalanceJob.planned_run_at.asc().nulls_last())
            .limit(200)
        )
        jobs = list((await db.execute(job_stmt)).scalars().all())
        for job in jobs:
            trade_date = ""
            if job.planned_run_at is not None:
                trade_date = job.planned_run_at.date().isoformat()
            key = planned_dedup_key(job.tenant_id, job.user_id, job.strategy_id, trade_date)
            seen.add(key)
            planned.append(
                {
                    "id": f"job:{job.job_id}",
                    "kind": "rebalance_job",
                    "mode": "SIMULATION",
                    "tenant_id": job.tenant_id,
                    "user_id": job.user_id,
                    "strategy_id": job.strategy_id,
                    "phase": None,
                    "status": job.status,
                    "trade_date": trade_date or None,
                    "planned_at": isoformat_dt(job.planned_run_at),
                    "window_end_at": isoformat_dt(job.window_end_at),
                    "title": "模拟盘托管调仓",
                    "detail": job.last_error,
                }
            )

        try:
            task_rows = (
                await db.execute(
                    text(
                        """
                        SELECT task_id, tenant_id, user_id, strategy_id, strategy_name,
                               trading_mode, status, stage, task_source, trigger_mode,
                               prediction_trade_date, created_at
                        FROM trade_manual_execution_tasks
                        WHERE status IN ('queued', 'running')
                          AND (
                            task_source = 'hosted_runner'
                            OR trigger_mode = 'hosted'
                            OR task_type = 'hosted'
                          )
                        ORDER BY created_at DESC
                        LIMIT 100
                        """
                    )
                )
            ).mappings().all()
        except Exception:
            task_rows = []
        for row in task_rows:
            trade_date = str(row.get("prediction_trade_date") or "")
            key = planned_dedup_key(
                str(row.get("tenant_id") or "default"),
                str(row.get("user_id") or ""),
                str(row.get("strategy_id") or ""),
                trade_date,
            )
            seen.add(key)
            planned.append(
                {
                    "id": f"task:{row.get('task_id')}",
                    "kind": "hosted_task",
                    "mode": str(row.get("trading_mode") or "REAL").upper(),
                    "tenant_id": row.get("tenant_id"),
                    "user_id": str(row.get("user_id") or ""),
                    "strategy_id": row.get("strategy_id"),
                    "phase": None,
                    "status": row.get("status"),
                    "trade_date": trade_date or None,
                    "planned_at": isoformat_dt(row.get("created_at")),
                    "window_end_at": None,
                    "title": row.get("strategy_name") or "托管执行任务",
                    "detail": f"{row.get('task_source')}/{row.get('stage')}",
                }
            )

    redis = _ensure_redis()
    for row in iter_active_strategy_payloads(redis):
        payload = row.get("payload") or {}
        live_cfg = _normalize_live_trade_config(payload.get("live_trade_config"))
        market = (
            hosted_schedule_market(payload)
            if str(row.get("mode") or "SIMULATION").upper() == "SIMULATION"
            else None
        )
        try:
            context = (
                await asyncio.to_thread(open_registered_schedule_context, market)
                if market
                else None
            )
            context_kwargs = {"context": context} if context else {}
            started_day = _parse_started_at(payload.get("started_at"), **context_kwargs)
            nxt = _next_scheduled_trigger(
                now=now,
                live_trade_config=live_cfg,
                started_day=started_day,
                **context_kwargs,
            )
        except ScheduleDataUnavailable as error:
            logger.warning("Registered simulation schedule unavailable: %s", error)
            continue
        if nxt is None:
            continue
        tenant_id = str(row.get("tenant_id") or "default")
        user_id = str(row.get("user_id") or "")
        strategy_id = str(payload.get("strategy_id") or "").strip()
        key = planned_dedup_key(tenant_id, user_id, strategy_id, nxt.trade_date)
        if key in seen:
            continue
        seen.add(key)
        planned.append(
            {
                "id": f"schedule:{tenant_id}:{user_id}:{strategy_id}:{nxt.trade_date}:{nxt.phase}",
                "kind": "schedule",
                "mode": str(row.get("mode") or "SIMULATION").upper(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "strategy_id": strategy_id or None,
                "phase": nxt.phase,
                "status": "scheduled",
                "trade_date": nxt.trade_date,
                "planned_at": isoformat_dt(
                    nxt.target_at if context else nxt.target_at.replace(tzinfo=None)
                ),
                "window_end_at": isoformat_dt(
                    nxt.window_end_at if context else nxt.window_end_at.replace(tzinfo=None)
                ),
                "title": "下次托管窗口",
                "detail": f"{nxt.phase} {live_cfg.get('schedule_type')}",
            }
        )

    planned.sort(key=lambda item: item.get("planned_at") or "9999", reverse=False)
    return _ok(planned)
