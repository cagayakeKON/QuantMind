"""JP daily limits compare the chosen execution quote, using ordinary Qlib fills."""

from collections import defaultdict
import asyncio

import duckdb
import pandas as pd
import pytest
import qlib
from qlib.backtest.decision import Order
from qlib.backtest.position import Position

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.market_backtest_config import (
    configure_market_exchange,
    prepare_market_batch_request,
)
from backend.services.engine.qlib_app.utils.jp_exchange import JpExchange
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.mark.parametrize("split", [1.0, 0.5])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("price", ["open", "close", "vwap"])
@pytest.mark.parametrize("basis", ["adjusted", "raw"])
def test_execution_quote_limits_do_not_block_normal_open_on_intraday_touch(
    snapshot, tmp_path, monkeypatch, split, side, price, basis
):
    base = 90 * split
    bound = base + 30 if side == "buy" else base - 30
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=90,H=91,L=89,C=90,"
            "Vo=10000,Va=900000 WHERE Date='2026-09-28'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=?,H=?,L=?,C=?,Vo=10000,Va=?,"
            "AdjFactor=?,ExRT=?,UL=?,LL=? WHERE Date='2026-09-29'",
            [
                base,
                max(base, bound),
                min(base, bound),
                bound,
                (base + bound) / 2 * 10000,
                split,
                "1" if split != 1 else "",
                "1" if side == "buy" else "0",
                "1" if side == "sell" else "0",
            ],
        )
    root = tmp_path / "quantjp"
    result = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request = QlibBacktestRequest(
        market="JP",
        start_date="2026-09-29",
        end_date="2026-09-29",
        commission=0.00035,
        min_commission=0,
        impact_cost_coefficient=0,
    )
    asyncio.run(prepare_market_batch_request(request))
    provider = prepare_jp_rd_provider(root, price_basis=basis)
    qlib.init(provider_uri=str(provider), region="cn", kernels=1)
    # QlibBacktestRequest currently exposes open/close; the standard Exchange
    # also accepts vwap, which must observe the same raw/adjusted price boundary.
    request.deal_price = price
    config = configure_market_exchange(request, {"kwargs": {"backtest_id": None}})
    exchange = JpExchange(**config["kwargs"])
    day = pd.Timestamp("2026-09-29")
    direction = Order.BUY if side == "buy" else Order.SELL
    tradable = exchange.is_stock_tradable("jp_72030", day, day, direction)
    assert tradable == (price != "close")
    factor = exchange.get_factor("jp_72030", day, day)
    order = Order("jp_72030", 100 / factor, direction, day, day)
    position = Position(
        cash=1_000_000,
        position_dict={"jp_72030": {"amount": 1000 / factor, "price": base * factor}},
    )
    value, cost, _ = exchange.deal_order(
        order, position=position, dealt_order_amount=defaultdict(float)
    )
    if price == "close":
        assert order.deal_amount == 0 and value == cost == 0
    else:
        assert order.deal_amount * factor == pytest.approx(100)
        assert value == pytest.approx(
            100 * (base if price == "open" else (base + bound) / 2)
        )
        assert cost == pytest.approx(value * 0.00035)
    assert result["version"] == request.jp_data_version


@pytest.mark.parametrize("volume", [0, None])
def test_unavailable_daily_volume_still_blocks_both_sides(
    snapshot, tmp_path, monkeypatch, volume
):
    with duckdb.connect(str(snapshot)) as conn:
        if volume is None:
            conn.execute(
                "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,"
                "Vo=NULL,Va=NULL WHERE Date='2026-09-29'"
            )
        else:
            conn.execute(
                "UPDATE research.daily_prices SET Vo=0,Va=0 WHERE Date='2026-09-29'"
            )
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request = QlibBacktestRequest(
        market="JP", start_date="2026-09-29", end_date="2026-09-29"
    )
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    config = configure_market_exchange(request, {"kwargs": {"backtest_id": None}})
    exchange = JpExchange(**config["kwargs"])
    day = pd.Timestamp("2026-09-29")
    assert not exchange.is_stock_tradable("jp_72030", day, day, Order.BUY)
    assert not exchange.is_stock_tradable("jp_72030", day, day, Order.SELL)


