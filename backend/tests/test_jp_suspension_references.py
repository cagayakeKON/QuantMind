"""Suspended Japanese securities retain valid limit/valuation price references."""

from datetime import date
import math

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.qlib_data_builder import QlibDataBuilder
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.services.simulation.services.market_rules import japan_price_rules
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture
def suspended_publication(snapshot, tmp_path, monkeypatch, request):
    adjustment = request.param
    rights = "3" if adjustment == 0.9 else "1" if adjustment != 1 else ""
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=90,H=91,L=89,C=90,Va=90000 "
            "WHERE Code='216A0' AND Date='2026-09-28'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,"
            "Vo=NULL,Va=NULL,AdjFactor=?,ExRT=? "
            "WHERE Code='216A0' AND Date='2026-09-29'",
            [adjustment, rights],
        )
        close = 90 * adjustment * 0.9
        conn.execute(
            "UPDATE research.daily_prices SET O=?,H=?,L=?,C=?,Va=? "
            "WHERE Code='216A0' AND Date='2026-09-30'",
            [close, close + 1, close - 1, close, close * 1000],
        )
        # A later action changes cumulative factors, but must cancel out of
        # September's raw price reference and the Qlib price-limit comparison.
        conn.execute(
            "INSERT INTO research.master SELECT DATE '2026-10-01',Code,CoName,"
            "CoNameEn,Mkt,MktNm,S17,S33,S33Nm,ScaleCat,ProdCat "
            "FROM research.master WHERE Code='216A0' AND Date='2026-09-30'"
        )
        conn.execute(
            "INSERT INTO research.daily_prices SELECT DATE '2026-10-01',Code,"
            "O*0.8,H*0.8,L*0.8,C*0.8,Vo,Va*0.8,0.8,'1','0','0' "
            "FROM research.daily_prices WHERE Code='216A0' AND Date='2026-09-30'"
        )
        conn.execute("INSERT INTO research.calendar VALUES ('2026-10-01','1')")
        conn.execute(
            "INSERT INTO research.topix SELECT DATE '2026-10-01',O,H,L,C "
            "FROM research.topix WHERE Date='2026-09-30'"
        )
    root = tmp_path / "published"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root, adjustment


@pytest.mark.parametrize("suspended_publication", [1, 0.5, 0.9], indirect=True)
@pytest.mark.asyncio
async def test_resume_and_valuation_references_use_same_basis_as_qlib(
    suspended_publication, monkeypatch
):
    root, adjustment = suspended_publication
    hub = QuantJPDataHub(root)
    data = LocalMarketData(hub=hub, market="JP")
    scans = []
    original = data._read_partition

    def traced(root, dt, **kwargs):
        symbols = kwargs.get("symbols")
        scans.append((dt, set(symbols) if symbols is not None else None))
        return original(root, dt, **kwargs)

    monkeypatch.setattr(data, "_read_partition", traced)
    # Cache construction for another security must not be poisoned by a
    # suspended security's prior NaN close.
    assert data.load_date(date(2026, 9, 30), ["JP72030"])
    bar = data.get_bar("JP216A0", date(2026, 9, 30))
    assert bar.pre_close == pytest.approx(90 * adjustment * 0.9)
    assert not bar.suspended and bar.open == bar.close == pytest.approx(bar.pre_close)
    assert scans == [(20260930, None), (20260929, None), (20260928, {"216A0.JP"})]
    halted = data.get_bar("JP216A0", date(2026, 9, 29))
    assert halted.suspended
    assert (
        halted.open == halted.high == halted.low == halted.close == halted.volume == 0
    )
    assert halted.pre_close == pytest.approx(90 * adjustment)

    provider = prepare_jp_rd_provider(root)
    values = {}
    for field in ("jp_limit_up", "jp_limit_down", "factor"):
        _, values[field] = QlibDataBuilder._read_bin_file(
            provider / f"features/jp_216a0/{field}.day.bin"
        )
    factor = values["factor"][2]
    assert bar.limit_up == pytest.approx(values["jp_limit_up"][2] / factor)
    assert bar.limit_down == pytest.approx(values["jp_limit_down"][2] / factor)
    assert await SimulationCorporateActionService._load_latest_price(
        None, "JP216A0", as_of=date(2026, 9, 29)
    ) == pytest.approx(halted.pre_close)


def test_normal_jp_reference_resolution_reads_only_two_partitions(
    snapshot, tmp_path, monkeypatch
):
    root = tmp_path / "published"
    import_jquants_snapshot(snapshot, root)
    data = LocalMarketData(hub=QuantJPDataHub(root), market="JP")
    scans = []
    original = data._read_partition

    def traced(root, dt, **kwargs):
        scans.append(dt)
        return original(root, dt, **kwargs)

    monkeypatch.setattr(data, "_read_partition", traced)
    assert data.load_date(date(2026, 9, 30), ["JP72030"])
    assert scans == [20260930, 20260929]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_limit_inputs_raise_explicit_data_error(value):
    with pytest.raises(RuleDataMissing, match="not finite"):
        japan_price_rules(date(2026, 9, 30), value, 100, {})
    with pytest.raises(RuleDataMissing, match="not finite"):
        japan_price_rules(date(2026, 9, 30), 100, value, {})
