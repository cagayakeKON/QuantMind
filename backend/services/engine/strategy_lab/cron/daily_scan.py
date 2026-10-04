"""Daily scan tasks — runs saved Strategy Lab scripts on the latest close.

A user marks a Lab script as "watched" by saving it via
``POST /strategy-lab/watch`` (registered in routers.py); the cron pulls all
watched entries every weekday after close and stores any new buy/sell
signals under Redis key ``qm:lab:signals:latest`` for the dashboard card.

This module is intentionally minimal — it reuses ``run_overfit_check``'s
in-process backtest helper (`_run_one`) to execute the script with a 1-week
look-back window and harvest trades dated today.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from backend.shared.redis_sentinel_client import get_redis_sentinel_client

from ..overfit.runner import _run_one
from ..runner.ast_checker import assert_safe

logger = logging.getLogger(__name__)

WATCH_LIST_KEY = "qm:lab:watch"  # set of script_sha values
WATCH_HASH_KEY = "qm:lab:watch:meta"  # sha -> JSON {user_id, name, code, registered_at}
SIGNALS_KEY = "qm:lab:signals:latest"  # JSON list of latest scan output
LAST_RUN_KEY = "qm:lab:scan:last_run"


def add_watch(
    *,
    script_sha: str,
    user_id: str,
    name: str,
    code: str,
    options=None,
    params=None,
    stock_pool=None,
) -> None:
    r = get_redis_sentinel_client()
    r.sadd(WATCH_LIST_KEY, script_sha.encode("utf-8"))
    payload = {
        "user_id": str(user_id),
        "name": name,
        "code": code,
        "registered_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    if options:
        payload.update(options=options, params=params or {}, stock_pool=stock_pool)
    r.hset(WATCH_HASH_KEY, script_sha, json.dumps(payload, ensure_ascii=False))


def remove_watch(script_sha: str) -> None:
    r = get_redis_sentinel_client()
    r.srem(WATCH_LIST_KEY, script_sha)
    r.hdel(WATCH_HASH_KEY, script_sha)


def list_watch() -> list[dict[str, Any]]:
    r = get_redis_sentinel_client()
    out: list[dict[str, Any]] = []
    try:
        members = r.smembers(WATCH_LIST_KEY) or set()
    except Exception:
        members = set()
    for sha in members:
        sha_str = sha.decode() if isinstance(sha, (bytes, bytearray)) else str(sha)
        try:
            raw = r.hget(WATCH_HASH_KEY, sha_str)
            if not raw:
                continue
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode()
            meta = json.loads(raw)
            out.append({"script_sha": sha_str, **meta})
        except Exception:
            continue
    return out


def fetch_latest_signals() -> dict[str, Any]:
    r = get_redis_sentinel_client()
    raw = r.get(SIGNALS_KEY)
    if not raw:
        return {"generated_at": None, "signals": []}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        return {"generated_at": None, "signals": []}


def run_daily_scan(*, lookback_days: int = 7) -> dict[str, Any]:
    """Iterate every watched Lab script; gather today-dated trades.

    The function is synchronous and intended to run inside a Celery task or a
    one-off CLI invocation. It returns a summary; signals are also persisted
    to Redis so the dashboard can read them without touching DB tables.
    """
    today = _dt.date.today()
    start = today - _dt.timedelta(days=lookback_days)
    today_str = today.strftime("%Y-%m-%d")
    start_str = start.strftime("%Y-%m-%d")

    signals: list[dict[str, Any]] = []
    summary = {"watched": 0, "ok": 0, "failed": 0, "with_signal": 0}

    for entry in list_watch():
        summary["watched"] += 1
        code = entry.get("code") or ""
        if not code:
            continue
        try:
            assert_safe(code)
        except Exception as e:
            summary["failed"] += 1
            logger.warning("daily_scan: AST failed for %s: %s", entry.get("name"), e)
            continue
        try:
            from ..runtime_context import auxiliary_context

            options, params, pool, provider = auxiliary_context(
                options=entry.get("options"),
                params=entry.get("params"),
                stock_pool=entry.get("stock_pool"),
            )
            scan_day = provider.reader.latest_trade_date(today) if provider else today
            if scan_day is None:
                raise ValueError("No covered native scan session")
            scan_start = scan_day - _dt.timedelta(days=lookback_days)
            result = _run_one(
                code,
                start=str(scan_start) if provider else start_str,
                end=str(scan_day) if provider else today_str,
                **({"provider": provider, "params": params} if provider else {}),
            )
        except Exception as e:
            summary["failed"] += 1
            logger.warning("daily_scan: run failed for %s: %s", entry.get("name"), e)
            continue
        if result is None or (provider is not None and result.status != "success"):
            summary["failed"] += 1
            continue
        summary["ok"] += 1
        # Trades dated today_str count as fresh signals
        fresh = [
            t
            for t in (result.trades or [])
            if str(getattr(t, "date", "")).startswith(str(scan_day))
        ]
        if not fresh:
            continue
        summary["with_signal"] += 1
        for t in fresh:
            signals.append(
                {
                    "strategy": entry.get("name"),
                    "script_sha": entry.get("script_sha"),
                    "symbol": getattr(t, "symbol", None),
                    "direction": getattr(t, "direction", None),
                    "price": getattr(t, "price", None),
                    "qty": getattr(t, "qty", None),
                    "reason": getattr(t, "reason", None),
                    "date": getattr(t, "date", today_str),
                    **(
                        {
                            "market": provider.market,
                            "data_version": provider.reader.data_version,
                            "execution_date_mode": "published_daily_delayed",
                        }
                        if provider
                        else {}
                    ),
                }
            )

    payload = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "signals": signals,
        "summary": summary,
    }
    try:
        r = get_redis_sentinel_client()
        r.set(
            SIGNALS_KEY,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        )
        r.set(LAST_RUN_KEY, payload["generated_at"].encode("utf-8"))
    except Exception as e:
        logger.warning("daily_scan: persist failed: %s", e)
    return payload


__all__ = [
    "WATCH_LIST_KEY",
    "WATCH_HASH_KEY",
    "SIGNALS_KEY",
    "LAST_RUN_KEY",
    "add_watch",
    "remove_watch",
    "list_watch",
    "fetch_latest_signals",
    "run_daily_scan",
]
