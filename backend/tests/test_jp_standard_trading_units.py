"""Dated board lots from immutable publication through the ordinary JP adapter."""

from datetime import date
import json

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.services.ashare_matcher import match_order
from backend.services.simulation.services.local_market_data import (
    DailyBar,
    LocalMarketData,
)
from backend.services.simulation.services.market_rules import japan_bar_rules
from backend.services.simulation.replay.router import _match_config_from_params
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture
HISTORICAL_DAY = date(2017, 9, 12)


@pytest.fixture
def historical_publication(snapshot, tmp_path, monkeypatch):
    with duckdb.connect(str(snapshot)) as connection:
        for table in ("master", "daily_prices", "calendar", "topix"):
            connection.execute(
                f"UPDATE research.{table} SET Date=DATE '2017-09-11' + "
                "CAST(Date-DATE '2026-09-28' AS INTEGER)"
            )
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    root = tmp_path / "published-jp"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))

    def publish(rows=None):
        if rows is not None:
            units = tmp_path / "units.csv"
            units.write_text(
                "symbol,valid_from,valid_to,lot_size,source\n" + rows,
                encoding="utf-8",
            )
            monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
        import_jquants_snapshot(snapshot, root)
        return root

    return publish


@pytest.mark.parametrize(
    "rows,expected",
    [
        ("JP72030,2017-09-11,2017-09-12,1000,exchange archive\n", 1000),
        (None, None),
        ("JP216A0,2017-09-11,2017-09-13,1000,exchange archive\n", None),
        ("JP72030,2017-09-11,2017-09-11,1000,exchange archive\n", None),
    ],
)
def test_historical_raw_adapter_resolves_exact_symbol_and_date(
    historical_publication, rows, expected
):
    root = historical_publication(rows)
    data = LocalMarketData(
        market="JP"
    )  # Same raw-publication factory as live simulation.
    assert "lot_size" not in data._hub.fetch_stock_list(HISTORICAL_DAY).columns
    if expected is None:
        with pytest.raises(RuleDataMissing, match="Historical JP trading unit"):
            data.get_bar("JP72030", HISTORICAL_DAY)
        with pytest.raises(RuleDataMissing, match="Historical JP trading unit"):
            data.load_date(HISTORICAL_DAY, ["JP72030"])
    else:
        bar = data.get_bar("JP72030", HISTORICAL_DAY)
        assert bar.lot_size == expected
        assert data.load_date(HISTORICAL_DAY, ["JP72030"])["72030.JP"].lot_size == 1000
        fill = match_order(
            "buy", 1200, bar, _match_config_from_params({"market": "JP"})
        )
        assert fill.success and fill.fill_quantity == 1000
        pinned = LocalMarketData(hub=QuantJPDataHub(root), market="JP")
        assert pinned.get_bar("JP72030", HISTORICAL_DAY).lot_size == 1000


@pytest.mark.parametrize("fault", ["digest", "escape"])
def test_historical_units_publication_integrity_is_enforced(
    historical_publication, tmp_path, fault
):
    root = historical_publication(
        "JP72030,2017-09-11,2017-09-13,1000,exchange archive\n"
    )
    from backend.services.engine.data_platform.jp_publication import publication_path

    published = publication_path(root, raw=True)
    manifest = json.loads((published / "manifest.json").read_text("utf-8"))
    if fault == "digest":
        (published / manifest["trading_units"]["path"]).write_text("tampered")
    else:
        manifest["trading_units"]["path"] = "../outside.csv"
        (published / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="integrity validation"):
        LocalMarketData(market="JP").load_date(HISTORICAL_DAY, ["JP72030"])


def test_dated_rules_and_bare_bar_cannot_assume_historical_hundred_share_lot():
    with pytest.raises(RuleDataMissing, match="Historical JP trading unit"):
        japan_bar_rules(HISTORICAL_DAY, 50, 50, {})
    assert japan_bar_rules(date(2018, 10, 1), 50, 50, {})[2] == 100
    bar = DailyBar(
        symbol="JP72030",
        trade_date=HISTORICAL_DAY,
        open=50,
        high=51,
        low=49,
        close=50,
        volume=10000,
        amount=500000,
        vwap=50,
        pre_close=50,
        limit_up=100,
        limit_down=20,
        is_st=False,
        suspended=False,
    )
    fill = match_order("buy", 100, bar, _match_config_from_params({"market": "JP"}))
    assert not fill.success and "Historical JP trading unit" in fill.reason
    bar.trade_date = date(2018, 10, 1)
    assert match_order(
        "buy", 100, bar, _match_config_from_params({"market": "JP"})
    ).success


def test_existing_raw_adapter_refreshes_units_when_publication_advances(
    historical_publication,
):
    root = historical_publication(
        "JP72030,2017-09-11,2017-09-13,1000,exchange archive\n"
    )
    research_pointer = (root / "current.json").read_bytes()
    data = LocalMarketData(market="JP")
    assert data.get_bar("JP72030", HISTORICAL_DAY).lot_size == 1000
    historical_publication(
        "JP72030,2017-09-11,2017-09-13,100,exchange archive correction\n"
    )
    assert (root / "current.json").read_bytes() == research_pointer
    assert data.get_bar("JP72030", HISTORICAL_DAY).lot_size == 100
