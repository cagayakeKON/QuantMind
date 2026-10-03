"""One-time imports into the existing replay tables; no account/cache writes."""

from sqlalchemy import select

from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayTrade,
)


async def stage_replay_import(db, plan):
    """Caller owns the source lock, backup verification and outer transaction.

    A rerun validates previously imported history without rewinding a session
    that has since continued in the shared replay flow.
    """
    values = plan["session"]
    session_id = values["session_id"]
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
        return False
    db.add(ReplaySession(**values))
    await db.flush()
    for model, group, _ in groups:
        db.add_all(model(**values) for values in plan[group])
        await db.flush()
    return True
