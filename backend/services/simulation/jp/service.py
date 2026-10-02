"""Local-first JP sessions. Ongoing orders must precede the actual opening.

Replay pins an immutable publication. Daily sessions use the latest publication
but consume only orders already saved in PostgreSQL; step cannot supply orders.
"""

from __future__ import annotations

import os
import uuid
import asyncio
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.models.jp import JPSimulationSession
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.utc_datetime import utc_now
from .account import JPCashAccount
from .data import JPExecutionData
from .rules import RuleDataMissing, opening_utc


def execution_data(version: str | None = None) -> JPExecutionData:
    hub = QuantJPDataHub()
    root = hub._publication_root.resolve()
    if version:
        pinned = (root / "versions" / version).resolve()
        if (
            not pinned.is_relative_to(root / "versions")
            or not (pinned / "manifest.json").is_file()
        ):
            raise RuleDataMissing("Pinned JP data version is unavailable")
        hub = QuantJPDataHub(pinned)
    else:
        # Pin this operation too, so an atomic publication cannot mix versions
        # between master, previous-close and current raw-bar reads.
        hub = QuantJPDataHub(hub.data_dir)
    return JPExecutionData(hub, os.getenv("QM_JP_TRADING_UNITS_FILE"))


def check_daily_submission(
    signal_day: date, execution_day: date, latest_bar: date, now: datetime
):
    if now.tzinfo is None:
        raise ValueError("JP submission clock must be timezone aware")
    if opening_utc(execution_day) <= now or latest_bar >= execution_day:
        raise ValueError(
            "Daily JP orders must be persisted before the next session opens"
        )
    if signal_day > now.date():
        raise ValueError("JP signal date cannot be in the future")


