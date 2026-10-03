"""Japanese publication/date inputs and read-only legacy-history guard."""

from sqlalchemy import select

from backend.services.simulation.models.jp import JPSimulationSession
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
    return (
        await db.scalar(
            select(JPSimulationSession.session_id)
            .where(
                JPSimulationSession.tenant_id == tenant_id,
                JPSimulationSession.user_id.in_(user_ids),
            )
            .limit(1)
        )
        is not None
    )


def open_account_input_adapter():
    return SimulationAccountInputAdapter(
        "JP", prepare_account_inputs, legacy_history_exists
    )
