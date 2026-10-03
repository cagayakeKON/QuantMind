"""Read registered simulation funds through the original internal gateway.

The gateway restores the committed, dated PG checkpoint with the registered
cash rules. No worker reconstructs cash provenance from a raw Redis snapshot.
"""

from datetime import date
import math
import os

import httpx

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


def registered_sandbox_market(execution_config, live_trade_config):
    execution_config = execution_config if isinstance(execution_config, dict) else {}
    live_trade_config = live_trade_config if isinstance(live_trade_config, dict) else {}
    market = (
        str(live_trade_config.get("market") or execution_config.get("market") or "")
        .strip()
        .upper()
    )
    provider = LOCAL_MARKET_PROVIDERS.get(market)
    return market if provider and provider.simulation_account_input_adapter else None


def read_sandbox_simulation_account(*, market, tenant_id, user_id):
    from backend.services.trade_shared.simulation_manager import canonical_sim_uid
    from backend.shared.auth import get_internal_call_secret

    base_url = os.getenv("TRADE_SERVICE_INTERNAL_URL")
    if not base_url:
        base_url = (
            os.getenv("TRADE_SERVICE_URL", "http://127.0.0.1:8002").rstrip("/")
            + "/api/v1/internal/strategy"
        )
    response = httpx.get(
        base_url.rstrip("/") + "/sync-account",
        params={"market": market, "trading_mode": "SIMULATION"},
        headers={
            "X-Internal-Call": get_internal_call_secret(),
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
        },
        timeout=3.0,
    )
    response.raise_for_status()
    account = response.json()
    tenant = str(tenant_id or "").strip() or "default"
    if (
        not isinstance(account, dict)
        or account.get("tenant_id") != tenant
        or account.get("user_id") != canonical_sim_uid(user_id)
        or account.get("market") != market
    ):
        raise ValueError("Registered sandbox account owner/market mismatch")
    inputs = account.get("execution_context")
    if (
        not isinstance(inputs, dict)
        or inputs.get("market") != market
        or inputs.get("execution_mode") != "daily_open"
        or not inputs.get("data_version")
    ):
        raise ValueError("Registered sandbox account has no dated provenance")
    date.fromisoformat(inputs["trade_date"])
    for field in ("cash", "total_asset", "market_value"):
        if not math.isfinite(float(account[field])):
            raise ValueError("Registered sandbox account contains non-finite values")
    positions = account.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("Registered sandbox account has no position mapping")
    for symbol, position in positions.items():
        if StockCodeUtil.to_prefix(symbol, market=market) != symbol:
            raise ValueError("Registered sandbox position is not in API code format")
        for field in ("volume", "available_volume", "cost", "price", "market_value"):
            if not math.isfinite(float(position[field])):
                raise ValueError(
                    "Registered sandbox position contains non-finite values"
                )
    return account
