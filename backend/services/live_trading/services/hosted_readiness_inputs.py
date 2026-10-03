"""Registered dated data inputs for the original readiness checks."""

import asyncio
import json
import math

from backend.services.simulation.services.account_context import (
    SimulationAccountContext,
    registered_account_input_adapter,
)
from backend.shared.stock_utils import StockCodeUtil
from .hosted_execution_context import validate_hosted_inputs


def parse_hosted_readiness_inputs(raw, *, mode, market):
    if raw is None:
        return None
    return validate_hosted_inputs(
        json.loads(raw) if isinstance(raw, str) else raw,
        mode=mode,
        execution_config=None,
        live_trade_config={"market": market},
    )


async def check_hosted_dated_quotes(inputs):
    adapter = registered_account_input_adapter(inputs.market)
    if adapter is None:
        raise ValueError("Registered dated quote inputs are unavailable")
    context = await asyncio.to_thread(adapter.prepare_inputs, inputs.model_dump())
    if not isinstance(context, SimulationAccountContext) or (
        context.market != inputs.market
        or context.trade_date != inputs.trade_date
        or context.rules.data_version != inputs.data_version
    ):
        raise ValueError("Registered quote inputs belong to another market/session")
    bars = await asyncio.to_thread(context.rules.reader.load_date, inputs.trade_date)
    available = False
    for symbol, bar in bars.items():
        if (
            bar.trade_date != inputs.trade_date
            or StockCodeUtil.to_suffix(symbol, market=inputs.market) != bar.symbol
        ):
            raise ValueError("Registered opening quote has another security/session")
        if not bar.suspended and math.isfinite(bar.open) and bar.open > 0:
            available = True
    return {
        "ok": available,
        "message": (
            f"{inputs.market} {inputs.trade_date} daily_open "
            f"data_version={context.rules.data_version}: "
            + ("日期开盘行情已就绪" if available else "无可用日期开盘行情")
        ),
    }
