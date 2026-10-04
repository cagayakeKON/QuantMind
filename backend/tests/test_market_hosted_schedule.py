"""Registered Japan inputs in the existing hosted scheduling/business flow."""

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.services.api.routers.admin import orders as admin
from backend.services.simulation import engine as engine_module
from backend.services.simulation.jp.schedule import open_schedule_context
from backend.services.simulation.services import (
    simulation_hosted_scheduler as scheduler,
)
from backend.services.simulation.services.market_schedule import (
    ScheduleDataUnavailable,
    hosted_schedule_market,
    open_registered_schedule_context,
)

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture(scope="module")
def context():
    return open_schedule_context()


def config(clock="09:00", **overrides):
    return scheduler._normalize_live_trade_config(
        {
            "market": "JP",
            "rebalance_days": 1,
            "enabled_sessions": ["AM", "PM"],
            "sell_time": clock,
            "buy_time": clock,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "day,clock,expected",
    [
        ("2026-10-02", "09:00", "matched"),
        ("2026-10-02", "08:59", "outside_session"),
        ("2026-10-02", "11:30", "outside_session"),
        ("2026-10-02", "12:00", "outside_session"),
        ("2026-10-02", "12:30", "matched"),
        ("2026-10-02", "15:24", "matched"),
        ("2026-10-02", "15:25", "outside_session"),
        ("2026-10-02", "15:30", "outside_session"),
        ("2026-10-03", "09:00", "non_trading_day"),
        ("2026-10-12", "09:00", "non_trading_day"),
        ("2026-12-31", "09:00", "non_trading_day"),
        ("2024-11-01", "15:24", "outside_session"),
        ("2024-11-05", "15:24", "matched"),
    ],
)
def test_real_calendar_and_dated_continuous_hours(context, day, clock, expected):
    now = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=JST)
    result = scheduler._should_trigger(
        now=now.astimezone(timezone.utc),
        live_trade_config=config(clock),
        started_day=None,
        context=context,
    )
    assert result.trade_date == day
    assert result.reason == expected
    assert result.should_trigger is (expected == "matched")


def test_interval_counts_japanese_holiday_sessions(context):
    cfg = config(rebalance_days=3)
    for day, eligible in [(date(2026, 9, 24), False), (date(2026, 9, 28), True)]:
        result = scheduler._should_trigger(
            now=datetime.combine(day, datetime.min.time(), tzinfo=JST)
            + timedelta(hours=9),
            live_trade_config=cfg,
            started_day=date(2026, 9, 18),
            context=context,
        )
        assert result.should_trigger is eligible


def test_weekly_and_enabled_sessions_remain_original_policy(context):
    cfg = config("12:30", schedule_type="weekly", trade_weekdays=["MON"])
    result = scheduler._should_trigger(
        now=datetime(2026, 10, 5, 12, 30, tzinfo=JST),
        live_trade_config=cfg,
        started_day=None,
        context=context,
    )
    assert result.should_trigger and result.phase == "ALL"
    cfg["enabled_sessions"] = ["AM"]
    assert (
        scheduler._should_trigger(
            now=datetime(2026, 10, 5, 12, 30, tzinfo=JST),
            live_trade_config=cfg,
            started_day=None,
            context=context,
        ).reason
        == "outside_session"
    )


def test_utc_start_anchor_uses_japanese_day(context):
    stamp = "2026-10-01T15:30:00Z"
    assert scheduler._parse_started_at(stamp, context=context) == date(2026, 10, 2)
    assert scheduler._parse_started_at(stamp) == date(2026, 10, 1)
    assert scheduler._parse_started_at("bad", context=context) is None


def test_next_trigger_skips_japanese_holiday_and_retains_offset(context):
    nxt = scheduler._next_scheduled_trigger(
        now=datetime(2026, 10, 9, 15, 26, tzinfo=JST),
        live_trade_config=config(),
        started_day=None,
        context=context,
    )
    assert nxt.trade_date == "2026-10-13"
    assert nxt.target_at.isoformat() == "2026-10-13T09:00:00+09:00"
    assert nxt.window_end_at - nxt.target_at == timedelta(seconds=90)


