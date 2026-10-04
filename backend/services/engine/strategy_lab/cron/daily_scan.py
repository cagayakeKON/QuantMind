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
import hashlib
import json
import logging
import uuid
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
    tenant_id=None,
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
        if str(options.get("market") or "").upper() == "JP":
            payload["tenant_id"] = str(tenant_id or "")
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
    claims = []
    redis = None
    native_seen = False
    legacy_seen = False
    legacy_signals = []

    for entry in list_watch():
        if str((entry.get("options") or {}).get("market") or "").upper() == "JP":
            native_seen = True
        else:
            legacy_seen = True
        summary["watched"] += 1
        code = entry.get("code") or ""
        if not code:
            continue
        claim = None
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
                latest_publication=True,
            )
            scan_day = provider.reader.latest_trade_date(today) if provider else today
            if scan_day is None:
                raise ValueError("No covered native scan session")
            scan_start = scan_day - _dt.timedelta(days=lookback_days)
            if provider is not None and provider.market == "JP":
                redis = redis or get_redis_sentinel_client()
                fingerprint = {
                    "tenant_id": entry.get("tenant_id", ""),
                    "user_id": entry.get("user_id", "0"),
                    "script_sha": entry.get("script_sha"),
                    "code": code,
                    "options": options,
                    "params": params,
                    "stock_pool": pool,
                    "scan_day": str(scan_day),
                    "start": str(scan_start),
                }
                digest = hashlib.sha256(
                    json.dumps(
                        fingerprint, sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                ).hexdigest()
                claim = ("qm:lab:scan:source:" + digest, uuid.uuid4().hex)
                if not redis.set(claim[0], claim[1], nx=True, ex=3600):
                    summary["skipped"] = summary.get("skipped", 0) + 1
                    continue
            result = _run_one(
                code,
                start=str(scan_start) if provider else start_str,
                end=str(scan_day) if provider else today_str,
                **({"provider": provider, "params": params} if provider else {}),
            )
        except Exception as e:
            if claim and redis:
                _release_claim(redis, claim)
            summary["failed"] += 1
            logger.warning("daily_scan: run failed for %s: %s", entry.get("name"), e)
            continue
        if result is None or (provider is not None and result.status != "success"):
            if claim and redis:
                _release_claim(redis, claim)
            summary["failed"] += 1
            continue
        if claim:
            try:
                owner = _native_read(redis, claim[0])
            except Exception:
                _release_claim(redis, claim)
                summary["failed"] += 1
                continue
            if isinstance(owner, bytes):
                owner = owner.decode()
            if owner != claim[1]:
                summary["failed"] += 1
                continue
        summary["ok"] += 1
        # Trades dated today_str count as fresh signals
        fresh = [
            t
            for t in (result.trades or [])
            if str(getattr(t, "date", "")).startswith(str(scan_day))
        ]
        if fresh:
            summary["with_signal"] += 1
        watch_scope = (
            hashlib.sha256(
                json.dumps(
                    [
                        entry.get("tenant_id", ""),
                        entry.get("user_id", "0"),
                        entry.get("script_sha"),
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            if claim
            else None
        )
        fresh_signals = []
        for t in fresh:
            fresh_signals.append(
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
                            "watch_scope": watch_scope,
                        }
                        if provider
                        else {}
                    ),
                }
            )
        signals.extend(fresh_signals)
        if claim:
            claims.append(
                {
                    "claim": claim,
                    "scope": watch_scope,
                    "script_sha": entry.get("script_sha"),
                    "signals": fresh_signals,
                }
            )
        else:
            legacy_signals.extend(fresh_signals)

    payload = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "signals": signals,
        "summary": summary,
    }
    try:
        r = get_redis_sentinel_client()
        if native_seen:
            accepted = _publish_scoped_scan(
                r, payload, claims, legacy_seen, legacy_signals
            )
            lost = [batch for batch in claims if batch["claim"][0] not in accepted]
            summary["failed"] += len(lost)
            summary["ok"] -= len(lost)
            summary["with_signal"] -= sum(bool(batch["signals"]) for batch in lost)
            payload["signals"] = legacy_signals + [
                signal
                for batch in claims
                if batch["claim"][0] in accepted
                for signal in batch["signals"]
            ]
        else:
            r.set(
                SIGNALS_KEY,
                json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            )
            r.set(LAST_RUN_KEY, payload["generated_at"].encode("utf-8"))
    except Exception as e:
        for batch in claims:
            _release_claim(redis, batch["claim"])
        if native_seen:
            payload["signals"] = []
            payload["persistence_error"] = str(e)
            summary["failed"] += summary["ok"]
            summary["ok"] = 0
            summary["with_signal"] = 0
        logger.warning("daily_scan: persist failed: %s", e)
    return payload


