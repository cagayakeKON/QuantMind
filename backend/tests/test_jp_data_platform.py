"""JP code, immutable import and Qlib tests with historical corporate actions."""

from datetime import date
import hashlib
import json

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.market_hub import get_hub_for_market
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.qlib_data_builder import QlibDataBuilder
from backend.services.simulation.services.market_rules import (
    Market,
    infer_market,
    rules_for,
)
from backend.shared.stock_pool.normalize import (
    is_valid_symbol,
    normalize_to_qlib,
    to_api_symbol,
    to_storage_symbol,
)
from backend.shared.stock_utils import StockCodeUtil


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "source/jquants.duckdb"
    path.parent.mkdir()
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA research")
        conn.execute(
            "CREATE TABLE research.master (Date DATE, Code VARCHAR, CoName VARCHAR, "
            "CoNameEn VARCHAR, Mkt VARCHAR, MktNm VARCHAR, S17 VARCHAR, "
            "S33 VARCHAR, S33Nm VARCHAR, ScaleCat VARCHAR, ProdCat VARCHAR)"
        )
        for day in ("2026-09-28", "2026-09-29", "2026-09-30"):
            for code, category in (
                ("72030", "011"),
                ("216A0", "011"),
                ("13250", "014"),
            ):
                conn.execute(
                    "INSERT INTO research.master VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        "トヨタ",
                        "Toyota",
                        "0111",
                        "Prime",
                        "3",
                        "3050",
                        "Transportation",
                        "TOPIX Core30",
                        category,
                    ],
                )
        conn.execute(
            "INSERT INTO research.master VALUES ('2026-09-28','13370','Retired',"
            "'Retired','0111','Prime','3','3050','Transportation','-','011')"
        )
        conn.execute(
            "CREATE TABLE research.daily_prices (Date DATE, Code VARCHAR, "
            "O DOUBLE,H DOUBLE,L DOUBLE,C DOUBLE,Vo DOUBLE,Va DOUBLE,"
            "AdjFactor DOUBLE,ExRT VARCHAR,UL VARCHAR,LL VARCHAR)"
        )
        for day, price, factor, rights in (
            ("2026-09-28", 100, 1, ""),
            ("2026-09-29", 50, 0.5, "1"),
            ("2026-09-30", 45, 0.9, "3"),
        ):
            for code in ("72030", "216A0", "13250"):
                conn.execute(
                    "INSERT INTO research.daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        price,
                        price + 1,
                        price - 1,
                        price,
                        1000,
                        price * 1000,
                        factor,
                        rights,
                        "0",
                        "0",
                    ],
                )
        conn.execute(
            "INSERT INTO research.daily_prices VALUES "
            "('2026-09-28','13370',10,10,10,10,1000,10000,1,'','0','0')"
        )
        conn.execute("CREATE TABLE research.calendar (Date DATE, HolDiv VARCHAR)")
        conn.execute(
            "INSERT INTO research.calendar VALUES "
            "('2026-09-28','1'),('2026-09-29','1'),('2026-09-30','1'),"
            "('2026-10-03','0'),('2026-10-12','3')"
        )
        conn.execute(
            "CREATE TABLE research.topix (Date DATE,O DOUBLE,H DOUBLE,L DOUBLE,C DOUBLE)"
        )
        conn.execute(
            "INSERT INTO research.topix VALUES "
            "('2026-09-28',2500,2501,2499,2500),"
            "('2026-09-29',2510,2511,2509,2510),"
            "('2026-09-30',2520,2521,2519,2520)"
        )
    return path


@pytest.mark.parametrize(
    "value", ["7203", "72030", "JP72030", "72030.JP", "7203.T", "jp_72030"]
)
def test_jp_numeric_aliases_are_contextual(value):
    assert StockCodeUtil.to_jp_code(value) == "72030"
    assert to_storage_symbol(value, "JP") == "72030.JP"
    assert to_api_symbol(value, "JP") == "JP72030"
    assert normalize_to_qlib(value, "JP") == "jp_72030"


@pytest.mark.parametrize("value", ["216A", "216A0", "JP216A0", "216A0.JP", "jp_216a0"])
def test_jp_alphanumeric_codes(value):
    assert StockCodeUtil.to_jp_code(value) == "216A0"
    assert StockCodeUtil.to_qlib(value, market="JP") == "jp_216a0"
    assert is_valid_symbol(value, "JP")


def test_no_hk_ambiguity_or_security_class_loss():
    assert StockCodeUtil.to_prefix("00700") == "00700"
    assert to_storage_symbol("00700", "HK") == "0700.HK"
    assert StockCodeUtil.to_jp_code("72031") == "72031"
    assert StockCodeUtil.to_prefix("600036.SH") == "SH600036"
    assert StockCodeUtil.to_qlib("000001.SZ") == "sz000001"
    assert not is_valid_symbol("600036.SH", "JP")
    with pytest.raises(ValueError):
        StockCodeUtil.to_jp_code("72030'; DROP TABLE stocks")


