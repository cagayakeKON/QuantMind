"""JP consumer integration while preserving original market defaults."""

import json
import os
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy


@pytest.fixture
def default_models_database():
    from backend.shared.database_manager_v2 import DatabaseConfig

    url = DatabaseConfig().get_master_url().replace("+asyncpg", "+psycopg2")
    schema = "jp_default_scan_" + uuid.uuid4().hex
    admin = sqlalchemy.create_engine(url)
    engine = None
    try:
        with admin.begin() as conn:
            conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema}"'))
        engine = sqlalchemy.create_engine(
            url, connect_args={"options": f"-csearch_path={schema}"}
        )
        yield engine
    finally:
        if engine:
            engine.dispose()
        with admin.begin() as conn:
            conn.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in")
@pytest.mark.parametrize("default_market", ["CN", "JP"])
def test_default_model_scan_uses_only_the_standard_sql_default_in_every_market(
    monkeypatch, default_models_database, default_market
):
    from backend.services.engine.inference.gap_backfill import list_default_models

    engine = default_models_database
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "CREATE TABLE qm_user_models (tenant_id TEXT, user_id TEXT, "
                "model_id TEXT, storage_path TEXT, metadata_json JSONB, "
                "is_default BOOLEAN, status TEXT)"
            )
        )
        conn.execute(
            sqlalchemy.text(
                "CREATE UNIQUE INDEX default_per_user ON qm_user_models "
                "(tenant_id, user_id) WHERE is_default=TRUE"
            )
        )
        records = [
            ("CN", default_market == "CN", False, "ready", "cn", "7", "/cn"),
            ("JP", default_market == "JP", False, "ready", "jp", "7", "/jp"),
            ("JP", False, True, "ready", "jp_stale_metadata", "7", "/jp"),
            ("JP", False, False, "ready", "jp_not_default", "7", "/jp"),
            ("US", False, True, "ready", "us_metadata", "7", "/us"),
            ("JP", False, True, "archived", "jp_archived", "7", "/jp"),
            ("JP", False, True, "ready", "jp_other_user", "8", "/jp"),
            ("JP", False, True, "ready", "jp_no_path", "7", ""),
        ]
        for market, default, native_default, status, model, user, path in records:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO qm_user_models VALUES "
                    "('review', :user, :model, :path, :metadata, :default, :status)"
                ),
                {
                    "user": user,
                    "model": model,
                    "path": path,
                    "metadata": json.dumps(
                        {
                            "market": market,
                            "market_default": native_default,
                        }
                    ),
                    "default": default,
                    "status": status,
                },
            )
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: engine)
    rows = list_default_models(tenant_id="review", user_id="7")
    assert {row["model_id"] for row in rows} == {default_market.lower()}


@pytest.mark.asyncio
async def test_jp_calendar_override_precedes_published_session(monkeypatch):
    from backend.services.engine.data_platform import jp_calendar
    from backend.shared.trading_calendar import TradingCalendarService

    service = TradingCalendarService()
    service._find_db_override = AsyncMock(return_value=False)
    native = Mock(return_value=True)
    monkeypatch.setattr(jp_calendar, "is_cash_session", native)
    assert not await service.is_trading_day(
        market="JP",
        trade_date=date(2026, 9, 28),
        tenant_id="review",
        user_id="7",
    )
    native.assert_not_called()
    service._find_db_override.assert_awaited_once_with(
        market="XTKS",
        trade_date=date(2026, 9, 28),
        tenant_id="review",
        user_id="7",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,origin,closed,wanted",
    [
        ("next_trading_day", 28, 29, 30),
        ("prev_trading_day", 30, 29, 28),
    ],
)
async def test_jp_adjacent_days_skip_override_closed_session(
    monkeypatch, method, origin, closed, wanted
):
    from backend.services.engine.data_platform import jp_calendar
    from backend.shared.trading_calendar import TradingCalendarService

    monkeypatch.setattr(
        jp_calendar,
        "cash_sessions",
        lambda day: tuple(date(2026, 9, d) for d in (28, 29, 30)),
    )
    service = TradingCalendarService()
    service._find_db_override = AsyncMock(
        side_effect=lambda **kw: (
            False if kw["trade_date"] == date(2026, 9, closed) else None
        )
    )
    result = await getattr(service, method)(
        market="JP",
        trade_date=date(2026, 9, origin),
        tenant_id="review",
        user_id="7",
    )
    assert result == date(2026, 9, wanted)
    assert service._find_db_override.await_count == 2


@pytest.mark.asyncio
async def test_jp_adjacent_missing_publication_does_not_guess_weekdays(monkeypatch):
    from backend.services.engine.data_platform import jp_calendar
    from backend.shared.trading_calendar import TradingCalendarService

    monkeypatch.setattr(jp_calendar, "cash_sessions", lambda day: (day,))
    service = TradingCalendarService()
    service._find_db_override = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="no enabled next cash session"):
        await service.next_trading_day(
            market="JP",
            trade_date=date(2026, 9, 28),
            tenant_id="review",
            user_id="7",
        )


def test_only_jp_describe_cache_keys_are_versioned(tmp_path):
    from backend.services.engine.inference import script_runner as runner

    runner._describe_cache.clear()
    cn = SimpleNamespace(
        market="CN", data_dir=tmp_path / "cn", describe=Mock(return_value="cn")
    )
    us = SimpleNamespace(
        market="US", data_dir=tmp_path / "us", describe=Mock(return_value="us")
    )
    jp = SimpleNamespace(
        market="JP", data_dir=tmp_path / "jp", describe=Mock(return_value="jp")
    )
    jp2 = SimpleNamespace(
        market="JP", data_dir=tmp_path / "jp2", describe=Mock(return_value="jp2")
    )
    try:
        assert runner._cached_describe(cn, "l1_factors") == "cn"
        # Preserve the original source-only reuse even across non-JP readers.
        assert runner._cached_describe(us, "l1_factors") == "cn"
        us.describe.assert_not_called()
        assert runner._cached_describe(jp, "l1_factors") == "jp"
        assert runner._cached_describe(jp2, "l1_factors") == "jp2"
        assert runner._cached_describe(jp, "l1_factors") == "jp"
        jp.describe.assert_called_once()
    finally:
        runner._describe_cache.clear()


def test_default_calendar_uses_raw_publication_but_explicit_hub_stays_pinned(
    monkeypatch, tmp_path
):
    from backend.services.engine.data_platform import jp_calendar
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )

    day = date(2026, 9, 29)
    raw_path, research_path = tmp_path / "raw-new", tmp_path / "research-old"
    open_raw = Mock(return_value=SimpleNamespace(data_dir=raw_path))
    monkeypatch.setitem(
        LOCAL_MARKET_PROVIDERS, "JP", SimpleNamespace(open_raw=open_raw)
    )
    seen = []

    def calendar(path):
        seen.append(path)
        return frozenset([day]), (day,)

    monkeypatch.setattr(jp_calendar, "_calendar", calendar)
    assert jp_calendar.cash_sessions(day) == (day,)
    assert jp_calendar.cash_sessions(day, SimpleNamespace(data_dir=research_path)) == (
        day,
    )
    assert seen == [raw_path, research_path]
    open_raw.assert_called_once_with()