def test_new_market_window_cannot_extend_into_preclosing(context):
    nxt = scheduler._next_scheduled_trigger(
        now=datetime(2026, 10, 2, 15, 23, tzinfo=JST),
        live_trade_config=config("15:24"),
        started_day=None,
        context=context,
    )
    assert nxt.target_at.isoformat() == "2026-10-02T15:24:00+09:00"
    assert nxt.window_end_at.isoformat() == "2026-10-02T15:25:00+09:00"


def test_unavailable_calendar_never_falls_back_to_weekdays(context, monkeypatch):
    outside = context.calendar.last_session.date() + timedelta(days=4)
    with pytest.raises(ScheduleDataUnavailable, match="does not cover"):
        scheduler._should_trigger(
            now=datetime.combine(outside, datetime.min.time(), tzinfo=JST),
            live_trade_config=config(),
            started_day=None,
            context=context,
        )
    with pytest.raises(ScheduleDataUnavailable, match="does not cover"):
        scheduler._next_scheduled_trigger(
            now=datetime.combine(outside, datetime.min.time(), tzinfo=JST),
            live_trade_config=config(),
            started_day=None,
            context=context,
        )
    import exchange_calendars

    def unavailable(*args, **kwargs):
        raise RuntimeError("no calendar")

    monkeypatch.setattr(exchange_calendars, "get_calendar", unavailable)
    with pytest.raises(ScheduleDataUnavailable, match="unavailable"):
        open_schedule_context()


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "FUTURES", "CRYPTO", "bad"])
def test_unregistered_markets_do_not_open_new_calendar(market):
    assert open_registered_schedule_context(market) is None


def test_registered_factory_must_not_silently_return_default_context(monkeypatch):
    from backend.services.simulation.jp import schedule

    monkeypatch.setattr(schedule, "open_schedule_context", lambda: None)
    with pytest.raises(ScheduleDataUnavailable, match="context is unavailable"):
        open_registered_schedule_context("JP")


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"live_trade_config": {"market": "JP"}}, "JP"),
        ({"execution_config": {"market": "JP"}}, "JP"),
        ({"market": "JP"}, "JP"),
        (
            {
                "live_trade_config": {"market": "CN"},
                "execution_config": {"market": "JP"},
            },
            None,
        ),
        ({"live_trade_config": "old malformed config"}, None),
        ({}, None),
    ],
)
def test_registered_market_uses_deployment_context(payload, expected):
    assert hosted_schedule_market(payload) == expected


class RedisMemory:
    def __init__(self, payload):
        self.values = {"trade:active_strategy:test:0007": json.dumps(payload)}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.writes.append(("set", key, value, kwargs))
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        self.writes.append(("delete", key))
        self.values.pop(key, None)

    def eval(self, script, count, key, expected, updated):
        assert count == 1 and "KEEPTTL" in script
        self.writes.append(("eval", key, expected, updated))
        if self.values.get(key) != expected:
            return 0
        self.values[key] = updated
        return 1


def active_payload():
    return {
        "mode": "SIMULATION",
        "strategy_id": "strategy-7",
        "started_at": "2026-10-01T15:30:00Z",
        "execution_config": {"market": "JP"},
        "live_trade_config": config(),
    }


