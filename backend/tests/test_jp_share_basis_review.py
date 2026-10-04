"""Real Qlib/SDK decisions preserve dated raw-share units across split boundaries."""

from datetime import date
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from qlib.backtest.decision import Order

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.dated_strategy import (
    DatedStrategyRunner,
    build_dated_strategy,
)
from backend.services.engine.qlib_app.services.dated_strategy_backtest import (
    open_dated_strategy_inputs,
    run_dated_strategy_series,
)
from backend.services.simulation.jp.backtest import run_cash_backtest
from backend.services.simulation.jp.data import open_execution_data
from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture
DAYS = [date(2026, 9, day) for day in (28, 29, 30)]


@pytest.fixture
def native(snapshot, tmp_path, monkeypatch):
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    with duckdb.connect(str(snapshot)) as db:
        db.execute("UPDATE research.daily_prices SET AdjFactor=1,ExRT='',Vo=100000")
        db.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-25','1'),('2026-10-01','1'),('2026-10-02','1')"
        )
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return snapshot, root


def publish(native):
    source, root = native
    version = import_jquants_snapshot(source, root)["version"]
    return open_execution_data(version)


@pytest.mark.parametrize("factor,held,expected", [(0.5, 100, 200), (2, 400, 200)])
def test_actual_topk_clearance_uses_execution_share_basis(
    native, factor, held, expected
):
    source, _ = native
    with duckdb.connect(str(source)) as db:
        db.execute(
            "UPDATE research.daily_prices SET O=100*?,C=100*?,H=100*?+1,L=100*?-1,AdjFactor=?,ExRT=? WHERE Date='2026-09-29'",
            [factor] * 5 + ["1" if factor < 1 else "2"],
        )
    reader = publish(native)
    account = DatedCashBacktestAccount.create(
        reader, 100000, market="JP", commission_rate=0, slippage_bps=0
    )
    account.execute_day(
        DAYS[0],
        [
            {
                "order_id": "opening",
                "signal_date": "2026-09-25",
                "symbol": "JP72030",
                "side": "BUY",
                "quantity": held,
            }
        ],
    )
    run = run_dated_strategy_series(
        inputs=open_dated_strategy_inputs("JP", reader),
        account=account,
        strategy_config={
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy.signal_strategy",
            "kwargs": {"signal": "<PRED>", "topk": 1, "n_drop": 1, "hold_thresh": 0},
        },
        sessions=[DAYS[1]],
        anchor=DAYS[0],
        initial_capital=100000,
        commission=0,
        scores={
            DAYS[0]: [
                {"symbol": "JP72030", "score": 0},
                {"symbol": "JP216A0", "score": 1},
            ]
        },
    )
    sales = [fill for fill in account.state["fills"] if fill["side"] == "SELL"]
    assert [(fill["symbol"], fill["quantity"]) for fill in sales] == [
        ("JP72030", expected)
    ]
    assert "JP72030" not in account.state["positions"]
    journal = next(item for item in account.state["orders"] if item["side"] == "SELL")
    assert journal["signal_quantity"] == held
    assert journal["quantity_basis_date"] == str(DAYS[0])
    assert journal["quantity"] == expected
    executed_order = next(
        item[0] for item in run.runner.execute_result if item[0].direction == Order.SELL
    )
    assert executed_order.amount == executed_order.deal_amount == expected


