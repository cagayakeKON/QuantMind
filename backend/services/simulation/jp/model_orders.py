"""Preview and persist model orders through the existing JP account service."""

import asyncio
import hashlib
import json
from datetime import date

from backend.shared.utc_datetime import utc_now
from . import service
from .account import money
from .model_portfolio import portfolio_orders
from .model_signals import (
    labels_available_on,
    prediction_path,
    read_test_scores,
    resolve_model,
)
from .rules import RuleDataMissing


def _plan(session, data, directory, meta, model_id, topk, exposure, min_score):
    signal = (
        date.fromisoformat(session.state["cursor"])
        if session.state["cursor"]
        else session.anchor_date
    )
    execute = data.calendar.next_session(signal)
    if session.end_date and execute > session.end_date:
        raise ValueError("JP replay is finished")
    if session.pending:
        raise ValueError("Execute saved JP orders before creating a model plan")
    if session.mode == "daily":
        service.check_daily_submission(
            signal, execute, data.latest_price_date(), utc_now()
        )
    known = labels_available_on(meta, data.calendar, signal)
    scores, digest = read_test_scores(prediction_path(directory), signal, signal)
    if not scores.get(signal):
        raise RuleDataMissing(f"Exact JP test-split signals are missing on {signal}")
    held = list(session.state["positions"])
    symbols = sorted({row["symbol"] for row in scores[signal]} | set(held))
    bars, master = data.day(signal, symbols, held)
    orders = portfolio_orders(
        session.state,
        scores[signal],
        bars,
        master,
        signal,
        execute,
        topk=topk,
        exposure=money(exposure),
        min_score=min_score,
    )
    model_key = hashlib.sha256(model_id.encode()).hexdigest()[:16]
    for order in orders:
        order["order_id"] = (
            f"model:{model_key}:{signal}:{order['symbol']}:{order['side']}"
        )
        order["model_id"] = model_id
        order["prediction_sha256"] = digest
        order["data_version"] = data.hub.data_dir.name
    plan = {
        "session_id": str(session.session_id),
        "revision": session.revision,
        "model_id": model_id,
        "signal_date": str(signal),
        "execution_date": str(execute),
        "data_version": data.hub.data_dir.name,
        "training_data_version": meta["jp_data_version"],
        "prediction_sha256": digest,
        "training_labels_available_on": str(known),
        "topk": topk,
        "exposure": str(money(exposure)),
        "min_score": min_score,
        "orders": orders,
    }
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


async def model_plan(
    db,
    session_id,
    user_id,
    tenant_id,
    *,
    model_id,
    revision,
    topk,
    exposure,
    min_score,
    plan_sha256=None,
):
    session = await service.load_session(
        db, session_id, user_id, tenant_id, lock=plan_sha256 is not None
    )
    if session.revision != revision:
        raise ValueError("JP account changed; refresh before creating a model plan")
    directory, meta = await resolve_model(tenant_id, user_id, model_id)
    data = await asyncio.to_thread(
        service.execution_data,
        session.data_version if session.mode == "replay" else None,
    )
    plan = await asyncio.to_thread(
        _plan, session, data, directory, meta, model_id, topk, exposure, min_score
    )
    if plan_sha256 is None:
        return plan
    if plan["plan_sha256"] != plan_sha256:
        raise ValueError("JP predictions or data changed; preview the model plan again")
    if not plan["orders"]:
        return service.view(session)
    return await service.queue_orders(
        db,
        session_id,
        user_id,
        tenant_id,
        plan["orders"],
        expected_revision=revision,
        _data=data,
    )
