"""Queued DAY orders use the registered Japan close, preserving old markets."""

from datetime import date, datetime, timezone
import os

import pytest
from sqlalchemy import select

from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.schemas.order import SimOrderCreate
from backend.services.simulation.services.order_service import SimOrderService
from backend.tests.test_jp_standard_simulation_pg import storage as storage_fixture

storage = storage_fixture

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
        reason="UUID PostgreSQL and Redis opt-in",
    ),
]


async def queued(storage, symbol, day, **kwargs):
    service = SimOrderService(storage.db)
    order = await service.create_order(
        storage.tenant,
        "123",
        SimOrderCreate(
            symbol=symbol,
            side="buy",
            order_type="limit",
            quantity=100,
            price=100,
            **kwargs,
        ),
    )
    await service.queue_order(order, trading_session_date=day)
    row = (
        await storage.db.execute(
            select(SimulationOrderV2).where(
                SimulationOrderV2.order_id == order.order_id,
            )
        )
    ).scalar_one()
    await storage.db.refresh(row)
    return row


@pytest.mark.parametrize(
    "symbol,day,deadline",
    [
        ("JP72030", date(2024, 11, 1), datetime(2024, 11, 1, 6)),
        ("JP72030", date(2024, 11, 5), datetime(2024, 11, 5, 6, 30)),
        ("JP216A0", date(2026, 9, 29), datetime(2026, 9, 29, 6, 30)),
        ("SH600036", date(2026, 9, 29), datetime(2026, 9, 29, 7)),
        ("0700.HK", date(2026, 9, 29), datetime(2026, 9, 29, 8)),
        # This legacy alias still follows the original CN fallback.
        ("HK00700", date(2026, 9, 29), datetime(2026, 9, 29, 7)),
        ("AAPL", date(2026, 9, 29), datetime(2026, 9, 29, 20)),
    ],
)
async def test_actual_create_and_queue_preserve_day_close(
    storage, symbol, day, deadline
):
    row = await queued(storage, symbol, day)
    assert row.expires_at == deadline
    assert row.trading_session_date == day and row.status == "pending"


async def test_jp_explicit_expiry_and_gtc_keep_original_contract(storage):
    expiry = datetime(2026, 9, 30, 1, tzinfo=timezone.utc)
    explicit = await queued(
        storage,
        "JP72030",
        date(2026, 9, 29),
        expires_at=expiry,
    )
    assert explicit.expires_at == expiry.replace(tzinfo=None)
    gtc = await queued(storage, "JP72030", date(2026, 9, 29), time_in_force="GTC")
    assert gtc.expires_at is None and gtc.time_in_force == "GTC"
