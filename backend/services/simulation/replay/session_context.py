"""Optional market inputs for the original replay API and execution flow.

The router retains ownership, statuses, cursor advancement and order sequencing.
Providers supply data/model provenance, calendar and dated cash rules only.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.models.replay import ReplaySession
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.replay.confirmation import (
    RegisteredReplayConfirmationRules,
)
from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.replay.execution_context import ReplayExecutionContext
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.shared.stock_utils import StockCodeUtil


def registered_session_provider(params):
    market = (params or {}).get("market")
    provider = (
        LOCAL_MARKET_PROVIDERS.get(market.upper()) if isinstance(market, str) else None
    )
    return provider if provider and provider.replay_session_input_preparer else None


@dataclass(frozen=True)
class ReplaySessionInputs:
    params: dict
    model_id: str
    model_dir: Path
    reader: object


async def prepare_registered_session_inputs(req, auth):
    provider = registered_session_provider(req.strategy_params)
    if provider is None:
        return None
    module, name = provider.replay_session_input_preparer.rsplit(".", 1)
    return await getattr(import_module(module), name)(req, auth)


class ReplaySessionBusy(ValueError):
    pass


async def lock_registered_session(db, row):
    """After original ownership lookup, serialize this registered session only."""
    try:
        return (
            await db.execute(
                select(ReplaySession)
                .where(ReplaySession.session_id == row.session_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise ReplaySessionBusy("Replay session is already executing") from error
        raise


@dataclass(frozen=True)
class RegisteredReplaySessionContext:
    params: dict
    reader: object
    cash_rules: object

    @property
    def sessions(self):
        return [int(day.strftime("%Y%m%d")) for day in self.reader.calendar.sessions]

    def execution(self, day):
        return ReplayExecutionContext(
            self.cash_rules.market, self.reader.data_version, day, self.reader
        )

    def accounts(self, session_id):
        return ReplayAccountManager(
            session_id, cash_rules=self.cash_rules, checkpointed=True
        )

    def runner(self, day):
        return ReplayDayRunner(
            market_data=self.reader,
            match_config=self.cash_rules.match_config,
            execution_context=self.execution(day),
        )

    def confirmation(self, day, account):
        return RegisteredReplayConfirmationRules(
            self.execution(day), self.cash_rules, account
        )

    def public_orders(self, orders):
        rows = deepcopy(orders)
        for row in rows:
            if row.get("symbol"):
                try:
                    row["symbol"] = StockCodeUtil.to_prefix(
                        row["symbol"], market=self.cash_rules.market
                    )
                except ValueError:
                    # Rejected user input may intentionally contain a foreign code.
                    pass
        return rows

    def public_account(self, account):
        result = deepcopy(account)
        result.pop("_market_cash_rules", None)
        if "positions" in result:
            result["positions"] = {
                StockCodeUtil.to_prefix(symbol, market=self.cash_rules.market): value
                for symbol, value in result["positions"].items()
            }
        return result


def open_registered_session_context(params, *, reader=None):
    if registered_session_provider(params) is None:
        return None
    version = params.get("data_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Registered replay session requires its saved data_version")
    reader = reader or open_market_execution_data(
        params["market"], data_version=version
    )
    if reader.data_version != version:
        raise ValueError("Replay session publication does not match saved data_version")
    rules = open_registered_replay_cash_rules(params, reader=reader)
    if rules is None:
        raise ValueError("Registered replay session requires dated cash rules")
    return RegisteredReplaySessionContext(params, reader, rules)


async def session_context_for_row(row):
    if registered_session_provider(row.strategy_params) is None:
        return None
    return await asyncio.to_thread(
        open_registered_session_context, row.strategy_params or {}
    )
