"""Dated JP readers share the local daily contract without enabling execution."""

from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.jp import data as jp_data
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.services import market_execution_data as resolver
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture

snapshot = snapshot_fixture


@pytest.fixture
def published(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    return root


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "FUTURES", "CRYPTO"])
def test_existing_market_reader_identity_and_call_are_preserved(monkeypatch, market):
    reader = object()
    calls = []

    def original(**kwargs):
        calls.append(kwargs)
        return reader

    monkeypatch.setattr(resolver, "get_local_market_data", original)
    assert resolver.open_market_execution_data(market) is reader
    assert calls == [{"market": market or "CN"}]


def test_registered_factory_is_optional_and_the_resolver_uses_it(monkeypatch):
    calls = []
    reader = object()
    provider = replace(LOCAL_MARKET_PROVIDERS["JP"], execution_data_factory="test.open")
    monkeypatch.setitem(LOCAL_MARKET_PROVIDERS, "JP", provider)
    monkeypatch.setattr(
        resolver,
        "import_module",
        lambda module: SimpleNamespace(
            open=lambda version: calls.append(version) or reader
        ),
    )
    assert (
        resolver.open_market_execution_data("JP", data_version="saved-publication")
        is reader
    )
    assert calls == ["saved-publication"]
    monkeypatch.setitem(
        LOCAL_MARKET_PROVIDERS, "JP", replace(provider, execution_data_factory=None)
    )
    with pytest.raises(ValueError, match="not registered"):
        resolver.open_market_execution_data("JP", data_version="saved-publication")


def test_registered_reader_uses_raw_prices_full_codes_and_dated_master(published):
    reader = resolver.open_market_execution_data("JP")
    version = reader.data_version
    assert (
        resolver.open_market_execution_data("JP", data_version=version).data_version
        == version
    )
    bars = reader.load_date(date(2026, 9, 29))
    assert set(bars) == {"72030.JP", "216A0.JP"}
    assert bars["72030.JP"].open == bars["72030.JP"].close == 50
    assert bars["72030.JP"].volume == 1000
    assert bars["72030.JP"].amount == 50000
    assert bars["72030.JP"].lot_size == 100
    assert reader.get_bar("jp_216a0", date(2026, 9, 29)) == bars["216A0.JP"]
    assert reader.get_bar("JP13370", date(2026, 9, 29)) is None
    assert "13370.JP" in reader.load_date(date(2026, 9, 28))
    assert reader.latest_trade_date() == date(2026, 9, 30)
    assert reader.latest_trade_date(date(2026, 9, 29)) == date(2026, 9, 29)
    assert reader.latest_trade_date(date(2026, 9, 27)) is None
    with pytest.raises(RuleDataMissing, match="Exact dated"):
        reader.load_date(date(2026, 10, 1))
    assert reader.data_version == version


def test_reader_and_matcher_share_publication_rules_and_volume(published):
    reader = resolver.open_market_execution_data("JP")
    day = date(2026, 9, 29)
    bar = reader.get_bar("JP72030", day)
    cfg = MatchConfig(
        price_mode="open", slippage_bps=0, commission_rate=Decimal(".00025")
    )
    result = match_order(
        "buy", 100, bar, cfg, rules=reader.matching_rules("72030.JP", day)
    )
    assert result.success and result.fill_price == Decimal(50)
    assert result.commission == Decimal(2)
    assert result.stamp_duty == result.transfer_fee == 0
    with pytest.raises(ValueError, match="observed daily volume"):
        match_order(
            "buy",
            100,
            bar,
            cfg,
            rules=reader.matching_rules("JP72030", day, used_volume=1000),
        )


def test_open_reader_keeps_its_version_without_reopening_current(
    published, monkeypatch
):
    reader = resolver.open_market_execution_data("JP")
    pinned = reader.data_version
    monkeypatch.setattr(
        jp_data,
        "QuantJPDataHub",
        lambda *args: (_ for _ in ()).throw(AssertionError("CURRENT read")),
    )
    assert reader.get_bar("JP72030", date(2026, 9, 29)).close == 50
    assert reader.data_version == pinned


@pytest.mark.parametrize("version", ["../..", "../missing", "not-a-publication"])
def test_factory_rejects_invalid_saved_publication(published, version):
    with pytest.raises(RuleDataMissing, match="Pinned JP data version"):
        resolver.open_market_execution_data("JP", data_version=version)


def test_historical_reader_never_guesses_units():
    day = date(2017, 1, 4)
    reader = jp_data.JPExecutionData.__new__(jp_data.JPExecutionData)
    raw = {"open": 100, "close": 100, "volume": 10000, "amount": 1000000}
    metadata = {"product_category": "011", "scale_category": "-"}
    reader.day = lambda day, symbols: ({"JP72030": raw}, {"JP72030": metadata})
    with pytest.raises(RuleDataMissing, match="Historical trading unit"):
        reader.get_bar("JP72030", day)
    metadata["lot_size"] = 1000
    assert reader.get_bar("JP72030", day).lot_size == 1000


@pytest.mark.parametrize("used_volume", [-1, 1.5, True, None])
def test_new_reader_rejects_invalid_liquidity_usage(used_volume):
    reader = jp_data.JPExecutionData.__new__(jp_data.JPExecutionData)
    with pytest.raises(ValueError, match="nonnegative integer"):
        reader.matching_rules("JP72030", date(2026, 9, 29), used_volume=used_volume)


def test_missing_raw_bar_values_are_not_replaced_by_a_fill():
    day = date(2026, 9, 29)
    reader = jp_data.JPExecutionData.__new__(jp_data.JPExecutionData)
    raw = {"open": None, "close": 50, "volume": 1000}
    metadata = {"product_category": "011", "scale_category": "-"}
    reader.day = lambda day, symbols: ({"JP72030": raw}, {"JP72030": metadata})
    bar = reader.get_bar("JP72030", day)
    assert bar.suspended
    result = match_order(
        "buy",
        100,
        bar,
        MatchConfig(price_mode="open"),
        rules=reader.matching_rules("JP72030", day),
    )
    assert not result.success and result.reason == "SUSPENDED"
    with pytest.raises(ValueError, match="no forward-filled execution"):
        reader.matching_rules("JP72030", day).validate("buy", 100, bar)
    assert raw["open"] is None


def test_generic_jp_simulator_guard_stays_closed():
    with pytest.raises(NotImplementedError, match="dated cash-account"):
        LocalMarketData(market="JP")


def test_projection_matches_former_daily_contract_except_dated_unit():
    raw = {"open": Decimal("123.45"), "close": Decimal("124.5"), "volume": 10000}
    result = jp_data.to_daily_bar(date(2017, 1, 4), "JP72030", raw, {"lot_size": 1000})
    assert asdict(result) == {
        "symbol": "72030.JP",
        "trade_date": date(2017, 1, 4),
        "open": Decimal("123.45"),
        "high": 0,
        "low": 0,
        "close": Decimal("124.5"),
        "volume": 10000,
        "amount": 0,
        "vwap": 0,
        "pre_close": 0,
        "limit_up": float("inf"),
        "limit_down": 0,
        "is_st": False,
        "suspended": False,
        "lot_size": 1000,
    }