@pytest.mark.parametrize("intent", ["all", "target_zero", "target_empty"])
@pytest.mark.parametrize("factor,held,expected", [(0.5, 100, 200), (2, 400, 200)])
def test_public_lab_clearance_does_not_leave_split_shares(
    native, intent, factor, held, expected
):
    source, _ = native
    with duckdb.connect(str(source)) as db:
        db.execute(
            "UPDATE research.daily_prices SET O=100,C=100,H=101,L=99 WHERE Date<'2026-09-30'"
        )
        db.execute(
            "UPDATE research.daily_prices SET O=100*?,C=100*?,H=100*?+1,L=100*?-1,AdjFactor=?,ExRT=? WHERE Date='2026-09-30'",
            [factor] * 5 + ["1" if factor < 1 else "2"],
        )
    publish(native)
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    ctx = Context()
    ctx.market = "JP"

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = str(DAYS[0]), str(DAYS[2]), 1000000
        ctx.benchmark, ctx.commission, ctx.slippage = "TOPIX", 0, 0
        ctx.tax_sell = ctx.transfer_fee = 0

    def on_bar(ctx, bar):
        if bar.date.date() == DAYS[0]:
            ctx.buy("JP72030", qty=held)
        elif bar.date.date() == DAYS[1]:
            if intent == "all":
                ctx.sell("JP72030", all=True)
            elif intent == "target_zero":
                ctx.set_position("JP72030", weight=0)
            else:
                ctx.set_target_holdings([])

    result = run_backtest(
        ctx=ctx, provider=provider, user_globals={"setup": setup, "on_bar": on_bar}
    )
    assert result.status == "success", result.error
    assert [(trade.direction, trade.qty) for trade in result.trades] == [
        ("BUY", held),
        ("SELL", expected),
    ]
    assert ctx._positions == {}


def test_jp_short_template_is_rejected_before_building_cn_margin_strategy(
    native, monkeypatch
):
    publish(native)
    from backend.services.engine.qlib_app.utils import extended_strategies

    monkeypatch.setattr(
        extended_strategies,
        "get_margin_eligible_set",
        lambda *a: pytest.fail("CN margin pool"),
    )
    request = QlibBacktestRequest(
        market="JP",
        strategy_type="long_short_topk",
        benchmark="TOPIX",
        start_date=str(DAYS[1]),
        end_date=str(DAYS[1]),
        jp_slippage_bps=0,
    )
    context = SimpleNamespace(
        market_state_kwargs=lambda request: {},
        assert_reads_succeeded=lambda: None,
        advance=lambda *a: None,
    )
    with pytest.raises(ValueError, match="shorting"):
        run_cash_backtest(request, None, {}, strategy_context=context)


def test_actual_public_base_strategy_can_read_snapshot_volume_and_quote(native):
    reader = publish(native)
    context = SimpleNamespace(
        market_state_kwargs=lambda request: {},
        assert_reads_succeeded=lambda: None,
        advance=lambda *a: None,
    )
    request = QlibBacktestRequest(
        market="JP",
        strategy_type="CustomStrategy",
        strategy_content="""
from qlib.strategy.base import BaseStrategy
from qlib.backtest.decision import TradeDecisionWO
class QuoteStrategy(BaseStrategy):
    def generate_trade_decision(self, execute_result=None):
        start, end = self.trade_calendar.get_step_time(shift=1)
        assert self.trade_exchange.get_volume('jp_72030',start,end) == 100000
        assert self.trade_exchange.get_quote_info('jp_72030',start,end,'$open') == 100
        assert self.trade_exchange.quote.get_data('jp_72030',start,end,'$amount','sum') == 100000
        return TradeDecisionWO([], self)
def get_strategy_instance():
    return QuoteStrategy()
""",
    )
    strategy = build_dated_strategy(request, strategy_context=context)
    runner = DatedStrategyRunner(
        strategy,
        reader.calendar.sessions,
        DAYS[1],
        DAYS[1],
        0,
        strategy_context=context,
    )
    account = DatedCashBacktestAccount.create(
        reader, 30000, market="JP", commission_rate=0, slippage_bps=0
    )
    bars, master = reader.day(DAYS[0], ["JP72030"])
    kwargs = open_dated_strategy_inputs("JP", reader).decision_snapshot(
        account.state, [], bars, master, DAYS[0]
    )
    assert runner.decide(step=0, **kwargs) == []


