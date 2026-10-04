"""Mixed scans use real Redis Lua against UUID keys, never production watch state."""

import json
import os
from types import SimpleNamespace
import uuid

import pytest

from backend.services.engine.strategy_lab import runtime_context
from backend.services.engine.strategy_lab.cron import daily_scan
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.tests.test_jp_lab_round4 import provider as provider_fixture
from backend.tests.test_jp_share_basis_review import native as native_fixture
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture

provider = provider_fixture
native = native_fixture
snapshot = snapshot_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="UUID Redis integration opt-in"
)


class BoundPipeline:
    def __init__(self, scoped):
        self.scoped = scoped
        self.pipeline = scoped.client.pipeline()

    def __enter__(self):
        self.pipeline.__enter__()
        return self

    def __exit__(self, *args):
        return self.pipeline.__exit__(*args)

    def get(self, key):
        self.pipeline.get(self.scoped.key(key))
        return self

    def eval(self, script, count, *args):
        if self.scoped.before_eval:
            self.scoped.before_eval(count, args)
        self.pipeline.eval(
            script, count, *[self.scoped.key(k) for k in args[:count]], *args[count:]
        )
        return self

    def execute(self):
        return self.pipeline.execute()


class ScopedSentinel:
    # Exercise the actual project's public pipeline protocol: it has no eval().
    def __init__(self):
        self.client = get_redis_sentinel_client()
        self.prefix = "qm:jp-review:" + uuid.uuid4().hex + ":"
        self.keys = set()
        self.before_eval = None
        self.deny_claim = False

    def key(self, key):
        assert isinstance(key, str) and key
        physical = self.prefix + key
        self.keys.add(physical)
        return physical

    def pipeline(self):
        return BoundPipeline(self)

    def get(self, key):
        return self.client.get(self.key(key), use_slave=False)

    def set(self, key, value, nx=False, ex=None):
        if nx and self.deny_claim:
            self.client.set(self.key(key), "other worker", ex=60)
            return False
        return self.client.set(self.key(key), value, nx=nx, ex=ex or 60)

    def document(self):
        return json.loads(self.get(daily_scan.SIGNALS_KEY))

    def clean(self):
        assert all(key.startswith(self.prefix) for key in self.keys)
        if self.keys:
            self.client.delete(*self.keys)


@pytest.fixture
def redis():
    scoped = ScopedSentinel()
    try:
        yield scoped
    finally:
        scoped.clean()


def setup_scan(provider, redis, monkeypatch):
    native = {
        "script_sha": "jp-task",
        "user_id": "7",
        "tenant_id": "tenant",
        "code": "def setup(ctx):\n    pass\n",
        "options": {"market": "JP"},
    }
    legacy = {"script_sha": "cn-task", "code": "def setup(ctx):\n    pass\n"}
    entries = [native, legacy]
    monkeypatch.setattr(daily_scan, "list_watch", lambda: entries)
    monkeypatch.setattr(daily_scan, "get_redis_sentinel_client", lambda: redis)

    def context(*, options=None, **kw):
        return (
            (
                {"market": "JP", "data_version": provider.reader.data_version},
                {},
                None,
                provider,
            )
            if options and options.get("market") == "JP"
            else ({}, {}, None, None)
        )

    def run(*a, **kw):
        symbol = "JP72030" if kw.get("provider") else "SH600036"
        return SimpleNamespace(
            status="success",
            trades=[
                SimpleNamespace(
                    date=kw["end"],
                    symbol=symbol,
                    direction="BUY",
                    price=100,
                    qty=100,
                )
            ],
        )

    monkeypatch.setattr(runtime_context, "auxiliary_context", context)
    monkeypatch.setattr(daily_scan, "_run_one", run)
    redis.set(
        daily_scan.SIGNALS_KEY,
        json.dumps(
            {
                "signals": [
                    {
                        "market": "JP",
                        "script_sha": "jp-task",
                        "symbol": "JP72030",
                        "reason": "previous native",
                    },
                    {"symbol": "SH600000", "reason": "previous legacy"},
                ]
            }
        ),
    )
    return entries, native, run


