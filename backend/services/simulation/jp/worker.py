"""Settle already-persisted daily JP orders after a raw EOD bar is published."""

import asyncio
import logging
from datetime import date

from sqlalchemy import select

from backend.services.simulation.models.jp import JPSimulationSession
from backend.shared.database_manager_v2 import get_session
from . import service

logger = logging.getLogger(__name__)


async def settle_daily_accounts():
    try:
        data = await asyncio.to_thread(service.execution_data)
        latest = data.latest_price_date()
    except (ValueError, OSError):
        return 0  # JP data is optional; existing markets keep running.
    async with get_session(read_only=True) as db:
        rows = await db.execute(
            select(JPSimulationSession).where(JPSimulationSession.mode == "daily")
        )
        ready = []
        for session in rows.scalars():
            cursor = (
                date.fromisoformat(session.state["cursor"])
                if session.state["cursor"]
                else session.anchor_date
            )
            if data.calendar.next_session(cursor) <= latest:
                ready.append(
                    (
                        session.session_id,
                        session.user_id,
                        session.tenant_id,
                        session.revision,
                    )
                )
    settled = 0
    for sid, uid, tenant, revision in ready:
        async with get_session(read_only=False) as db:
            try:
                await service.advance_session(
                    db, sid, uid, tenant, expected_revision=revision
                )
                settled += 1
            except ValueError as exc:
                await db.rollback()
                logger.debug(
                    "JP daily account %s awaits data/rules or changed: %s", sid, exc
                )
    return settled


async def run_daily_worker():
    while True:
        try:
            await settle_daily_accounts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("JP daily settlement worker failed")
        await asyncio.sleep(60)
