"""Shared lifecycle protocol, independent of Japan's cash/metadata schema."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from qlib.backtest.decision import Order
from qlib.backtest.position import Position

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.qlib_app.services import dated_strategy_backtest as shared
from backend.services.engine.qlib_app.services.dated_strategy import DecisionQuote


@pytest.mark.parametrize("market", ["CN", "HK", "US", "FUTURES", "CRYPTO"])
def test_other_market_routes_do_not_activate_dated_backtests(market):
    with pytest.raises(ValueError, match="not registered"):
        shared.open_dated_strategy_inputs(market, object())


@pytest.mark.parametrize("fault", ["market", "reader", "type"])
def test_registered_inputs_cannot_change_market_or_publication(monkeypatch, fault):
    reader = object()
    inputs = shared.DatedStrategyDataInputs("JP", reader, None, None, None, None)
    if fault == "market":
        inputs = replace(inputs, market="CN")
    elif fault == "reader":
        inputs = replace(inputs, reader=object())
    else:
        inputs = object()
    monkeypatch.setattr(
        shared,
        "import_module",
        lambda name: SimpleNamespace(open_backtest_inputs=lambda supplied: inputs),
    )
    with pytest.raises(ValueError, match="market/publication"):
        shared.open_dated_strategy_inputs("JP", reader)


@pytest.mark.parametrize(
    "market,code,api", [("CN", "sh600036", "SH600036"), ("JP", "jp_216a0", "JP216A0")]
)
def test_common_order_boundary_keeps_native_decision_and_dated_identity(
    market, code, api
):
    signal, execution = date(2026, 9, 28), date(2026, 9, 29)
    decision = Order(code, 100, Order.BUY, signal, signal)
    before = deepcopy(decision.__dict__)
    orders = shared.dated_strategy_orders([decision], signal, execution, market=market)
    assert orders == [
        {
            "order_id": f"strategy:{signal}:0:{api}",
            "symbol": api,
            "side": "BUY",
            "quantity": 100,
            "signal_date": str(signal),
            "execution_date": str(execution),
            "order_type": "MARKET",
        }
    ]
    assert decision.__dict__ == before


@pytest.mark.parametrize("amount", [0, -100, 0.5, float("nan"), float("inf")])
def test_cash_decisions_require_finite_positive_whole_shares(amount):
    decision = SimpleNamespace(stock_id="jp_216a0", amount=amount, direction=Order.BUY)
    with pytest.raises(ValueError, match="positive whole"):
        shared.dated_strategy_orders(
            [decision], date(2026, 9, 28), date(2026, 9, 29), market="JP"
        )


def test_shared_series_runs_another_registered_market_without_japanese_schema(
    monkeypatch,
):
    # This controlled executor records lifecycle calls only. Financial matching
    # is covered by actual registered cash tests, not invented by this protocol.
    days = [date(2026, 9, day) for day in (28, 29, 30)]
    events = []

    class Reader:
        execution_data_errors = (ValueError,)
        calendar = SimpleNamespace(sessions=days)
        data_version = "controlled-cn-publication"

        def __init__(self):
            self.hub = self

        def fetch_index_kline(self, symbol, start, end):
            assert symbol == "CONTROLLED_INDEX" and (start, end) == (days[1], days[2])
            return pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(days[1:]),
                    "open": [100, 102],
                    "close": [101, 103],
                }
            )

        def day(self, day, symbols, held):
            events.append(("data", day, tuple(symbols), tuple(held)))
            return {symbol: {"close": 10} for symbol in symbols}, {}

    reader = Reader()

    class Account:
        params = {"market": "CN"}

        def __init__(self):
            self.reader = reader
            self._state = {"positions": {}}

        @property
        def state(self):
            return deepcopy(self._state)

        def execute_day(self, day, orders):
            events.append(("execution", day, deepcopy(orders)))
            self._state = {"positions": {"SH600036": {"shares": 100}}}
            return {
                "orders": [
                    {
                        **order,
                        "status": "filled",
                        "fill": {"quantity": order["quantity"], "price": 11, "fee": 1},
                    }
                    for order in orders
                ],
                "snapshot": {"equity": 1100, "stale_symbols": []},
            }

    def snapshot(state, scores, bars, master, day):
        events.append(("decision", day))
        held = bool(state["positions"])
        return {
            "signal_day": day,
            "scores": {"sh600036": scores[0]["score"]},
            "quotes": {"sh600036": DecisionQuote(10, 100)},
            "cash": 0 if held else 1000,
            "positions": {"sh600036": {"amount": 100, "price": 10}} if held else {},
        }

    inputs = shared.DatedStrategyDataInputs(
        "CN",
        reader,
        snapshot,
        None,
        lambda state: Position(
            cash=0, position_dict={"sh600036": {"amount": 100, "price": 11}}
        ),
        lambda state, master: {"SH600036": {"name": "Controlled security"}},
    )
    monkeypatch.setitem(
        LOCAL_MARKET_PROVIDERS,
        "CN",
        replace(LOCAL_MARKET_PROVIDERS["JP"], benchmark="CONTROLLED_INDEX"),
    )
    config = {
        "class": "TopkDropoutStrategy",
        "module_path": "qlib.contrib.strategy.signal_strategy",
        "kwargs": {"signal": "<PRED>", "topk": 1, "n_drop": 1, "risk_degree": 1},
    }
    account = Account()
    result = shared.run_dated_strategy_series(
        inputs=inputs,
        account=account,
        strategy_config=config,
        sessions=days[1:],
        anchor=days[0],
        initial_capital=1000,
        commission=0,
        scores={day: [{"symbol": "SH600036", "score": 1}] for day in days[:2]},
    )
    executions = [row for row in events if row[0] == "execution"]
    assert executions[0][2][0]["quantity"] == 100
    assert executions[0][2][0]["signal_date"] == str(days[0])
    assert executions[1][2][0]["signal_date"] == str(days[1])
    assert executions[1][2][0]["side"] == "SELL"
    assert executions[1][2][0]["quantity"] == 100
    assert [row[1] for row in events if row[0] == "decision"] == days[:2]
    assert [row["benchmark_value"] for row in result.equity_curve] == [1000, 1010, 1030]
    assert len(result.position_history) == len(result.position_info) == 2
    assert config["kwargs"]["signal"] == "<PRED>"
    assert set(account.state) == {"positions"}