async def load_session(
    db: AsyncSession, session_id, user_id: str, tenant_id: str, *, lock=False
):
    query = select(JPSimulationSession).where(
        JPSimulationSession.session_id == session_id,
        JPSimulationSession.user_id == str(user_id),
        JPSimulationSession.tenant_id == tenant_id,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    session = result.scalar_one_or_none()
    if session is None:
        raise LookupError("JP account not found")
    return session


def view(session: JPSimulationSession) -> dict:
    state = session.state
    return {
        "session_id": str(session.session_id),
        "name": session.name,
        "mode": session.mode,
        "market": "JP",
        "currency": "JPY",
        "anchor_date": str(session.anchor_date),
        "end_date": str(session.end_date) if session.end_date else None,
        "data_version": session.data_version,
        "revision": session.revision,
        "state": state,
        "pending": session.pending,
    }


async def create_session(
    db: AsyncSession,
    user_id: str,
    tenant_id: str,
    *,
    mode: str,
    name: str,
    initial_cash,
    start_date: date | None,
    end_date: date | None,
    commission_rate,
    slippage_bps,
):
    data = await asyncio.to_thread(execution_data)
    version = data.hub.data_dir.name
    if mode == "daily":
        anchor = data.latest_price_date()
    elif mode == "replay" and start_date:
        index = data.calendar.sessions.index(start_date)
        if not index:
            raise RuleDataMissing("JP replay requires a prior signal session")
        anchor = data.calendar.sessions[index - 1]
        if end_date and end_date < start_date:
            raise ValueError("end_date must not precede start_date")
    else:
        raise ValueError("JP mode must be daily, or replay with start_date")
    account = JPCashAccount.create(
        data.calendar,
        initial_cash,
        commission_rate=commission_rate,
        slippage_bps=slippage_bps,
    )
    account.state["next_date"] = str(data.calendar.next_session(anchor))
    session = JPSimulationSession(
        session_id=uuid.uuid4(),
        user_id=str(user_id),
        tenant_id=tenant_id,
        name=name,
        mode=mode,
        anchor_date=anchor,
        end_date=end_date,
        data_version=version,
        state=account.state,
        pending=[],
        revision=0,
    )
    db.add(session)
    await db.commit()
    return view(session)


async def queue_orders(
    db: AsyncSession,
    session_id,
    user_id,
    tenant_id,
    requests: list[dict],
    *,
    expected_revision: int,
    _data: JPExecutionData | None = None,
):
    session = await load_session(db, session_id, user_id, tenant_id, lock=True)
    if session.revision != expected_revision:
        raise ValueError("JP account changed; refresh before submitting")
    data = _data or await asyncio.to_thread(
        execution_data, session.data_version if session.mode == "replay" else None
    )
    signal_day = (
        date.fromisoformat(session.state["cursor"])
        if session.state["cursor"]
        else session.anchor_date
    )
    execute_day = data.calendar.next_session(signal_day)
    if session.end_date and execute_day > session.end_date:
        raise ValueError("JP replay is finished")
    if session.mode == "daily":
        check_daily_submission(
            signal_day, execute_day, data.latest_price_date(), utc_now()
        )
    ids = {o["order_id"] for o in session.pending + session.state["orders"]}
    pending = list(session.pending)
    for request in requests:
        request = {
            **request,
            "symbol": StockCodeUtil.to_prefix(request["symbol"], market="JP"),
        }
        identifier = request["order_id"]
        if identifier in ids:
            # Network retries are idempotent only for the identical order.
            previous = next(
                o
                for o in pending + session.state["orders"]
                if o["order_id"] == identifier
            )
            if all(
                previous.get(k) == request.get(k)
                for k in ("symbol", "side", "quantity")
            ):
                continue
            raise ValueError("Conflicting JP order ID")
        ids.add(identifier)
        pending.append(
            {
                **request,
                "symbol": StockCodeUtil.to_prefix(request["symbol"], market="JP"),
                "signal_date": str(signal_day),
                "execution_date": str(execute_day),
                "submitted_at": utc_now().isoformat().replace("+00:00", "Z"),
                "order_type": "MARKET",
            }
        )
    session.pending = pending
    session.revision += 1
    await db.commit()
    return view(session)


async def advance_session(
    db: AsyncSession, session_id, user_id, tenant_id, *, expected_revision: int
):
    session = await load_session(db, session_id, user_id, tenant_id, lock=True)
    if session.revision != expected_revision:
        raise ValueError("JP account changed; refresh before stepping")
    data = await asyncio.to_thread(
        execution_data, session.data_version if session.mode == "replay" else None
    )
    signal_day = (
        date.fromisoformat(session.state["cursor"])
        if session.state["cursor"]
        else session.anchor_date
    )
    day = data.calendar.next_session(signal_day)
    if session.end_date and day > session.end_date:
        raise ValueError("JP replay is finished")
    if day > data.latest_price_date():
        raise RuleDataMissing("Next JP session has no published EOD data yet")
    if session.mode == "daily" and opening_utc(day) > utc_now():
        raise ValueError("JP daily session is still in the future")
    account = JPCashAccount(data.calendar, session.state)
    symbols = sorted(
        set(account.state["positions"]) | {o["symbol"] for o in session.pending}
    )
    bars, master = await asyncio.to_thread(
        data.day, day, symbols, list(account.state["positions"])
    )
    if session.mode == "daily":
        for order in session.pending:
            submitted = datetime.fromisoformat(
                order["submitted_at"].replace("Z", "+00:00")
            )
            if (
                submitted.tzinfo is None
                or submitted >= opening_utc(day)
                or order["execution_date"] != str(day)
            ):
                raise ValueError("Invalid persisted JP daily submission deadline")
    # The ledger and all queued orders commit together. A missing action/unit
    # aborts before this state assignment, preserving the pending requests.
    result = account.step(day, bars, master, session.pending)
    account.state["next_date"] = str(data.calendar.next_session(day))
    session.state = account.state
    session.pending = []
    session.data_version = data.hub.data_dir.name
    session.revision += 1
    await db.commit()
    return {**view(session), "result": result}