@pytest.mark.parametrize("factor,expected", [(0.5, 400), (2, 100)])
@pytest.mark.parametrize("held", [0, 400])
def test_lab_nonzero_target_weight_keeps_the_requested_economic_position(
    native, factor, expected, held
):
    source, _ = native
    with duckdb.connect(str(source)) as db:
        db.execute(
            "UPDATE research.daily_prices SET O=100,C=100,H=101,L=99 WHERE Date<'2026-09-30'"
        )
        db.execute(
            "UPDATE research.daily_prices SET O=100*?,C=100*?,H=100*?+1,L=100*?-1,AdjFactor=?,ExRT=? WHERE Date='2026-09-30'",
            [factor] * 5 + ["1" if factor < 1 else "2"],
        )
    publish(native)
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context

    ctx = Context()
    ctx.market = "JP"

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = str(DAYS[0]), str(DAYS[2]), 1000000
        ctx.benchmark, ctx.commission, ctx.slippage = "TOPIX", 0, 0
        ctx.tax_sell = ctx.transfer_fee = 0

    def on_bar(ctx, bar):
        if bar.date.date() == DAYS[0] and held:
            ctx.buy("JP72030", qty=held)
        if bar.date.date() == DAYS[1]:
            ctx.set_position("JP72030", weight=0.02)

    result = run_backtest(
        ctx=ctx,
        provider=_resolve_provider({"options": {"market": "JP"}}, None),
        user_globals={"setup": setup, "on_bar": on_bar},
    )
    assert result.status == "success", result.error
    assert ctx._positions["JP72030"].qty == expected
    assert ctx._positions["JP72030"].market_value == 20000
    assert ctx.equity == 1000000


def test_custom_long_short_subclass_cannot_reach_cn_margin_pool(monkeypatch):
    from backend.services.engine.qlib_app.utils import extended_strategies

    monkeypatch.setattr(
        extended_strategies,
        "get_margin_eligible_set",
        lambda *a: pytest.fail("CN margin pool"),
    )
    context = SimpleNamespace(
        market_state_kwargs=lambda request: {}, assert_reads_succeeded=lambda: None
    )
    request = QlibBacktestRequest(
        market="JP",
        strategy_type="CustomStrategy",
        strategy_content="""
from backend.services.engine.qlib_app.utils.extended_strategies import RedisLongShortTopkStrategy
class MyShortStrategy(RedisLongShortTopkStrategy):
    pass
def get_strategy_config():
    return {'class':MyShortStrategy,'kwargs':{'signal':'<PRED>'}}
""",
    )
    with pytest.raises(ValueError, match="shorting"):
        build_dated_strategy(request, strategy_context=context)


@pytest.mark.parametrize(
    "query",
    [
        "self.trade_exchange.get_volume('jp_72030', end+pd.Timedelta(days=1), end+pd.Timedelta(days=1))",
        "self.trade_exchange.get_quote_info('jp_72030',start,end,'$not_published')",
        "self.trade_exchange.get_deal_price('jp_72030',end+pd.Timedelta(days=2),end+pd.Timedelta(days=2),1)",
        "self.trade_exchange.get_close('jp_72030',start-pd.Timedelta(days=1),end)",
        "self.trade_exchange.get_quote_from_qlib()",
    ],
)
def test_swallowed_unsupported_exchange_query_is_an_explicit_failure(native, query):
    reader = publish(native)
    context = SimpleNamespace(
        market_state_kwargs=lambda request: {},
        assert_reads_succeeded=lambda: None,
        advance=lambda *a: None,
    )
    request = QlibBacktestRequest(
        market="JP",
        strategy_type="CustomStrategy",
        strategy_content=f"""
import pandas as pd
from qlib.strategy.base import BaseStrategy
from qlib.backtest.decision import TradeDecisionWO
class QuoteStrategy(BaseStrategy):
    def generate_trade_decision(self, execute_result=None):
        start,end = self.trade_calendar.get_step_time(shift=1)
        try:
            {query}
        except Exception:
            pass
        return TradeDecisionWO([],self)
def get_strategy_instance():
    return QuoteStrategy()
""",
    )
    strategy = build_dated_strategy(request, strategy_context=context)
    runner = DatedStrategyRunner(
        strategy,
        reader.calendar.sessions,
        DAYS[1],
        DAYS[1],
        0,
        strategy_context=context,
    )
    account = DatedCashBacktestAccount.create(
        reader, 30000, market="JP", commission_rate=0, slippage_bps=0
    )
    bars, master = reader.day(DAYS[0], ["JP72030"])
    kwargs = open_dated_strategy_inputs("JP", reader).decision_snapshot(
        account.state, [], bars, master, DAYS[0]
    )
    with pytest.raises(ValueError, match="Dated Exchange quote read failed"):
        runner.decide(step=0, **kwargs)


