"""Registered dated inputs to the original account read/reset endpoints.

Reset cleanup, owner aliases, settings and global fund baselines remain in the
original router. These helpers only bind registered cash metadata and publication
inputs; they never reconstruct missing cash provenance from Redis.
"""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib import import_module

from pydantic import BaseModel, Field
from sqlalchemy import select, text

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.services.dated_account import (
    DatedSimulationAccountManager,
    METADATA_KEY,
)
from backend.services.simulation.services.ledger_service import SimulationLedgerService
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil


class DatedAccountInputs(BaseModel):
    data_version: str
    trade_date: date
    commission_rate: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, lt=10000)


class RegisteredAccountUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class SimulationAccountInputAdapter:
    market: str
    prepare_inputs: Callable
    legacy_history_exists: Callable

    async def require_migrated(self, db, tenant_id, raw_user_id, user_id):
        if await self.legacy_history_exists(
            db, tenant_id=tenant_id, user_ids={str(raw_user_id), str(user_id)}
        ):
            raise RegisteredAccountUnavailable(
                "Existing registered-market sessions require migration before using the common account"
            )


@dataclass(frozen=True)
class SimulationAccountContext:
    market: str
    trade_date: date
    rules: object

    def __post_init__(self):
        if (
            self.market != self.rules.market
            or self.trade_date not in self.rules.reader.calendar.sessions
        ):
            raise ValueError("Account inputs do not match their market/trading session")

    def validate_initial_cash(self, initial_cash):
        # Pure preparation validates publication coverage and settlement before
        # the original reset performs any settings, cleanup or runtime writes.
        return self.rules.prepare_day(
            self.rules.initialize(initial_cash), self.trade_date
        )

    def serialize(self, account, checkpoint=None):
        public = deepcopy(account)
        public.pop(METADATA_KEY, None)
        public["positions"] = {
            StockCodeUtil.to_prefix(symbol, market=self.market): position
            for symbol, position in public["positions"].items()
        }
        public["execution_context"] = {
            "market": self.market,
            "data_version": self.rules.data_version,
            "trade_date": str(self.trade_date),
            "execution_mode": "daily_open",
            **deepcopy(self.rules.config),
        }
        if checkpoint and checkpoint.get("cycle_inputs"):
            public["execution_context"]["last_cycle_inputs"] = deepcopy(
                checkpoint["cycle_inputs"]
            )
        return public

    async def initialize_after_reset(
        self, db, redis, *, tenant_id, user_id, initial_cash
    ):
        account_id = SimulationLedgerService.build_account_id(tenant_id, str(user_id))
        remaining = await db.scalar(
            select(SimulationAccount.account_id).where(
                SimulationAccount.account_id == account_id
            )
        )
        if remaining is not None:
            raise RegisteredAccountUnavailable(
                "Original reset cleanup did not remove the account root"
            )
        manager = DatedSimulationAccountManager(
            db, redis, tenant_id=tenant_id, user_id=user_id, cash_rules=self.rules
        )
        account = await manager.initialize(initial_cash, self.trade_date)
        await db.commit()
        return self.serialize(account)


def registered_account_input_adapter(market):
    provider = LOCAL_MARKET_PROVIDERS.get(market)
    if not provider or not provider.simulation_account_input_adapter:
        return None
    module, name = provider.simulation_account_input_adapter.rsplit(".", 1)
    adapter = getattr(import_module(module), name)()
    if (
        not isinstance(adapter, SimulationAccountInputAdapter)
        or adapter.market != market
    ):
        raise RegisteredAccountUnavailable("Wrong registered account input adapter")
    return adapter


async def prepare_registered_account_reset(
    market, execution_inputs, *, db, tenant_id, raw_user_id, user_id
):
    adapter = registered_account_input_adapter(market)
    if adapter is None:
        return None
    if execution_inputs is None:
        raise ValueError("Registered account reset requires dated execution_context")
    context = await asyncio.to_thread(
        adapter.prepare_inputs, execution_inputs.model_dump()
    )
    if not isinstance(context, SimulationAccountContext) or context.market != market:
        raise RegisteredAccountUnavailable("Wrong registered account reset context")
    await adapter.require_migrated(db, tenant_id, raw_user_id, user_id)
    return context


async def read_registered_simulation_account(
    market, *, redis, tenant_id, raw_user_id, user_id, execution_inputs=None
):
    adapter = registered_account_input_adapter(market)
    if adapter is None:
        return False, None
    # Match the original ledger recovery's master source, while forbidding writes.
    # Replica lag must not turn a just-committed cash checkpoint into "missing".
    async with get_session(read_only=False) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await adapter.require_migrated(db, tenant_id, raw_user_id, user_id)
        root = await db.get(
            SimulationAccount,
            SimulationLedgerService.build_account_id(tenant_id, str(user_id)),
        )
        if root is None:
            return True, None
        if root.tenant_id != tenant_id or root.user_id != str(user_id):
            raise RegisteredAccountUnavailable(
                "Registered account belongs to another owner"
            )
        if root.market_state is None:
            return True, None
        if not isinstance(root.market_state, dict):
            raise RegisteredAccountUnavailable(
                "Invalid registered account checkpoint mapping"
            )
        checkpoint = root.market_state.get(market)
        if checkpoint is None:
            return True, None
        if not isinstance(checkpoint, dict):
            raise RegisteredAccountUnavailable("Invalid registered account checkpoint")
        metadata = checkpoint.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("state"), dict
        ):
            raise RegisteredAccountUnavailable(
                "Invalid registered account rule metadata"
            )
        config = metadata["state"].get("config")
        if not isinstance(config, dict):
            raise RegisteredAccountUnavailable(
                "Invalid registered account cash settings"
            )
        inputs = DatedAccountInputs(
            data_version=checkpoint.get("data_version"),
            trade_date=metadata.get("prepared_date"),
            commission_rate=config.get("commission_rate"),
            slippage_bps=config.get("slippage_bps"),
        )
        saved_date = inputs.trade_date
        if execution_inputs is not None:
            inputs = DatedAccountInputs.model_validate(execution_inputs)
        context = await asyncio.to_thread(adapter.prepare_inputs, inputs.model_dump())
        if (
            not isinstance(context, SimulationAccountContext)
            or context.market != market
            or (
                execution_inputs is not None and context.trade_date != inputs.trade_date
            )
        ):
            raise RegisteredAccountUnavailable("Wrong registered account read context")
        # Use this single PG checkpoint for date, publication and cash. A second
        # account read could observe a newer day after opening its publication.
        account = context.rules.restore_checkpoint(
            checkpoint,
            saved_date if execution_inputs is not None else context.trade_date,
        )
        if execution_inputs is not None:
            # The same rules as execution project the committed checkpoint to the
            # requested day. This read does not advance the persisted account.
            prepared = context.rules.prepare_day(account, context.trade_date)
            context.rules.corporate_action_inputs(account, prepared, context.trade_date)
            account = prepared
        return True, context.serialize(account, checkpoint)
