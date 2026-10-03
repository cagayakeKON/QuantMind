"""Registered dated inputs for the original ordinary hosted cycle."""

from dataclasses import dataclass, field, replace
from datetime import date
import hashlib
import json
import logging
import math

from sqlalchemy import text

from backend.services.live_trading.services.manual_execution_context import (
    prepare_manual_context,
)
from backend.services.live_trading.services.hosted_execution_context import (
    validate_hosted_inputs,
)
from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)
from backend.services.simulation.services.account_context import (
    registered_account_input_adapter,
)
from backend.services.simulation.services.signal_loader import SignalLoader, SignalScore
from backend.shared.database_manager_v2 import get_session
from backend.shared.strategy_storage import get_strategy_storage_service
from backend.services.simulation.services.simulation_manager import require_sim_user_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostedCycleSignals:
    run_id: str
    rows: tuple[tuple, ...]
    snapshot_sha256: str = field(init=False)

    def __post_init__(self):
        if not self.run_id or any(not math.isfinite(row[1]) for row in self.rows):
            raise ValueError("Hosted cycle signals require a batch and finite scores")
        encoded = [(*row[:2], row[2].isoformat(), *row[3:]) for row in self.rows]
        object.__setattr__(
            self,
            "snapshot_sha256",
            hashlib.sha256(json.dumps(encoded, allow_nan=False).encode()).hexdigest(),
        )

    def signals(self):
        return [SignalScore(*row) for row in self.rows]

    def require_context(self, context):
        symbols = set()
        for row in self.signals():
            if (
                row.tenant_id != context.tenant_id
                or row.user_id != context.user_id
                or row.run_id != self.run_id
                or row.trade_date != context.trade_date
            ):
                raise ValueError(
                    "Hosted signal rows belong to another owner/batch/session"
                )
            canonical = context.execution.symbol(row.symbol)
            if canonical != row.symbol or canonical in symbols:
                raise ValueError(
                    "Hosted signal rows contain invalid/duplicate securities"
                )
            symbols.add(canonical)


async def prepare_hosted_cycle_context(
    inputs,
    *,
    tenant_id,
    user_id,
    strategy_id,
    config,
    scheduled_trade_date=None,
):
    tenant_id = (tenant_id or "").strip() or "default"
    user_id = str(user_id or "").strip()
    inputs = validate_hosted_inputs(
        inputs,
        mode="SIMULATION",
        execution_config=None,
        live_trade_config=config,
    )
    if scheduled_trade_date is not None:
        scheduled = date.fromisoformat(str(scheduled_trade_date))
        if inputs.trade_date != scheduled:
            raise ValueError(
                "Hosted execution inputs do not match the scheduled session"
            )
    status = await manual_execution_service.get_default_model_hosted_status(
        tenant_id=tenant_id,
        user_id=user_id,
        market=inputs.market,
        trade_date=inputs.trade_date,
    )
    if not status.get("available"):
        raise ValueError(
            f"signal_batch_unavailable:{status.get('reason_code') or 'unavailable'} "
            f"{status.get('message') or ''}"
        )
    run_id = str(status.get("latest_run_id") or "").strip()
    run = await manual_execution_service._load_inference_run(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
    )
    if (
        not run
        or run.get("status") != "completed"
        or str(run.get("model_id") or "") != status.get("latest_default_model_id")
    ):
        raise ValueError("Hosted default model batch is unavailable or has changed")
    try:
        strategy = await get_strategy_storage_service().get(
            strategy_id=int(strategy_id) if strategy_id.isdigit() else 0,
            user_id=user_id,
        )
    except Exception as error:
        logger.warning("Hosted cycle strategy inputs use original defaults: %s", error)
        strategy = None
    # Ordinary cycles infer market from their selected signal batch. Keep their
    # default-model and strategy/default-configuration precedence; do not add
    # the manual task's verified-strategy gate or override rebalance settings.
    params = {**((strategy or {}).get("parameters") or {}), "market": inputs.market}
    context = await prepare_manual_context(
        inputs,
        run=run,
        strategy_params=params,
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        mode="SIMULATION",
    )
    adapter = registered_account_input_adapter(context.market.value)
    if adapter is None:
        raise ValueError("Hosted dated account inputs are unavailable")
    async with get_session(read_only=False) as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await adapter.require_migrated(
            db,
            tenant_id,
            user_id,
            require_sim_user_id(user_id, tenant_id),
        )
        # Reuse the original explicit-batch query, min_score and ordering.
        # An empty batch stays empty; native predictions do not replace PG
        # fusion scores or introduce the manual task's parquet fallback.
        rows = await SignalLoader().load_latest_signals(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
        )
    batch = HostedCycleSignals(
        run_id,
        tuple(
            (s.symbol, s.score, s.trade_date, s.run_id, s.tenant_id, s.user_id)
            for s in rows
        ),
    )
    return replace(context, hosted_signals=batch)
