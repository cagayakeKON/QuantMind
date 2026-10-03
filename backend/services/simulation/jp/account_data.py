"""Japanese publication/date inputs and read-only legacy-history guard."""

from datetime import date
from uuid import UUID

from sqlalchemy import text

from backend.services.simulation.models.replay import ReplaySession
from backend.services.simulation.jp.replay_migration import source_digest
from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.services.account_context import (
    SimulationAccountContext,
    SimulationAccountInputAdapter,
)
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)


def prepare_account_inputs(params):
    version = params.get("data_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("JP account requires a pinned execution publication")
    reader = open_market_execution_data("JP", data_version=version)
    rules = open_registered_replay_cash_rules({**params, "market": "JP"}, reader=reader)
    return SimulationAccountContext("JP", params["trade_date"], rules)


async def legacy_history_exists(db, *, tenant_id, user_ids):
    # Source rows are retained as an archive after the one-time import. No
    # legacy execution endpoint/worker may write them after the common switch.
    sources = (
        await db.execute(
            text(
                "SELECT to_jsonb(s) FROM jp_simulation_sessions s "
                "WHERE tenant_id = :tenant_id "
                "AND user_id = ANY(CAST(:user_ids AS TEXT[]))"
            ),
            {"tenant_id": tenant_id, "user_ids": list(user_ids)},
        )
    ).scalars()
    for source in sources:
        state = source.get("state")
        if (
            source.get("mode") != "replay"
            or not str(source["user_id"]).isdigit()
            or not isinstance(state, dict)
            or state.get("market") != "JP"
            or state.get("currency") != "JPY"
            or type(state.get("schema_version")) is not int
            or state.get("schema_version") != 1
            or not isinstance(state.get("daily"), list)
        ):
            return True
        target = await db.get(ReplaySession, UUID(source["session_id"]))
        if target is None:
            return True
        marker = (target.signal_progress or {}).get("legacy_import")
        cursor = state.get("cursor")
        try:
            cursor = date.fromisoformat(cursor) if cursor is not None else None
        except (TypeError, ValueError):
            return True
        params = target.strategy_params or {}
        if (
            not isinstance(marker, dict)
            or marker.get("format") != "jp_simulation_sessions_v1"
            or marker.get("source") != source
            or marker.get("source_sha256") != source_digest(source)
            or target.tenant_id != source["tenant_id"]
            or target.user_id != int(source["user_id"])
            or params.get("market") != "JP"
            or params.get("data_version") != source["data_version"]
            or target.sessions_done < len(state.get("daily", []))
            or (
                cursor is not None
                and (target.cursor_date is None or target.cursor_date < cursor)
            )
        ):
            return True
    return False


def open_account_input_adapter():
    return SimulationAccountInputAdapter(
        "JP", prepare_account_inputs, legacy_history_exists
    )
