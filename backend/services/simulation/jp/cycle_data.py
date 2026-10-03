"""Native Japanese model and dated market inputs for the original cycle."""

import asyncio

import pandas as pd

from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.services.cycle_context import SimulationCycleContext
from backend.services.simulation.services.market_execution_data import (
    ReplaySignalInput,
    open_market_execution_data,
)
from .model_signals import (
    labels_available_on,
    prediction_path,
    read_test_scores,
    resolve_model,
)
from .rules import RuleDataMissing


async def prepare_cycle_inputs(params, *, tenant_id, user_id, strategy_id, trade_date):
    if params.get("mode", "signals") != "signals" or params.get("stop_loss_pct"):
        raise NotImplementedError(
            "Code/intraday simulation needs its dated data adapter"
        )
    version = params.get("data_version")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        raise RuleDataMissing("JP cycle execution data version is invalid")
    reader = await asyncio.to_thread(
        open_market_execution_data, "JP", data_version=params.get("data_version")
    )
    sessions = reader.calendar.sessions
    if trade_date not in sessions or not sessions.index(trade_date):
        raise RuleDataMissing("JP cycle requires a covered day and previous session")
    signal_day = sessions[sessions.index(trade_date) - 1]
    model_id = params.get("model_id")
    directory, meta = await resolve_model(
        tenant_id, user_id, model_id, strategy_id=strategy_id
    )
    if params.get("_model_data_version") and (
        params["_model_data_version"] != meta["jp_data_version"]
    ):
        raise RuleDataMissing("JP cycle model publication changed")
    await asyncio.to_thread(labels_available_on, meta, reader.calendar, signal_day)
    path = prediction_path(directory)
    scores, digest = await asyncio.to_thread(
        read_test_scores, path, signal_day, signal_day
    )
    if params.get("prediction_sha256") and params["prediction_sha256"] != digest:
        raise RuleDataMissing("JP cycle predictions differ from the requested snapshot")
    if not scores.get(signal_day):
        raise RuleDataMissing(
            f"Exact JP test-split signals are missing on {signal_day}"
        )
    model_id = model_id or meta["effective_model_id"]
    params.update(
        market="JP",
        data_version=reader.data_version,
        _model_data_version=meta["jp_data_version"],
    )
    signal_input = ReplaySignalInput(
        "JP",
        signal_day,
        pd.DataFrame(scores[signal_day]),
        directory,
        path,
        reader.data_version,
        digest,
    )
    rules = open_registered_replay_cash_rules(params, reader=reader)
    return SimulationCycleContext(
        tenant_id,
        user_id,
        strategy_id,
        model_id,
        params,
        trade_date,
        signal_input,
        rules,
    )
