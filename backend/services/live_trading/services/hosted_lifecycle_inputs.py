"""Optional registered inputs for the existing public controller lifecycle."""

from functools import wraps
import json

from fastapi import HTTPException


def parse_lifecycle_inputs(payload, *, mode, execution_config, live_trade_config):
    from backend.services.trade.sandbox.registered_account_reader import (
        validate_sandbox_execution_inputs,
    )

    try:
        return validate_sandbox_execution_inputs(
            json.loads(payload) if isinstance(payload, str) else payload,
            mode=mode,
            execution_config=execution_config,
            live_trade_config=live_trade_config,
        )
    except (ValueError, NotImplementedError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def lifecycle_session_ranges(inputs):
    from backend.services.simulation.services.market_schedule import (
        open_registered_schedule_context,
    )

    inputs = parse_lifecycle_inputs(
        inputs, mode="SIMULATION", execution_config=None, live_trade_config=None
    )
    try:
        context = open_registered_schedule_context(inputs.market)
        if context is None or not context.is_trading_day(inputs.trade_date):
            raise ValueError("Dated lifecycle inputs require a covered trading session")
        return {
            name: (start.strftime("%H:%M"), end.strftime("%H:%M"))
            for name, (start, end) in context.continuous_windows(
                inputs.trade_date
            ).items()
        }
    except (ValueError, NotImplementedError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def lifecycle_status_cache(*, ttl):
    from backend.services.trade_shared.utils.redis_cache import redis_cache

    def decorator(func):
        cached = redis_cache(ttl=ttl)(func)

        @wraps(func)
        async def wrapped(*args, **kwargs):
            # FastAPI supplies optional query defaults. Keep the original hash
            # when the new inputs are absent, without changing global caching.
            for name in ("market", "execution_context"):
                if kwargs.get(name) is None:
                    kwargs.pop(name, None)
            return await cached(*args, **kwargs)

        return wrapped

    return decorator