def _publish_scoped_scan(redis, payload, batches, legacy_seen, legacy_signals):
    """Replace only owned native tasks; a native skip never suppresses legacy output."""
    for _ in range(3):
        accepted = [
            batch
            for batch in batches
            if _native_read(redis, batch["claim"][0])
            in (batch["claim"][1], batch["claim"][1].encode())
        ]
        if not legacy_seen and not accepted:
            return set()
        previous_raw = _native_read(redis, SIGNALS_KEY)
        previous = json.loads(previous_raw) if previous_raw else {"signals": []}
        retained = []
        for signal in previous.get("signals", []):
            native = signal.get("market") == "JP"
            if not native and legacy_seen:
                continue
            if native and any(
                signal.get("watch_scope") == batch["scope"]
                or (
                    not signal.get("watch_scope")
                    and batch["script_sha"] is not None
                    and signal.get("script_sha") == batch["script_sha"]
                )
                for batch in accepted
            ):
                continue
            retained.append(signal)
        merged_summary = dict(payload["summary"])
        lost = [batch for batch in batches if batch not in accepted]
        merged_summary["failed"] += len(lost)
        merged_summary["ok"] -= len(lost)
        merged_summary["with_signal"] -= sum(bool(batch["signals"]) for batch in lost)
        merged = {
            **payload,
            "summary": merged_summary,
            "signals": retained
            + legacy_signals
            + [signal for batch in accepted for signal in batch["signals"]],
        }
        published = _native_eval(
            redis,
            "local current=redis.call('GET',KEYS[1]); "
            "if ARGV[2]=='0' then if current then return 0 end "
            "elseif current ~= ARGV[1] then return 0 end; "
            "for i=3,#KEYS do if redis.call('GET',KEYS[i]) ~= ARGV[i+2] then return 0 end end; "
            "redis.call('SET',KEYS[1],ARGV[3]); redis.call('SET',KEYS[2],ARGV[4]); "
            "for i=3,#KEYS do redis.call('SET',KEYS[i],'completed') end; return 1",
            2 + len(accepted),
            SIGNALS_KEY,
            LAST_RUN_KEY,
            *[batch["claim"][0] for batch in accepted],
            previous_raw or "",
            "1" if previous_raw is not None else "0",
            json.dumps(merged, ensure_ascii=False, default=str),
            payload["generated_at"],
            *[batch["claim"][1] for batch in accepted],
        )
        if published:
            return {batch["claim"][0] for batch in accepted}
    raise ValueError("Scan publication changed during all three guarded attempts")


def _release_claim(redis, claim):
    """Only the current scan owner may complete or release its source claim."""
    try:
        _native_eval(
            redis,
            "if redis.call('GET',KEYS[1]) == ARGV[1] then "
            + "return redis.call('DEL',KEYS[1]) "
            + "end return 0",
            1,
            claim[0],
            claim[1],
        )
    except Exception:
        logger.warning("daily_scan: source claim cleanup failed", exc_info=True)


def _native_read(redis, key):
    # The public Sentinel wrapper exposes master pipelines, not Lua eval;
    # ownership and CAS reads must not use its optional replica read path.
    if not callable(getattr(redis, "eval", None)):
        with redis.pipeline() as pipeline:
            return pipeline.get(key).execute()[0]
    return redis.get(key)


def _native_eval(redis, script, count, *arguments):
    if callable(getattr(redis, "eval", None)):
        return redis.eval(script, count, *arguments)
    with redis.pipeline() as pipeline:
        return pipeline.eval(script, count, *arguments).execute()[0]


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
