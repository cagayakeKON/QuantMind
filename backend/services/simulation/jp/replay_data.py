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
    directory, meta = await resolve_model(
        row.tenant_id, params.get("_model_user_id", str(row.user_id)), row.model_id
    )
    if params.get("_model_data_version") and (
        params["_model_data_version"] != meta.get("jp_data_version")
    ):
        raise RuleDataMissing("JP replay model publication changed since creation")
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


async def prepare_session_inputs(req, auth):
    """Pin native JP inputs for the existing session creation, without writes."""
    from backend.services.simulation.replay.session_context import ReplaySessionInputs

    if req.mode.strip().lower() == "code" or (req.stop_loss_pct or 0) > 0:
        raise NotImplementedError(
            "Registered replay requires its code/intraday data adapter "
            "before using these execution paths"
        )
    params = dict(req.strategy_params)
    params["market"] = "JP"
    version = params.get("data_version")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        raise RuleDataMissing("JP replay execution data version is invalid")
    # Pin the requested mode; private legacy inputs cannot select another path.
    params["_mode"] = "signals"
    data = await asyncio.to_thread(
        open_market_execution_data, "JP", data_version=params.get("data_version")
    )
    days = [
        day for day in data.calendar.sessions if req.start_date <= day <= req.end_date
    ]
    if not days:
        raise ValueError("区间内无交易日")
    index = data.calendar.sessions.index(days[0])
    if not index:
        raise RuleDataMissing("JP replay requires a previous signal session")
    signal_day = data.calendar.sessions[index - 1]
    directory, meta = await resolve_model(auth.tenant_id, auth.user_id, req.model_id)
    await asyncio.to_thread(labels_available_on, meta, data.calendar, signal_day)
    path = prediction_path(directory)
    scores, digest = await asyncio.to_thread(
        read_test_scores, path, signal_day, signal_day
    )
    if not scores.get(signal_day):
        raise RuleDataMissing(
            f"Exact JP test-split signals are missing on {signal_day}"
        )
    model_id = req.model_id or meta["effective_model_id"]
    params.update(
        data_version=data.data_version,
        prediction_sha256=digest,
        _model_dir=str(directory),
        # Model source identity is separate from the original integer account alias.
        _model_user_id=auth.user_id,
        _model_data_version=meta["jp_data_version"],
    )
    return ReplaySessionInputs(params, model_id, directory, data)
