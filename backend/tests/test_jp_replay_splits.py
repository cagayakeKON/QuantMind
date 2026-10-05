"""Real published split prices through ordinary replay preparation and valuation."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture
DAY = date(2026, 9, 29)
SYMBOL = "72030.JP"


class MemoryAccount:
    """Only cache I/O is replaced; preparation, stops and valuation are real."""

    def __init__(self, market="JP", volume=100, cost=100, available=None):
        self.market = market
        position = {
            "volume": volume,
            "cost": cost,
            "price": cost,
            "market_value": volume * cost,
            "first_buy_date": "2026-09-28",
        }
        if available is not None:
            position["available_volume"] = available
        self.value = {
            "cash": 1234.0,
            "total_asset": 1234.0 + volume * cost,
            "positions": {SYMBOL: position},
        }

    async def get(self):
        return deepcopy(self.value)

    async def unlock(self):
        return {}

    def write(self, value):
        self.value = deepcopy(value)


class EmptySignals:
    async def load_signals_for_date(self, **kwargs):
        return []


def make_runner(snapshot, tmp_path):
    root = tmp_path / "publication"
    import_jquants_snapshot(snapshot, root)
    data = LocalMarketData(hub=QuantJPDataHub(root), market="JP")
    runner = ReplayDayRunner(market_data=data, loader=EmptySignals())

    async def snapshot_only(*args, **kwargs):
        return {}

    runner._write_snapshot = snapshot_only
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "confirm", "skip"])
async def test_split_preserves_nav_and_stop_cost_after_preview(
    snapshot, tmp_path, mode
):
    runner = make_runner(snapshot, tmp_path)
    accounts = MemoryAccount(available=100)
    session = uuid4()
    proposal = await runner.propose_day(
        None, session, DAY, accounts, stop_loss_pct=0.03
    )
    assert not proposal["proposals"]  # A split must not cause a false stop loss.
    assert proposal["account"]["positions"][SYMBOL]["volume"] == 200
    if mode == "auto":
        result = await runner.run_day(
            None, session, DAY, "t", "u", accounts, stop_loss_pct=0.03
        )
    else:
        result = await runner.execute_day(
            None, session, DAY, accounts, [], skip=mode == "skip"
        )
    position = result.account["positions"][SYMBOL]
    assert position["volume"] == position["available_volume"] == 200
    assert position["cost"] == position["price"] == 50
    assert result.account["total_asset"] == 11234
    assert result.account["cash"] == 1234
    assert not result.filled and not result.stop_loss_fills
    # The normal persisted positions restore the adjustment marker too.
    restored = MemoryAccount()
    restored.write(result.account)
    await runner._prepare_day(None, session, DAY, restored)
    assert restored.value["positions"][SYMBOL]["volume"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("via_view", [False, True])
async def test_reverse_split_preserves_cost_basis_and_legacy_available_fallback(
    snapshot, tmp_path, via_view
):
    with duckdb.connect(str(snapshot)) as connection:
        connection.execute(
            "UPDATE research.daily_prices SET O=200,H=201,L=199,C=200,"
            "Va=200*Vo,AdjFactor=2,ExRT='2' WHERE Date='2026-09-29'"
        )
    runner = make_runner(snapshot, tmp_path)
    if via_view:
        runner._market_data._direct_read_ok = False
    accounts = MemoryAccount(volume=1000)
    result = await runner.run_day(None, uuid4(), DAY, "t", "u", accounts)
    position = result.account["positions"][SYMBOL]
    assert position["volume"] == 500 and position["cost"] == 200
    assert "available_volume" not in position  # Preserve the ordinary nil fallback.
    assert result.account["total_asset"] == 101234


@pytest.mark.asyncio
async def test_rights_price_factor_does_not_grant_split_shares(snapshot, tmp_path):
    runner = make_runner(snapshot, tmp_path)
    accounts = MemoryAccount(volume=200, cost=50)
    # The previous split has already been processed at the Sep29 close.
    accounts.value["positions"][SYMBOL]["split_adjusted_date"] = DAY.isoformat()
    result = await runner.run_day(None, uuid4(), date(2026, 9, 30), "t", "u", accounts)
    position = result.account["positions"][SYMBOL]
    assert position["volume"] == 200 and position["cost"] == 50


@pytest.mark.asyncio
async def test_buy_on_ex_date_is_already_on_new_share_basis(snapshot, tmp_path):
    runner = make_runner(snapshot, tmp_path)
    accounts = MemoryAccount(cost=50)
    accounts.value["positions"][SYMBOL]["first_buy_date"] = DAY.isoformat()
    await runner._prepare_day(None, uuid4(), DAY, accounts)
    assert accounts.value["positions"][SYMBOL]["volume"] == 100
    assert accounts.value["positions"][SYMBOL]["cost"] == 50


@pytest.mark.asyncio
async def test_fractional_reverse_split_fails_before_rewriting_account(
    snapshot, tmp_path
):
    with duckdb.connect(str(snapshot)) as connection:
        connection.execute(
            "UPDATE research.daily_prices SET AdjFactor=3,ExRT='2' "
            "WHERE Date='2026-09-29'"
        )
    runner = make_runner(snapshot, tmp_path)
    accounts = MemoryAccount(volume=100)
    original = deepcopy(accounts.value)
    with pytest.raises(RuleDataMissing, match="fractional-share disposition"):
        await runner._prepare_day(None, uuid4(), DAY, accounts)
    assert accounts.value == original


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "HK", "US"])
async def test_other_replay_markets_do_not_apply_japan_events(market):
    accounts = MemoryAccount(market=market)
    original = deepcopy(accounts.value)
    data = SimpleNamespace(
        load_date=lambda *args: {SYMBOL: SimpleNamespace(split_factor=0.5)}
    )
    runner = ReplayDayRunner(market_data=data, loader=EmptySignals())
    await runner._prepare_day(None, uuid4(), DAY, accounts)
    assert accounts.value == original