def test_old_limit_cache_is_never_reused_or_modified(snapshot, tmp_path):
    root = tmp_path / "quantjp"
    publication = import_jquants_snapshot(snapshot, root)
    old = root / ".rd_cache" / publication["version"] / "qlib_v3"
    old.mkdir(parents=True)
    marker = old / "old_limit_contract.txt"
    marker.write_text("intraday-touch flags", encoding="utf-8")
    current = prepare_jp_rd_provider(root)
    assert current.name == "qlib_v4"
    assert marker.read_text() == "intraday-touch flags"
    assert (current / "features/jp_72030/jp_limit_up.day.bin").is_file()
    assert not (current / "features/jp_72030/jp_limit_buy.day.bin").exists()


def test_split_during_suspension_changes_next_session_limit_reference(
    snapshot, tmp_path, monkeypatch
):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=90,H=91,L=89,C=90,AdjFactor=1 "
            "WHERE Date='2026-09-28'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,"
            "Vo=NULL,Va=NULL,AdjFactor=0.5,ExRT='1' WHERE Date='2026-09-29'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=45,H=75,L=45,C=75,"
            "Vo=10000,Va=600000,AdjFactor=1,ExRT='',UL='0',LL='0' "
            "WHERE Date='2026-09-30'"
        )
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request = QlibBacktestRequest(
        market="JP", start_date="2026-09-29", end_date="2026-09-30"
    )
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    config = configure_market_exchange(request, {"kwargs": {"backtest_id": None}})
    opening = JpExchange(**config["kwargs"])
    suspended = pd.Timestamp("2026-09-29")
    resumed = pd.Timestamp("2026-09-30")
    assert not opening.is_stock_tradable("jp_72030", suspended, suspended, Order.BUY)
    assert opening.is_stock_tradable("jp_72030", resumed, resumed, Order.BUY)
    request.deal_price = "close"
    config = configure_market_exchange(request, {"kwargs": {"backtest_id": None}})
    closing = JpExchange(**config["kwargs"])
    # UL=0 does not override the true 45 + 30 price boundary.
    assert not closing.is_stock_tradable("jp_72030", resumed, resumed, Order.BUY)


@pytest.mark.parametrize("close", [100, 120, 60, None])
def test_factor_evaluation_uses_real_templates_and_native_qlib_limit_rules(
    snapshot, tmp_path, monkeypatch, close
):
    from pathlib import Path
    import rdagent
    import json
    import yaml
    from qlib.backtest import get_exchange
    from backend.services.engine.rd_agent.experiment_config import (
        compile_factor_template,
    )
    from backend.services.engine.rd_agent.market_adapters.japan import JapanAdapter
    from backend.services.engine.rd_agent.market_adapters.base import (
        BacktestConfig,
        DataConfig,
    )
    from backend.tests.test_rd_market_experiment_config import CONTEXT

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=90,H=91,L=89,C=90,AdjFactor=1 "
            "WHERE Date='2026-09-28'"
        )
        if close is None:
            conn.execute(
                "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,"
                "Vo=NULL,Va=NULL,AdjFactor=1,ExRT='',UL='0',LL='0' "
                "WHERE Date='2026-09-29'"
            )
        else:
            conn.execute(
                "UPDATE research.daily_prices SET O=90,H=120,L=60,C=?,"
                "Vo=10000,Va=900000,AdjFactor=1,ExRT='',UL='1',LL='1' "
                "WHERE Date='2026-09-29'",
                [close],
            )
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    prepare_jp_rd_provider(root)
    adapter = JapanAdapter()
    # Match the subprocess boundary and subsequent qrun YAML transport.
    settings = json.loads(json.dumps(adapter.get_research_config("all")))
    data, costs = DataConfig(**settings["data"]), BacktestConfig(**settings["backtest"])
    # Public experiment fees must survive the registered Exchange configuration.
    costs.commission_rate = 0.0025
    qlib.init(provider_uri=data.provider_uri, region=costs.region, kernels=1)
    templates = (
        Path(rdagent.__file__).parent / "scenarios/qlib/experiment/factor_template"
    )
    day = pd.Timestamp("2026-09-29")
    for template in templates.glob("*.yaml"):
        context = {**CONTEXT, "test_start": "2026-09-29", "test_end": "2026-09-29"}
        config = compile_factor_template(
            template.read_text(), context, data, costs, benchmark="jp_topix"
        )
        config = yaml.safe_load(yaml.safe_dump(config))
        kwargs = config["port_analysis_config"]["backtest"]["exchange_kwargs"]
        exchange = get_exchange(start_time=day, end_time=day, **kwargs)
        assert isinstance(exchange, JpExchange)
        assert exchange.trading_units == {}
        for direction, limited in (
            (Order.BUY, close in {None, 120}),
            (Order.SELL, close in {None, 60}),
        ):
            assert exchange.is_stock_tradable("jp_72030", day, day, direction) == (
                not limited
            )
            factor = exchange.get_factor("jp_72030", day, day)
            order = Order("jp_72030", 100 / factor, direction, day, day)
            position = Position(
                cash=1_000_000,
                position_dict={"jp_72030": {"amount": 1000, "price": 90}},
            )
            value, fee, _ = exchange.deal_order(
                order, position=position, dealt_order_amount=defaultdict(float)
            )
            if limited:
                assert value == fee == order.deal_amount == 0
            else:
                assert order.deal_amount > 0 and value > 0
                assert fee == pytest.approx(value * costs.commission_rate)


