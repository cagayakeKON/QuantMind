"""In-memory dated backtests using the registered cash and matching contracts.

No country cash arithmetic or strategy selection belongs here. This executor
stages each day locally and publishes it only after closing/journal validation.
"""

from copy import deepcopy
from datetime import date

from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.replay.execution_context import ReplayExecutionContext
from backend.services.simulation.services.dated_execution import (
    match_registered_cash_order,
)
from backend.shared.stock_utils import StockCodeUtil
from backend.services.simulation.services.local_market_data import DailyBar


class DatedCashBacktestAccount:
    def __init__(self, reader, params, account):
        self.reader = reader
        self.params = deepcopy(params)
        self.rules = open_registered_replay_cash_rules(params, reader=reader)
        if self.rules is None or not all(
            callable(getattr(self.rules, method, None))
            for method in ("backtest_state", "complete_backtest_day")
        ):
            raise ValueError("No dated backtest cash adapter is registered")
        if (
            self.rules.market != str(params["market"]).upper()
            or self.rules.data_version != reader.data_version
        ):
            raise ValueError("Cash rules differ from the dated execution publication")
        self.params["market"] = self.rules.market
        self.account = deepcopy(account)
        self.rules.backtest_state(self.account)

    @classmethod
    def create(cls, reader, initial_cash, *, market, **config):
        params = {"market": market, "data_version": reader.data_version, **config}
        rules = open_registered_replay_cash_rules(params, reader=reader)
        if rules is None:
            raise ValueError("No dated backtest cash adapter is registered")
        return cls(reader, params, rules.initialize(initial_cash))

    @property
    def state(self):
        return self.rules.backtest_state(self.account)

    def checkpoint(self):
        return {
            "params": deepcopy(self.params),
            "trade_date": self.state["cursor"],
            "cash": self.rules.checkpoint(self.account),
        }

    @classmethod
    def restore(cls, reader, checkpoint):
        params = checkpoint["params"]
        rules = open_registered_replay_cash_rules(params, reader=reader)
        if rules is None:
            raise ValueError("No dated backtest cash adapter is registered")
        day = date.fromisoformat(checkpoint["trade_date"])
        restored = cls(
            reader, params, rules.restore_checkpoint(checkpoint["cash"], day)
        )
        if restored.state["cursor"] != str(day):
            raise ValueError("Backtest journal cursor differs from checkpoint date")
        return restored

    def execute_day(self, day: date, orders: list[dict]) -> dict:
        context = ReplayExecutionContext(
            self.params["market"], self.params["data_version"], day, self.reader
        )
        state = self.state
        if state["cursor"] and str(day) <= state["cursor"]:
            raise ValueError("Dated backtest dates must advance strictly")
        staged = self.rules.prepare_day(self.account, day)
        known_ids = {item["order_id"] for item in state["orders"]}
        results = []
        for request in orders:
            order_id = request["order_id"]
            if order_id in known_ids:
                raise ValueError(f"Duplicate backtest order ID: {order_id}")
            known_ids.add(order_id)
            signal_day = date.fromisoformat(request["signal_date"])
            if self.reader.calendar.next_session(signal_day) != day:
                raise ValueError("Orders execute on the session after the signal date")
            if request.get("order_type", "MARKET") != "MARKET":
                raise ValueError("Dated backtests require next-open market orders")
            if request.get("position_side", "long") != "long" or request.get(
                "trade_action"
            ) in {"sell_to_open", "buy_to_close"}:
                raise NotImplementedError(
                    "Registered cash backtests do not allow short orders"
                )
            side, quantity = str(request["side"]).upper(), request["quantity"]
            if side not in {"BUY", "SELL"} or (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                raise ValueError(
                    "Orders require a cash side and positive integer quantity"
                )
            symbol = context.symbol(request["symbol"])
            basis = request.get("quantity_basis_date")
            if basis is not None:
                if basis != str(signal_day):
                    raise ValueError("Order share basis must match its signal date")
                convert = getattr(self.rules, "signal_quantity_at_execution", None)
                if not callable(convert):
                    raise NotImplementedError(
                        "This market cannot convert signal-day shares"
                    )
                quantity = convert(symbol, signal_day, day, quantity, side=side)
            result = {
                **deepcopy(request),
                "symbol": StockCodeUtil.to_prefix(symbol, market=context.market),
                "trade_date": str(day),
                "settlement_date": str(self.reader.calendar.settlement_date(day)),
                "status": "rejected",
                "quantity": quantity,
                **(
                    {"signal_quantity": request["quantity"]}
                    if basis is not None
                    else {}
                ),
            }
            if basis is not None and quantity == 0:
                result["reason"] = (
                    "Signal quantity converts below the execution trading unit"
                )
                results.append(result)
                continue
            bar = self.reader.get_bar(symbol, day)
            if bar is None:
                # A missing price row does not waive required security metadata.
                # This empty dated bar is only a non-tradable rule input; the
                # registered matcher must reject absent raw opening liquidity.
                bar = DailyBar(
                    symbol=symbol,
                    trade_date=day,
                    open=0,
                    high=0,
                    low=0,
                    close=0,
                    volume=0,
                    amount=0,
                    vwap=0,
                    pre_close=0,
                    limit_up=float("inf"),
                    limit_down=0,
                    is_st=False,
                    suspended=False,
                )
            unit = context.trading_unit(symbol, {symbol: bar})
            self.rules.confirmation_quantity(side, quantity, unit)
            matched = match_registered_cash_order(
                context,
                self.rules,
                staged,
                symbol=symbol,
                side=side.lower(),
                quantity=quantity,
                bar=bar,
                used_volume=self.rules.filled_volume(staged, day, symbol),
            )
            if not matched.success:
                result["reason"] = matched.reason
            else:
                try:
                    updated = self.rules.apply_fill(
                        staged, day, symbol, side.lower(), matched, order_id
                    )
                except ValueError as error:
                    if isinstance(error, self.reader.execution_data_errors):
                        raise
                    result["reason"] = str(error)
                else:
                    staged = updated
                    fill = self.rules.backtest_state(staged)["fills"][-1]
                    result.update(status="filled", fill=fill)
            results.append(result)
        marks = getattr(self.rules, "closing_marks", None)
        if callable(marks):
            projection, stale = marks(staged, day)
        else:
            projection = deepcopy(staged)
            stale = []
            for symbol, position in projection["positions"].items():
                bar = self.reader.get_bar(symbol, day)
                if bar is not None and bar.close > 0:
                    position["price"] = bar.close
                else:
                    stale.append(StockCodeUtil.to_prefix(symbol, market=context.market))
        staged = self.rules.merge_marks(staged, projection)
        staged = self.rules.complete_backtest_day(staged, day, results, stale)
        snapshot = self.rules.backtest_state(staged)["daily"][-1]
        self.account = staged
        return {"orders": results, "snapshot": snapshot}