def test_jp_market_selection():
    for symbol in ("JP72030", "72030.JP", "jp_216a0", "216A.T"):
        assert infer_market(symbol) is Market.JP
    rules = rules_for("JP")
    assert rules.currency == "JPY" and rules.has_price_limit
    assert not rules.t_plus_1
    assert get_hub_for_market("NO_SUCH_MARKET") is None


def test_import_preserves_source_and_separates_adjustments(snapshot, tmp_path):
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    destination = tmp_path / "quantjp"
    report = import_jquants_snapshot(snapshot, destination)
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
    assert report["rows"] == 7
    hub = QuantJPDataHub(destination)
    raw = hub.fetch_daily_kline("JP72030", adjust="none")
    adjusted = hub.fetch_daily_kline("72030.JP")
    assert raw["close"].tolist() == [100, 50, 45]
    assert adjusted["close"].tolist() == pytest.approx([45, 45, 45])
    assert adjusted["volume"].tolist() == [2000, 1000, 1000]
    assert raw["amount"].tolist() == adjusted["amount"].tolist()
    assert hub.fetch_daily_kline("13250", adjust="none").empty
    assert hub.fetch_stock_list()["stock_name"].iloc[0] == "トヨタ"
    assert set(hub.fetch_calendar()["holiday_division"]) == {"1"}
    assert "13370.JP" in hub.fetch_instrument_periods()["symbol"].tolist()
    assert "13370.JP" not in hub.fetch_stock_list()["symbol"].tolist()
    assert hub.fetch_index_kline("TOPIX")["close"].tolist() == [2500, 2510, 2520]


def test_failed_import_does_not_publish(snapshot, tmp_path):
    destination = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, destination)
    pointer = (destination / "current.json").read_bytes()
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.daily_prices SET AdjFactor=0 WHERE Code='72030'")
    with pytest.raises(ValueError, match="adjustment"):
        import_jquants_snapshot(snapshot, destination)
    assert (destination / "current.json").read_bytes() == pointer
    assert (
        QuantJPDataHub(destination)
        .fetch_daily_kline("72030", adjust="raw")["close"]
        .iloc[0]
        == 100
    )


def test_qlib_preserves_factors_and_delisted_instruments(snapshot, tmp_path):
    destination = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, destination)
    builder = QlibDataBuilder.for_market("JP", destination, tmp_path / "qlib")
    report = builder.build_all()
    assert report["features"] == 3 and report["skipped"] == 0
    assert report["calendar"] == 3
    instruments = (builder.qlib_dir / "instruments/all.txt").read_text()
    assert "jp_13370\t2026-09-28\t2026-09-28" in instruments
    assert "jp_topix" not in instruments  # Benchmark is not a tradable stock universe.
    assert "jp_topix" in (builder.qlib_dir / "instruments/indices.txt").read_text()
    root = builder.qlib_dir / "features/jp_72030"
    _, close = builder._read_bin_file(root / "close.day.bin")
    _, factor = builder._read_bin_file(root / "factor.day.bin")
    _, volume_factor = builder._read_bin_file(root / "volume_factor.day.bin")
    _, vwap = builder._read_bin_file(root / "vwap.day.bin")
    assert vwap.tolist() == pytest.approx([45, 45, 45])
    assert (close / factor).tolist() == pytest.approx([100, 50, 45])
    assert volume_factor.tolist() == pytest.approx([0.5, 1, 1])
    assert (builder.qlib_dir / "features/jp_topix/close.day.bin").is_file()
    assert builder._to_qdb_symbol("jp_216a0") == "216A0.JP"


def test_publication_path_cannot_escape(tmp_path):
    root = tmp_path / "quantjp"
    root.mkdir()
    (root / "current.json").write_text(json.dumps({"path": "../somewhere"}))
    with pytest.raises(ValueError, match="escapes"):
        _ = QuantJPDataHub(root).data_dir


def test_unpublished_staging_is_not_available(tmp_path):
    root = tmp_path / "quantjp"
    (root / "versions/.staging-incomplete").mkdir(parents=True)
    assert not QuantJPDataHub(root).available


def test_date_bounds_do_not_change_adjustment_anchor(snapshot, tmp_path):
    destination = tmp_path / "quantjp"
    import_jquants_snapshot(
        snapshot, destination, start=date(2026, 9, 28), end=date(2026, 9, 28)
    )
    adjusted = QuantJPDataHub(destination).fetch_daily_kline("72030")
    assert adjusted["close"].tolist() == pytest.approx([45])


def test_legacy_simulator_cannot_bypass_jp_cash_rules():
    from backend.services.simulation.services.local_market_data import LocalMarketData

    with pytest.raises(NotImplementedError, match="cash-account"):
        LocalMarketData(market="JP")


def test_jp_factor_reader_resolves_published_generation(
    snapshot, tmp_path, monkeypatch
):
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        QuantDBFactorReader,
        market_data_dir,
    )

    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    expected = QuantJPDataHub(root).data_dir
    assert market_data_dir("JP") == expected
    assert QuantDBFactorReader(market="JP").data_dir == expected
    assert QuantDBFactorReader(root, market="JP").data_dir == expected