@pytest.mark.parametrize("sourced_unit", [None, 1000])
def test_factor_exchange_preserves_historical_unit_requirement(
    snapshot, tmp_path, monkeypatch, sourced_unit
):
    from pathlib import Path
    import json
    import yaml
    import rdagent
    from qlib.backtest import get_exchange
    from backend.services.engine.rd_agent.experiment_config import (
        compile_factor_template,
    )
    from backend.services.engine.rd_agent.market_adapters.base import (
        BacktestConfig,
        DataConfig,
    )
    from backend.services.engine.rd_agent.market_adapters.japan import JapanAdapter
    from backend.tests.test_rd_market_experiment_config import CONTEXT

    with duckdb.connect(str(snapshot)) as conn:
        for table in ("master", "daily_prices", "calendar", "topix"):
            conn.execute(
                f"UPDATE research.{table} SET Date=DATE '2016-09-26'"
                "+CAST(Date-DATE '2026-09-28' AS INTEGER)"
            )
    if sourced_unit:
        units = tmp_path / "units.csv"
        units.write_text(
            "symbol,valid_from,valid_to,lot_size,source\n"
            "JP72030,2016-09-26,2016-09-28,1000,fixture source\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    prepare_jp_rd_provider(root)
    adapter = JapanAdapter()
    settings = json.loads(json.dumps(adapter.get_research_config("all")))
    data, costs = DataConfig(**settings["data"]), BacktestConfig(**settings["backtest"])
    qlib.init(provider_uri=data.provider_uri, region=costs.region, kernels=1)
    template = (
        Path(rdagent.__file__).parent
        / "scenarios/qlib/experiment/factor_template/conf_baseline.yaml"
    )
    context = {**CONTEXT, "test_start": "2016-09-27", "test_end": "2016-09-27"}
    config = compile_factor_template(
        template.read_text(), context, data, costs, benchmark="jp_topix"
    )
    config = yaml.safe_load(yaml.safe_dump(config))
    kwargs = config["port_analysis_config"]["backtest"]["exchange_kwargs"]
    exchange = get_exchange(**kwargs)
    if sourced_unit:
        assert exchange._lot("jp_72030", "2016-09-27") == 1000
    else:
        with pytest.raises(ValueError, match="Historical JP trading unit"):
            exchange.get_factor(
                "jp_72030", pd.Timestamp("2016-09-27"), pd.Timestamp("2016-09-27")
            )
