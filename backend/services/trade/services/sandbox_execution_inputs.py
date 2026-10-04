"""Registered dates, quotes and funds for the original sandbox consumer.

No strategy selection, order loop or financial formula lives here. Dated cash,
security rules and execution still come from the common simulation adapters.
"""

import asyncio
from dataclasses import dataclass
import math

from sqlalchemy import text

from backend.services.live_trading.services.hosted_execution_context import (
    validate_hosted_inputs,
)
from backend.services.simulation.replay.execution_context import ReplayExecutionContext
from backend.services.simulation.services.account_context import (
    SimulationAccountContext,
    registered_account_input_adapter,
)
from backend.services.simulation.services.dated_account import (
    DatedSimulationAccountManager,
)
from backend.services.trade_shared.simulation_manager import canonical_sim_uid
from backend.shared.database_manager_v2 import get_session


@dataclass(frozen=True)
class SandboxOrderInputs:
    tenant_id: str
    user_id: int
    account_context: SimulationAccountContext
    execution: ReplayExecutionContext
    symbol: str
    bar: object
    account: dict

    @property
    def price(self):
        return float(self.bar.open)

    @property
    def trading_unit(self):
        return self.execution.trading_unit(self.symbol, {self.symbol: self.bar})

    def accounts(self, db, redis):
        return DatedSimulationAccountManager(
            db,
            redis,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            cash_rules=self.account_context.rules,
        )


async def prepare_sandbox_order_inputs(signal, active, redis):
    if not isinstance(active, dict):
        raise ValueError("Dated sandbox runtime is unavailable")
    inputs = validate_hosted_inputs(
        signal["execution_context"],
        mode=active.get("mode"),
        execution_config=active.get("execution_config"),
        live_trade_config=active.get("live_trade_config"),
    )
    saved = validate_hosted_inputs(
        active.get("execution_context"),
        mode=active.get("mode"),
        execution_config=active.get("execution_config"),
        live_trade_config=active.get("live_trade_config"),
    )
    if inputs.model_dump() != saved.model_dump():
        raise ValueError("Sandbox signal differs from its saved dated inputs")
    tenant = signal.get("tenant_id", "default")
    raw_user = str(signal.get("user_id", ""))
    strategy = str(signal.get("strategy_id", ""))
    expected_strategy = str(
        active.get("strategy_id") or active.get("strategy_name") or ""
    )
    if (
        active.get("runtime_tenant_id") != tenant
        or active.get("runtime_user_id") != raw_user
        or strategy != expected_strategy
        or not signal.get("run_id")
        or signal["run_id"]
        != (active.get("sandbox_restored_run_id") or active.get("sandbox_run_id"))
    ):
        raise ValueError("Sandbox signal belongs to another owner/strategy/runtime")
    if active.get("trading_permission") != "trade_enabled":
        raise ValueError("Dated sandbox runtime does not have trading permission")
    uid = canonical_sim_uid(raw_user)
    if uid <= 0:
        raise ValueError("Dated sandbox requires a valid simulation owner")
    adapter = registered_account_input_adapter(inputs.market)
    if adapter is None:
        raise ValueError("Sandbox dated account inputs are unavailable")
    account_context = await asyncio.to_thread(
        adapter.prepare_inputs, inputs.model_dump()
    )
    if (
        not isinstance(account_context, SimulationAccountContext)
        or account_context.market != inputs.market
        or account_context.trade_date != inputs.trade_date
        or account_context.rules.data_version != inputs.data_version
    ):
        raise ValueError("Wrong sandbox account input adapter")
    execution = ReplayExecutionContext(
        inputs.market,
        inputs.data_version,
        inputs.trade_date,
        account_context.rules.reader,
    )
    symbol = execution.symbol(signal.get("data", {}).get("symbol"))
    bars = await asyncio.to_thread(
        execution.reader.load_date, inputs.trade_date, [symbol]
    )
    bar = bars.get(symbol)
    if bar is None:
        raise ValueError(f"Sandbox opening quote is unavailable for {symbol}")
    execution.matching_rules(symbol, bar)
    execution.trading_unit(symbol, bars)
    if not math.isfinite(float(bar.open)) or bar.open <= 0 or bar.suspended:
        raise ValueError(f"Sandbox opening quote is unavailable for {symbol}")
    async with get_session(read_only=False) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await adapter.require_migrated(db, tenant, raw_user, uid)
        manager = DatedSimulationAccountManager(
            db,
            redis,
            tenant_id=tenant,
            user_id=uid,
            cash_rules=account_context.rules,
        )
        account = await manager.get_account(uid, tenant_id=tenant, market=inputs.market)
        if account is None:
            raise ValueError("Sandbox registered cash account is not initialized")
        cursor = account_context.rules.backtest_state(account)["cursor"]
        if cursor and str(inputs.trade_date) <= cursor:
            raise ValueError(
                "Dated sandbox session is completed; wait for a new published cycle"
            )
        # Pure projection for target sizing. Inventory/ledger changes are applied
        # by the original execution transaction, never by this read-only input.
        from backend.services.simulation.services.dated_account_day import (
            project_account_to_day,
        )

        account = await asyncio.to_thread(
            project_account_to_day, account_context.rules, account, inputs.trade_date
        )
    return SandboxOrderInputs(
        tenant, uid, account_context, execution, symbol, bar, account
    )