@pytest.mark.parametrize(
    "condition", ["completed", "running", "failure", "empty", "unsafe"]
)
def test_jp_skip_or_failure_never_blocks_fresh_cn_persistence(
    provider, redis, monkeypatch, condition
):
    entries, native, actual = setup_scan(provider, redis, monkeypatch)
    if condition == "completed":
        entries.pop()
        daily_scan.run_daily_scan()
        entries.append({"script_sha": "cn-task", "code": "def setup(ctx):\n    pass\n"})
    elif condition == "running":
        redis.deny_claim = True
    elif condition == "empty":
        native["code"] = ""
    elif condition == "unsafe":
        native["code"] = "import os\n"
    else:

        def failing(*a, **kw):
            if kw.get("provider"):
                raise ValueError("controlled JP failure")
            return actual(*a, **kw)

        monkeypatch.setattr(daily_scan, "_run_one", failing)
    prior_jp = [s for s in redis.document()["signals"] if s.get("market") == "JP"]
    result = daily_scan.run_daily_scan()
    persisted = redis.document()
    assert [s["symbol"] for s in result["signals"]] == ["SH600036"]
    assert persisted["summary"] == result["summary"]
    assert redis.get(daily_scan.LAST_RUN_KEY).decode() == persisted["generated_at"]
    assert [s for s in persisted["signals"] if s.get("market") == "JP"] == prior_jp
    assert [s["symbol"] for s in persisted["signals"] if s.get("market") != "JP"] == [
        "SH600036"
    ]


def test_lost_owner_during_atomic_publish_keeps_new_jp_and_publishes_cn(
    provider, redis, monkeypatch
):
    setup_scan(provider, redis, monkeypatch)
    injected = []
    replacement = {
        "market": "JP",
        "script_sha": "jp-task",
        "symbol": "JP72030",
        "reason": "new owner",
    }

    def interfere(count, args):
        if count > 2 and not injected:
            injected.append(True)
            redis.set(args[2], "new-owner")
            redis.set(daily_scan.SIGNALS_KEY, json.dumps({"signals": [replacement]}))

    redis.before_eval = interfere
    result = daily_scan.run_daily_scan()
    assert (
        injected and result["summary"]["ok"] == 1 and result["summary"]["failed"] == 1
    )
    assert [s["symbol"] for s in result["signals"]] == ["SH600036"]
    assert redis.document()["signals"] == [replacement, result["signals"][0]]
    assert redis.document()["summary"] == result["summary"]


def test_cache_cas_retry_retains_other_watch_while_committing_own_source(
    provider, redis, monkeypatch
):
    setup_scan(provider, redis, monkeypatch)
    injected = []
    other = {
        "market": "JP",
        "script_sha": "other-jp",
        "watch_scope": "other-owner",
        "symbol": "JP67580",
    }

    def interfere(count, args):
        if count > 1 and not injected:
            injected.append(True)
            prior = redis.document()
            prior["signals"].append(other)
            redis.set(daily_scan.SIGNALS_KEY, json.dumps(prior))

    redis.before_eval = interfere
    result = daily_scan.run_daily_scan()
    assert result["summary"]["ok"] == 2 and result["summary"]["failed"] == 0
    assert other in redis.document()["signals"]
    assert sorted(s["symbol"] for s in redis.document()["signals"]) == [
        "JP67580",
        "JP72030",
        "SH600036",
    ]
    claims = [key for key in redis.keys if "qm:lab:scan:source:" in key]
    assert len(claims) == 1
    assert redis.client.get(claims[0], use_slave=False) == b"completed"


def test_native_only_scan_retains_legacy_cache_and_pure_legacy_retains_original_replacement(
    provider, redis, monkeypatch
):
    entries, _, _ = setup_scan(provider, redis, monkeypatch)
    legacy = entries.pop()
    result = daily_scan.run_daily_scan()
    assert result["summary"]["ok"] == 1
    assert {s["symbol"] for s in redis.document()["signals"]} == {"JP72030", "SH600000"}
    prior = redis.get(daily_scan.SIGNALS_KEY)
    stamp = redis.get(daily_scan.LAST_RUN_KEY)
    assert daily_scan.run_daily_scan()["summary"]["skipped"] == 1
    assert redis.get(daily_scan.SIGNALS_KEY) == prior
    assert redis.get(daily_scan.LAST_RUN_KEY) == stamp
    entries[:] = [legacy]
    assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    assert [s["symbol"] for s in redis.document()["signals"]] == ["SH600036"]


def test_failed_native_publication_is_explicit_and_cannot_clear_cached_rows(
    provider, redis, monkeypatch
):
    setup_scan(provider, redis, monkeypatch)
    prior = redis.get(daily_scan.SIGNALS_KEY)

    def fail(count, args):
        if count > 1:
            raise RuntimeError("controlled Redis publish failure")

    redis.before_eval = fail
    result = daily_scan.run_daily_scan()
    assert result["signals"] == [] and result["summary"]["ok"] == 0
    assert result["persistence_error"]
    assert redis.get(daily_scan.SIGNALS_KEY) == prior
    assert redis.get(daily_scan.LAST_RUN_KEY) is None
