"""Pinned Japanese calendar and predictions for the common replay reader."""

import asyncio
from datetime import date

import pandas as pd

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


async def read_signal_input(row, trade_date: date):
    params = row.strategy_params or {}
    version = params.get("data_version")
    if not isinstance(version, str) or not version:
        raise RuleDataMissing("JP replay requires a pinned execution data version")
    if not row.model_id:
        raise ValueError("JP model replay requires its saved registered model ID")
    data = await asyncio.to_thread(
        open_market_execution_data, "JP", data_version=version
    )
    if trade_date not in data.calendar.sessions:
        raise RuleDataMissing("JP replay date is not a covered cash-equity session")
    index = data.calendar.sessions.index(trade_date)
    if not index:
        raise RuleDataMissing("JP replay requires a previous signal session")
    data_day = data.calendar.sessions[index - 1]
    directory, meta = await resolve_model(row.tenant_id, str(row.user_id), row.model_id)
    await asyncio.to_thread(labels_available_on, meta, data.calendar, data_day)
    path = prediction_path(directory)
    scores, digest = await asyncio.to_thread(read_test_scores, path, data_day, data_day)
    if params.get("prediction_sha256") and params["prediction_sha256"] != digest:
        raise RuleDataMissing(
            "JP replay predictions changed since their saved snapshot"
        )
    if not scores.get(data_day):
        raise RuleDataMissing(f"Exact JP test-split signals are missing on {data_day}")
    frame = pd.DataFrame(scores[data_day])
    return ReplaySignalInput(
        market="JP",
        data_day=data_day,
        frame=frame,
        model_dir=directory,
        prediction_file=path,
        data_version=data.data_version,
        prediction_sha256=digest,
    )
