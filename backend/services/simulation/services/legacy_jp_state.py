"""Read-only recognition of the previous native-JPY account format.

Standard simulation retains the original base-currency account contract. Old
native balances must not be silently used as that account's opening capital.
"""

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from backend.shared.simulation_account_keys import (
    account_lookup_keys,
    ledger_user_id_candidates,
)
from backend.shared.trade_account_cache import read_json_cache


class LegacyJPNativeState(ValueError):
    pass


def read_existing_jp_account(redis, tenant_id, user_id):
    """Inspect every legacy alias without promoting it or changing settings."""
    accounts = [
        read_json_cache(redis, key)
        for key in account_lookup_keys(tenant_id, user_id, "JP")
    ]
    return next((a for a in accounts if is_legacy_jp_native(a)), None)


def is_legacy_jp_native(account):
    return isinstance(account, dict) and (
        "_market_cash_rules" in account
        or (account.get("currency") == "JPY" and "data_version" in account)
    )


def is_legacy_jp_runtime(payload):
    context = payload.get("execution_context") if isinstance(payload, dict) else None
    return isinstance(context, dict) and str(context.get("market", "")).upper() == "JP"


async def require_standard_account(db, tenant_id, user_id, *, cached=None):
    """Inspect existing metadata without deleting or converting any state."""
    states = (
        (
            await db.execute(
                text(
                    "SELECT to_jsonb(a)->'market_state' AS state FROM simulation_accounts a "
                    "WHERE a.tenant_id=:tenant AND a.user_id IN :users"
                )
                .bindparams(bindparam("users", expanding=True))
                .columns(state=JSONB()),
                {"tenant": tenant_id, "users": ledger_user_id_candidates(user_id)},
            )
        )
        .scalars()
        .all()
    )
    if any(
        isinstance(state, dict) and "JP" in state for state in states
    ) or is_legacy_jp_native(cached):
        raise LegacyJPNativeState(
            "An existing native-JPY account is retained read-only. "
            "It cannot be reset or interpreted as the standard base-currency account."
        )


async def require_standard_replay_snapshot(db, session_id):
    state = (
        await db.execute(
            text(
                "SELECT to_jsonb(s)->'market_state' AS state FROM replay_equity_snapshots s "
                "WHERE s.session_id=:session ORDER BY s.trade_date DESC LIMIT 1"
            ).columns(state=JSONB()),
            {"session": session_id},
        )
    ).scalar_one_or_none()
    if state:
        raise LegacyJPNativeState(
            "Existing native-JPY replay snapshot is retained read-only"
        )