@pytest.mark.asyncio
async def test_registered_cycle_cannot_resolve_cn_model_or_update_ordinary_cash(
    monkeypatch,
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("The original default-model/cash path must not run")

    monkeypatch.setattr(scheduler, "_resolve_hosted_signal_run_id", forbidden)
    monkeypatch.setattr(engine_module.simulation_engine, "run_cycle", forbidden)
    monkeypatch.setenv("SIM_HOSTED_STRICT_SIGNAL_BATCH", "0")
    result = await scheduler.run_simulation_cycle_for_active(
        tenant_id="test",
        user_id="0007",
        strategy_id="strategy-7",
        live_trade_config=config(),
        run_id="test-run",
    )
    assert result == {
        "task_id": "test-run",
        "status": "skipped",
        "error": "registered_market_dated_execution_unavailable",
        "signal_count": 0,
        "order_count": 0,
        "filled_count": 0,
    }


@pytest.mark.asyncio
async def test_original_scheduler_job_flow_uses_jst_and_releases_skipped_lock(
    monkeypatch,
):
    payload = active_payload()
    # The existing lifecycle can keep market in execution_config only.
    payload["live_trade_config"].pop("market")
    client = RedisMemory(payload)
    events = []

    class Jobs:
        @staticmethod
        async def ensure_job(**kwargs):
            events.append(("ensure", kwargs))

        @staticmethod
        async def mark_ready(task):
            events.append(("ready", task))

        @staticmethod
        async def mark_started(task):
            events.append(("started", task))

        @staticmethod
        async def mark_skipped(task, **kwargs):
            events.append(("skipped", task, kwargs))

    monkeypatch.setattr(scheduler, "SimulationRebalanceJobService", Jobs)
    instance = scheduler.SimulationHostedScheduler(SimpleNamespace(client=client))
    result = await instance._process_key(
        "trade:active_strategy:test:0007",
        now=datetime(2026, 10, 2, 0, tzinfo=timezone.utc),
    )
    assert result is False
    assert [item[0] for item in events] == ["ensure", "ready", "started", "skipped"]
    ensure = events[0][1]
    assert ensure["idempotency_key"].endswith(":2026-10-02:ALL")
    # The original active-identity resolver pads this user to eight digits.
    assert ensure["user_id"] == "00000007"
    # Preserve the existing job table's Shanghai wall-clock contract.
    assert ensure["planned_run_at"] == datetime(2026, 10, 2, 8)
    assert (
        events[-1][2]["last_error"] == "registered_market_dated_execution_unavailable"
    )
    assert [item[0] for item in client.writes] == ["set", "delete"]
    assert client.values == {"trade:active_strategy:test:0007": json.dumps(payload)}


@pytest.mark.asyncio
async def test_calendar_failure_in_active_scheduler_has_no_job_or_cache_writes(
    monkeypatch,
):
    client = RedisMemory(active_payload())

    def unavailable(*args):
        raise ScheduleDataUnavailable("unavailable")

    monkeypatch.setattr(scheduler, "open_registered_schedule_context", unavailable)
    instance = scheduler.SimulationHostedScheduler(SimpleNamespace(client=client))
    with pytest.raises(ScheduleDataUnavailable):
        await instance._process_key(
            "trade:active_strategy:test:0007", now=datetime(2026, 10, 2, 9, tzinfo=JST)
        )
    assert client.writes == []


@pytest.mark.asyncio
async def test_common_admin_plans_jp_and_original_market_without_old_timestamp_changes(
    monkeypatch,
):
    class Empty:
        def scalars(self):
            return self

        def mappings(self):
            return self

        def all(self):
            return []

    class DB:
        async def execute(self, *args):
            return Empty()

    @asynccontextmanager
    async def session(**kwargs):
        assert kwargs == {"read_only": True}
        yield DB()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 0, tzinfo=timezone.utc).astimezone(tz)

    jp = active_payload()
    cn = {**active_payload(), "live_trade_config": config("09:30", market="CN")}
    rows = [
        {"tenant_id": "test", "user_id": "0007", "mode": "SIMULATION", "payload": jp},
        {"tenant_id": "test", "user_id": "8", "mode": "SIMULATION", "payload": cn},
    ]
    monkeypatch.setattr(admin, "get_session", session)
    monkeypatch.setattr(admin, "datetime", Clock)
    monkeypatch.setattr(admin, "_ensure_redis", lambda: object())
    monkeypatch.setattr(admin, "iter_active_strategy_payloads", lambda redis: rows)
    result = await admin.list_planned_orders(current_user={})
    by_user = {row["user_id"]: row for row in result["data"]}
    assert by_user["0007"]["planned_at"] == "2026-10-02 09:00:00+09:00"
    assert by_user["0007"]["window_end_at"] == "2026-10-02 09:01:30+09:00"
    # The original Shanghai calendar skips the National Day holiday.
    assert by_user["8"]["planned_at"] == "2026-10-08 09:30:00"
    assert by_user["8"]["window_end_at"] == "2026-10-08 09:31:30"

    def unavailable(market):
        raise ScheduleDataUnavailable("calendar missing")

    monkeypatch.setattr(admin, "open_registered_schedule_context", unavailable)
    result = await admin.list_planned_orders(current_user={})
    assert [row["user_id"] for row in result["data"]] == ["8"]
