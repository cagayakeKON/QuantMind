"""Authenticated, isolated JP replay and daily cash accounts."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.simulation.models.jp import JPSimulationSession
from backend.services.trade_shared.deps import AuthContext, get_auth_context, get_db
from . import service
from .model_orders import model_plan
from .rules import RuleDataMissing

router = APIRouter(prefix="/api/v1/simulation/jp", tags=["JP-Cash-Simulation"])


class CreateRequest(BaseModel):
    mode: Literal["replay", "daily"] = "replay"
    name: str = Field(default="日股模拟账户", min_length=1, max_length=128)
    initial_cash: Decimal = Field(default=Decimal(1000000), gt=0, le=Decimal("1e12"))
    start_date: date | None = None
    end_date: date | None = None
    commission_rate: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    slippage_bps: Decimal = Field(default=Decimal(5), ge=0, lt=10000)


class OrderRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=96)
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, le=1000000000, strict=True)


class QueueRequest(BaseModel):
    revision: int = Field(ge=0)
    orders: list[OrderRequest] = Field(min_length=1, max_length=200)


class StepRequest(BaseModel):
    revision: int = Field(ge=0)


class ModelPlanRequest(BaseModel):
    revision: int = Field(ge=0)
    model_id: str = Field(min_length=1, max_length=200)
    topk: int = Field(default=5, ge=1, le=200)
    exposure: Decimal = Field(default=Decimal("0.95"), ge=0, le=1)
    min_score: float = Field(default=0, allow_inf_nan=False)


class ModelOrdersRequest(ModelPlanRequest):
    plan_sha256: str = Field(pattern="^[a-f0-9]{64}$")


def failure(exc):
    if isinstance(exc, LookupError):
        return HTTPException(404, detail=str(exc))
    if isinstance(exc, RuleDataMissing):
        return HTTPException(409, detail=str(exc))
    return HTTPException(400, detail=str(exc))


@router.get("/readiness")
def readiness(auth: AuthContext = Depends(get_auth_context)):
    try:
        data = service.execution_data()
        return {
            "market": "JP",
            "currency": "JPY",
            "latest_date": str(data.latest_price_date()),
            "data_version": data.hub.data_dir.name,
            "historical_units_configured": bool(data.units),
            "default_unit_from": "2018-10-01",
            "real_trading": False,
            "execution": "next_open_daily",
        }
    except (ValueError, OSError) as exc:
        raise failure(exc) from exc


@router.get("/sessions")
async def list_sessions(
    auth: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)
):
    rows = await db.execute(
        select(JPSimulationSession)
        .where(
            JPSimulationSession.user_id == str(auth.user_id),
            JPSimulationSession.tenant_id == auth.tenant_id,
        )
        .order_by(JPSimulationSession.created_at.desc())
        .limit(100)
    )
    return [service.view(row) for row in rows.scalars()]


@router.post("/sessions")
async def create(
    request: CreateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.create_session(
            db, auth.user_id, auth.tenant_id, **request.model_dump()
        )
    except (ValueError, OSError) as exc:
        await db.rollback()
        raise failure(exc) from exc


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return service.view(
            await service.load_session(db, session_id, auth.user_id, auth.tenant_id)
        )
    except LookupError as exc:
        raise failure(exc) from exc


@router.post("/sessions/{session_id}/orders")
async def queue(
    session_id: uuid.UUID,
    request: QueueRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.queue_orders(
            db,
            session_id,
            auth.user_id,
            auth.tenant_id,
            [o.model_dump() for o in request.orders],
            expected_revision=request.revision,
        )
    except (ValueError, LookupError, OSError) as exc:
        await db.rollback()
        raise failure(exc) from exc


@router.post("/sessions/{session_id}/step")
async def step(
    session_id: uuid.UUID,
    request: StepRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.advance_session(
            db,
            session_id,
            auth.user_id,
            auth.tenant_id,
            expected_revision=request.revision,
        )
    except (ValueError, LookupError, OSError) as exc:
        await db.rollback()
        raise failure(exc) from exc


@router.post("/sessions/{session_id}/model-plan")
async def preview_model(
    session_id: uuid.UUID,
    request: ModelPlanRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await model_plan(
            db, session_id, auth.user_id, auth.tenant_id, **request.model_dump()
        )
    except (ValueError, LookupError, OSError) as exc:
        await db.rollback()
        raise failure(exc) from exc


@router.post("/sessions/{session_id}/model-orders")
async def save_model_orders(
    session_id: uuid.UUID,
    request: ModelOrdersRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await model_plan(
            db, session_id, auth.user_id, auth.tenant_id, **request.model_dump()
        )
    except (ValueError, LookupError, OSError) as exc:
        await db.rollback()
        raise failure(exc) from exc
