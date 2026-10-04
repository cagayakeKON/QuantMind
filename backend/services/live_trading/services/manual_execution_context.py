"""Registered inputs to the original manual preview and task consumer.

This module supplies dates, quotes and rule inputs. Selection, budget allocation,
task persistence and order sequencing stay in manual_execution_service.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from functools import wraps
from importlib import import_module
import math
from types import SimpleNamespace

from pydantic import Field
from fastapi import HTTPException
from sqlalchemy import text

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.services.account_context import (
    DatedAccountInputs,
    registered_account_input_adapter,
)
from backend.services.simulation.services.cycle_context import (
    prepare_registered_cycle_context,
)
from backend.services.simulation.services.market_rules import infer_market
from backend.services.trade_shared.simulation_manager import require_sim_user_id
from backend.shared.database_manager_v2 import get_session
from backend.shared.fundamental_aligner import FundamentalAligner
from backend.shared.stock_utils import StockCodeUtil


class DatedManualInputs(DatedAccountInputs):
    market: str
    model_data_version: str | None = None
    prediction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def registered_manual_preview(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        if kwargs.get("execution_context") is None:
            return await method(self, *args, **kwargs)
        try:
            return await method(self, *args, **kwargs)
        except (ValueError, NotImplementedError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return wrapped


def registered_manual_task(method):
    @wraps(method)
    async def wrapped(self, task):
        request = task.get("request_json")
        if not isinstance(request, dict) or request.get("execution_context") is None:
            return await method(self, task)
        try:
            return await method(self, task)
        except Exception as error:
            # Original unregistered failure behavior is not changed. A new dated
            # input failure must leave an inspectable failed task after the
            # original PG session has rolled back, including final mark failures.
            from .manual_execution_persistence import manual_execution_persistence
            from .manual_execution_log_stream import manual_execution_log_stream

            detail = str(getattr(error, "detail", None) or error)
            await manual_execution_persistence.update_task(
                task_id=task["task_id"],
                status="failed",
                stage="validating",
                error_stage="signal_loading",
                error_message=detail,
                result_payload={"success": False, "error": detail},
            )
            manual_execution_log_stream.append_log(
                task_id=task["task_id"],
                tenant_id=task.get("tenant_id") or "default",
                user_id=str(task.get("user_id") or ""),
                level="error",
                stage="validating",
                status="failed",
                line=detail,
            )
            manual_execution_log_stream.update_state(
                task_id=task["task_id"],
                tenant_id=task.get("tenant_id") or "default",
                user_id=str(task.get("user_id") or ""),
                stage="validating",
                status="failed",
                error_stage="signal_loading",
                error_message=detail,
            )

    return wrapped


async def prepare_manual_context(
    inputs, *, run, strategy_params, tenant_id, user_id, strategy_id, mode
):
    inputs = DatedManualInputs.model_validate(inputs)
    market = inputs.market.strip().upper()
    provider = LOCAL_MARKET_PROVIDERS.get(market)
    if (
        mode != "SIMULATION"
        or not provider
        or not provider.simulation_cycle_input_preparer
    ):
        raise ValueError(
            "Registered dated inputs require a supported simulation market"
        )
    if str(strategy_params.get("market") or "CN").strip().upper() != market:
        raise ValueError("Manual strategy and execution markets differ")
    # Keep the original run/verified-strategy gates. Never synthesize a completed
    # inference run or bind to a different user's default model.
    require_sim_user_id(user_id, tenant_id)
    params = {
        **deepcopy(strategy_params),
        "market": market,
        "model_id": str(run.get("model_id") or ""),
        "data_version": inputs.data_version,
        "commission_rate": str(inputs.commission_rate),
        "slippage_bps": str(inputs.slippage_bps),
        "prediction_sha256": inputs.prediction_sha256,
        "_model_data_version": inputs.model_data_version,
    }
    context = await prepare_registered_cycle_context(
        params,
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        trade_date=inputs.trade_date,
    )
    if context is None or context.model_id != params["model_id"]:
        raise ValueError("Manual model does not match its registered input")
    if (
        date.fromisoformat(str(run.get("data_trade_date"))[:10])
        != context.signal_input.data_day
        or date.fromisoformat(str(run.get("prediction_trade_date"))[:10])
        != context.trade_date
    ):
        raise ValueError(
            "Inference run and dated manual inputs have different sessions"
        )
    return context


def saved_manual_inputs(context):
    return DatedManualInputs(
        market=context.market.value,
        data_version=context.cash_rules.data_version,
        trade_date=context.trade_date,
        commission_rate=context.cash_rules.config["commission_rate"],
        slippage_bps=context.cash_rules.config["slippage_bps"],
        model_data_version=context.params["_model_data_version"],
        prediction_sha256=context.signal_input.prediction_sha256,
    ).model_dump(mode="json")


def manual_signal_rows(context):
    rows = []
    for item in context.signal_input.frame.itertuples(index=False):
        score = float(item.score)
        if not math.isfinite(score):
            raise ValueError("Manual signals require finite native scores")
        rows.append(
            {
                "symbol": StockCodeUtil.to_prefix(
                    item.symbol, market=context.market.value
                ),
                "fusion_score": score,
                "signal_side": None,
                "expected_price": None,
            }
        )
    if len({row["symbol"] for row in rows}) != len(rows):
        raise ValueError("Manual inputs contain duplicate securities")
    # Original manual TopK filtering owns ranking and the score threshold.
    return rows


def validate_manual_rows(context, rows):
    normalized = []
    for row in rows:
        if infer_market(row.get("symbol")).value != context.market.value:
            raise ValueError("Inference run contains another market's security")
        if not math.isfinite(float(row.get("fusion_score"))):
            raise ValueError("Manual inference rows require finite scores")
        item = dict(row)
        item["symbol"] = StockCodeUtil.to_prefix(
            row["symbol"], market=context.market.value
        )
        normalized.append(item)
    if len({row["symbol"] for row in normalized}) != len(normalized):
        raise ValueError("Manual inference rows contain duplicate securities")
    return normalized


async def read_manual_snapshot(context, *, tenant_id, user_id, redis, db=None):
    if db is None:
        async with get_session(read_only=False) as master:
            await master.execute(text("SET TRANSACTION READ ONLY"))
            return await read_manual_snapshot(
                context,
                tenant_id=tenant_id,
                user_id=user_id,
                redis=redis,
                db=master,
            )
    context.require_owner(tenant_id, user_id, context.strategy_id, None)
    uid = require_sim_user_id(user_id, tenant_id)
    adapter = registered_account_input_adapter(context.market.value)
    if adapter is None:
        raise ValueError("Registered manual account inputs are unavailable")
    await adapter.require_migrated(db, tenant_id, user_id, uid)
    manager = context.accounts(db, redis)
    account = await manager.get_account(
        uid, tenant_id=tenant_id, market=context.market.value
    )
    if account is None:
        return None
    # Prospective settlement/corporate actions are pure during preview. Actual
    # lots and checkpoints are changed only by the original locked fill path.
    from backend.services.simulation.services.dated_account_day import (
        project_account_to_day,
    )

    account = await asyncio.to_thread(
        project_account_to_day, context.cash_rules, account, context.trade_date
    )
    return {
        "account_id": manager.account_id,
        "snapshot_at": account.get("timestamp"),
        "total_asset": account.get("total_asset"),
        "available_cash": account["cash"],
        "cash": account["cash"],
        "market_value": account.get("market_value"),
        "position_count": len(account["positions"]),
        "positions": deepcopy(account["positions"]),
        "source": "simulation_account",
    }


@dataclass(frozen=True)
class ManualPlanInputs:
    context: object
    bars: dict
    fundamental_filter: object

    def symbol(self, symbol):
        return self.context.execution.symbol(symbol)

    def unit(self, symbol):
        return self.context.execution.trading_unit(symbol, self.bars)

    def rules(self, symbol):
        canonical = self.symbol(symbol)
        bar = self.bars.get(canonical)
        if bar is None:
            raise ValueError(f"Exact dated manual bar is missing for {canonical}")
        return self.context.execution.matching_rules(symbol, bar)

    def price(self, symbol, side):
        bar = self.bars.get(self.symbol(symbol))
        if bar is None or bar.suspended:
            return 0.0
        if not math.isfinite(bar.open) or bar.open <= 0:
            return 0.0
        # Reuse registered tick/slippage pricing; this is a preview reference,
        # never a limit price or a replacement realtime tick.
        return float(
            self.rules(symbol).price(
                side.lower(), bar, self.context.cash_rules.match_config
            )
        )

    def fee(self, symbol, quantity, price, side):
        if quantity <= 0:
            return 0.0
        from decimal import Decimal

        return float(
            self.rules(symbol).fees(
                quantity,
                Decimal(str(price)),
                side.lower(),
                self.context.cash_rules.match_config,
            )[3]
        )


async def prepare_manual_plan_inputs(context, snapshot, rows, strategy_params):
    symbols = {row["symbol"] for row in rows} | set(snapshot["positions"])
    bars = await asyncio.to_thread(
        context.cash_rules.reader.load_date, context.trade_date, list(symbols)
    )
    # The original planner selects candidates. Their price/unit/fee accessors
    # validate exact bar identity through the registered execution context;
    # do not reread the publication once for every unselected native signal.
    constraints = {
        key[2:]: value for key, value in strategy_params.items() if key.startswith("f_")
    }

    def filterer(_day, instruments, *, constraints):
        return instruments

    if any(value is not None for value in constraints.values()):
        factory = LOCAL_MARKET_PROVIDERS[
            context.market.value
        ].fundamental_snapshot_reader_factory
        if not factory:
            raise ValueError("Registered fundamental inputs are unavailable")
        module, name = factory.rsplit(".", 1)
        reader = await asyncio.to_thread(
            getattr(import_module(module), name),
            SimpleNamespace(data_version=context.cash_rules.data_version),
        )
        aligner = FundamentalAligner(snapshot_loader=reader)

        def filterer(_day, instruments, *, constraints):
            # Fundamental information must be known on the signal day.
            return aligner.filter_instruments(
                context.signal_input.data_day, instruments, constraints
            )

    return ManualPlanInputs(context, bars, filterer)