def test_execution_share_orders_without_a_signal_basis_keep_their_existing_units(
    native,
):
    source, _ = native
    with duckdb.connect(str(source)) as db:
        db.execute(
            "UPDATE research.daily_prices SET AdjFactor=0.5,ExRT='1' WHERE Date='2026-09-29'"
        )
    reader = publish(native)
    account = DatedCashBacktestAccount.create(
        reader, 30000, market="JP", commission_rate=0, slippage_bps=0
    )
    result = account.execute_day(
        DAYS[1],
        [
            {
                "order_id": "execution-shares",
                "signal_date": str(DAYS[0]),
                "symbol": "JP72030",
                "side": "BUY",
                "quantity": 100,
            }
        ],
    )
    assert result["orders"][0]["fill"]["quantity"] == 100
    assert result["orders"][0]["quantity"] == 100
    assert "signal_quantity" not in result["orders"][0]


@pytest.mark.parametrize("kind", ["missing_module", "null_kwargs", "instance"])
def test_actual_short_strategy_capability_rejects_standard_config_forms(
    kind, monkeypatch
):
    from backend.services.engine.qlib_app.utils import extended_strategies

    monkeypatch.setattr(
        extended_strategies,
        "get_margin_eligible_set",
        lambda *a: pytest.fail("CN margin pool"),
    )
    if kind == "instance":
        config = extended_strategies.RedisLongShortTopkStrategy(
            signal=pd.Series(dtype=float), topk=1
        )
    else:
        config = {"class": "RedisLongShortTopkStrategy", "kwargs": None}
        if kind == "missing_module":
            config["kwargs"] = {"signal": "<PRED>", "topk": 1}
        else:
            config["module_path"] = (
                "backend.services.engine.qlib_app.utils.extended_strategies"
            )
    with pytest.raises(ValueError, match="shorting"):
        DatedStrategyRunner(config, [DAYS[0]], DAYS[0], DAYS[0])


def test_consolidated_odd_lot_clearance_is_rejected_without_partial_execution(native):
    from copy import deepcopy
    from backend.services.simulation.jp.rules import RuleDataMissing

    source, _ = native
    with duckdb.connect(str(source)) as db:
        db.execute(
            "UPDATE research.daily_prices SET AdjFactor=2,ExRT='2' WHERE Date='2026-09-29'"
        )
    reader = publish(native)
    account = DatedCashBacktestAccount.create(
        reader, 100000, market="JP", commission_rate=0, slippage_bps=0
    )
    account.execute_day(
        DAYS[0],
        [
            {
                "order_id": "opening",
                "signal_date": "2026-09-25",
                "symbol": "JP72030",
                "side": "BUY",
                "quantity": 500,
            }
        ],
    )
    before = deepcopy(account.account)
    with pytest.raises(RuleDataMissing, match="Odd-lot sale treatment"):
        account.execute_day(
            DAYS[1],
            [
                {
                    "order_id": "clear-all",
                    "signal_date": str(DAYS[0]),
                    "quantity_basis_date": str(DAYS[0]),
                    "symbol": "JP72030",
                    "side": "SELL",
                    "quantity": 500,
                }
            ],
        )
    assert account.account == before
