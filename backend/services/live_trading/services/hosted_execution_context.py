"""Market/model/date inputs for the original hosted task lifecycle."""

from .manual_execution_context import DatedManualInputs
from backend.services.simulation.services.market_schedule import (
    open_registered_schedule_context,
)
from backend.shared.model_registry import model_registry_service


async def load_hosted_model_inputs(*, tenant_id, user_id, market):
    selected = str(market).strip().upper()
    schedule = open_registered_schedule_context(selected)
    if schedule is None:
        raise ValueError("Registered hosted market inputs are unavailable")
    model = await model_registry_service.get_default_model(
        tenant_id=tenant_id, user_id=user_id, market=selected
    )
    if model is None:
        return None, schedule
    metadata = model.get("metadata_json") or {}
    if str(metadata.get("market") or "CN").strip().upper() != selected:
        raise ValueError("Hosted default model belongs to another market")
    if metadata.get("system_default"):
        return None, schedule
    return {**model, "metadata_json": metadata}, schedule


def validate_hosted_inputs(inputs, *, mode, execution_config, live_trade_config):
    inputs = DatedManualInputs.model_validate(inputs)
    selected = inputs.market.strip().upper()
    if mode != "SIMULATION":
        raise ValueError("Registered dated hosted inputs require simulation mode")
    for config in (execution_config, live_trade_config):
        if config and config.get("trading_mode"):
            if str(config["trading_mode"]).strip().upper() != mode:
                raise ValueError("Hosted configuration and execution modes differ")
        if config and config.get("market"):
            if str(config["market"]).strip().upper() != selected:
                raise ValueError("Hosted configuration and execution markets differ")
    return inputs.model_copy(update={"market": selected})


def validate_hosted_duplicate(task, inputs, *, tenant_id, user_id, strategy_id, mode):
    expected = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "strategy_id": strategy_id,
        "trading_mode": mode,
        "task_type": "hosted",
    }
    if any(str(task.get(key) or "") != value for key, value in expected.items()):
        raise ValueError("Hosted task ID belongs to another execution")
    saved = (task.get("request_json") or {}).get("execution_context")
    if not isinstance(saved, dict):
        raise ValueError("Hosted task has no registered execution inputs")
    saved_inputs = DatedManualInputs.model_validate(saved)
    incoming = inputs.model_dump(exclude_none=True)
    if any(
        saved_inputs.model_dump().get(key) != value for key, value in incoming.items()
    ):
        raise ValueError("Hosted task ID has different dated execution inputs")
