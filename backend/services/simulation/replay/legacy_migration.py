"""One-time replay imports and durable receipts; no account/cache writes."""

import hashlib
import json

from sqlalchemy import select

from backend.services.simulation.models.replay_import import ReplayImportReceipt

from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayTrade,
)


def _receipt_values(plan):
    values = plan["session"]
    marker = values["signal_progress"]["legacy_import"]
    digest = hashlib.sha256(
        json.dumps(
            marker["source"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    if digest != marker["source_sha256"]:
        raise ValueError("Replay import source digest does not match its record")
    return {
        "session_id": values["session_id"],
        "tenant_id": values["tenant_id"],
        "user_id": values["user_id"],
        "market": values["strategy_params"]["market"],
        "data_version": values["strategy_params"]["data_version"],
        "source_format": marker["format"],
        "source_sha256": digest,
    }


async def _record_receipt(db, existing, values):
    # Called only after every target history row has been verified or flushed.
    # Its commit/rollback is owned by the exact same outer import transaction.
    if existing is None:
        db.add(ReplayImportReceipt(**values))
        await db.flush()


async def stage_replay_import(db, plan):
    """Caller owns the source lock, backup verification and outer transaction.

    A rerun validates previously imported history without rewinding a session
    that has since continued in the shared replay flow.
    """
    values = plan["session"]
    session_id = values["session_id"]
    receipt_values = _receipt_values(plan)
    receipt = await db.get(
        ReplayImportReceipt, session_id, with_for_update={"nowait": True}
    )
    if receipt is not None and any(
        getattr(receipt, key) != value for key, value in receipt_values.items()
    ):
        raise ValueError("Existing replay import receipt differs from the source")
    row = (
        await db.execute(
            select(ReplaySession)
            .where(ReplaySession.session_id == session_id)
            .with_for_update(nowait=True)
        )
    ).scalar_one_or_none()
    groups = (
        (ReplayOrder, "orders", "order_id"),
        (ReplayTrade, "trades", "trade_id"),
        (ReplayEquitySnapshot, "snapshots", "trade_date"),
    )
    if row is not None:
        for key in (
            "tenant_id",
            "user_id",
            "name",
            "model_id",
            "strategy_params",
            "initial_cash",
            "start_date",
            "end_date",
            "auto_trade",
        ):
            if getattr(row, key) != values[key]:
                raise ValueError("Existing replay session differs from the import")
        marker = (row.signal_progress or {}).get("legacy_import")
        if marker != values["signal_progress"]["legacy_import"]:
            raise ValueError("Existing replay session has another migration source")
        if row.sessions_done < values["sessions_done"] or (
            values["cursor_date"] is not None
            and (row.cursor_date is None or row.cursor_date < values["cursor_date"])
        ):
            raise ValueError("Existing replay session lost its imported cursor")
        for model, group, identity in groups:
            for expected in plan[group]:
                existing = (
                    await db.execute(
                        select(model).where(
                            model.session_id == session_id,
                            getattr(model, identity) == expected[identity],
                        )
                    )
                ).scalar_one_or_none()
                if existing is None or any(
                    getattr(existing, key) != value for key, value in expected.items()
                ):
                    raise ValueError("Existing replay history differs from the import")
        await _record_receipt(db, receipt, receipt_values)
        return False
    if receipt is not None:
        raise ValueError(
            "Imported replay session was discarded; refusing to recreate it"
        )
    db.add(ReplaySession(**values))
    await db.flush()
    for model, group, _ in groups:
        db.add_all(model(**values) for values in plan[group])
        await db.flush()
    await _record_receipt(db, receipt, receipt_values)
    return True
