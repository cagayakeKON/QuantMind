"""Exact JP price windows through temporary publications and public consumers."""

from contextlib import asynccontextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import stock_terminal as terminal
from backend.services.api.stock_terminal_sources import (
    HubTerminalSource,
    terminal_source,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.local_stock_pool import (
    query_local_stock_pool,
)
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.jp.stock_pool import _snapshot
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture(params=["market_gap", "symbol_gap", "holiday"])
def publication(snapshot, tmp_path, monkeypatch, request):
    with duckdb.connect(str(snapshot)) as conn:
        if request.param == "symbol_gap":
            conn.execute(
                "DELETE FROM research.daily_prices "
                "WHERE Date='2026-09-29' AND Code='72030'"
            )
        else:
            conn.execute("DELETE FROM research.daily_prices WHERE Date='2026-09-29'")
        if request.param == "holiday":
            conn.execute(
                "UPDATE research.calendar SET HolDiv='3' WHERE Date='2026-09-29'"
            )
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return QuantJPDataHub(root), request.param


@pytest.fixture
def client(monkeypatch):
    # Only isolate the signal DB/auth; the real terminal loads the publication.
    class Result:
        def scalar_one_or_none(self):
            return None

        def fetchall(self):
            return []

    class Session:
        async def execute(self, *args, **kwargs):
            return Result()

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(terminal, "get_session", session)
    app = FastAPI()
    app.include_router(terminal.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "fixture"}
    with TestClient(app) as api:
        yield api


def test_imported_pool_and_actual_dsl_preserve_missing_session(publication):
    hub, mode = publication
    current = date(2026, 9, 30)
    frame = _snapshot(hub, current)
    row = frame.loc["72030.JP"]
    if mode == "holiday":
        assert row.return_1d == pytest.approx(-0.5)
        assert row["pct_change"] == pytest.approx(-50)
    else:
        assert pd.isna(row.return_1d) and pd.isna(row["pct_change"])
    for factor, threshold in (("return_1d", -0.4), ("pct_chg", -40)):
        items, day, _ = query_local_stock_pool(
            f"SELECT symbol WHERE {factor} < {threshold}", "JP"
        )
        assert day == current
        assert ("JP72030" in [item.symbol for item in items]) == (mode == "holiday")
    # Nulls do not become neutral returns and match equality-to-zero conditions.
    items, _, _ = query_local_stock_pool("SELECT symbol WHERE return_1d == 0", "JP")
    assert "JP72030" not in [item.symbol for item in items]


def test_terminal_public_list_profile_nulls_and_holiday_returns(publication, client):
    _, mode = publication
    source = HubTerminalSource("JP")
    frame, day = source.universe("2026-09-30")
    assert day == "2026-09-30"
    value = frame.set_index("Symbol").loc["72030.JP", "pct_change"]
    if mode == "holiday":
        assert value == pytest.approx(-50)
    else:
        assert pd.isna(value)
    profile = client.get(
        "/api/v1/stock-terminal/profile",
        params={"symbol": "JP72030", "date": "2026-09-30"},
    )
    listing = client.get(
        "/api/v1/stock-terminal/list",
        params={"market": "JP", "q": "JP72030", "date": "2026-09-30"},
    )
    assert profile.status_code == listing.status_code == 200
    for row in (profile.json()["data"], listing.json()["data"]["items"][0]):
        assert row["close"] == 45
        if mode == "holiday":
            assert row["pct_change"] == pytest.approx(-50)
        else:
            assert row["pct_change"] is None


def test_all_return_windows_use_cash_session_endpoints(snapshot, tmp_path, monkeypatch):
    cash_days = [
        day
        for day in pd.bdate_range(end="2026-09-30", periods=66)
        if day.strftime("%Y-%m-%d") not in {"2026-08-11", "2026-09-23"}
    ]
    windows = (1, 3, 5, 10, 20, 60)
    missing = {cash_days[-window - 1] for window in windows}
    with duckdb.connect(str(snapshot)) as conn:
        for table in ("daily_prices", "master", "calendar", "topix"):
            conn.execute(f"DELETE FROM research.{table}")
        for d, day in enumerate(cash_days):
            conn.execute("INSERT INTO research.calendar VALUES (?, '1')", [day.date()])
            conn.execute(
                "INSERT INTO research.topix VALUES (?,2500,2501,2499,2500)",
                [day.date()],
            )
            for code in ("72030", "216A0"):
                conn.execute(
                    "INSERT INTO research.master VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day.date(),
                        code,
                        "Fixture",
                        "Fixture",
                        "0111",
                        "Prime",
                        "3",
                        "3050",
                        "Industry",
                        "-",
                        "011",
                    ],
                )
                if code == "72030" and day in missing:
                    continue
                price = 100 + d
                conn.execute(
                    "INSERT INTO research.daily_prices VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day.date(),
                        code,
                        price,
                        price + 1,
                        price - 1,
                        price,
                        1000,
                        price * 1000,
                        1,
                        "",
                        "0",
                        "0",
                    ],
                )
        conn.execute(
            "INSERT INTO research.calendar VALUES "
            "('2026-08-11','0'),('2026-09-23','3')"
        )
    root = tmp_path / "long-publication"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    frame = _snapshot(QuantJPDataHub(root), cash_days[-1].date())
    last_price = 100 + len(cash_days) - 1
    for window in windows:
        assert pd.isna(frame.loc["72030.JP", f"return_{window}d"])
        assert frame.loc["216A0.JP", f"return_{window}d"] == pytest.approx(
            last_price / (last_price - window) - 1
        )
        items, _, _ = query_local_stock_pool(
            f"SELECT symbol WHERE return_{window}d > 0", "JP"
        )
        assert [item.symbol for item in items] == ["JP216A0"]
    first = _snapshot(QuantJPDataHub(root), cash_days[0].date())
    assert first[[f"return_{window}d" for window in windows]].isna().all().all()


def test_cn_terminal_keeps_original_partition_semantics(tmp_path, monkeypatch, client):
    detail = tmp_path / "2_base_sector/instrument_detail"
    detail.mkdir(parents=True)
    pd.DataFrame(
        {"Symbol": ["600036.SH"], "Name": ["Fixture"], "rs_hyname": ["Bank"]}
    ).to_parquet(detail / "instrument_list.parquet")
    for day, close in (("20260928", 100), ("20260930", 110)):
        folder = tmp_path / "1_kline_data/daily_unadjusted" / f"dt={day}"
        folder.mkdir(parents=True)
        pd.DataFrame({"symbol": ["600036.SH"], "close": [close]}).to_parquet(
            folder / "data.parquet"
        )
    monkeypatch.setattr(terminal, "_quantdb_dir", lambda: tmp_path)
    assert terminal_source(market="SH") is None
    assert terminal_source(market="US") is None
    frame, day = terminal._rebuild_universe("2026-09-30")
    assert day == "20260930"
    assert frame["pct_change"].iloc[0] == pytest.approx(10)
    response = client.get(
        "/api/v1/stock-terminal/list",
        params={"market": "SH", "q": "600036", "date": "2026-09-30"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"][0]["pct_change"] == pytest.approx(10)
