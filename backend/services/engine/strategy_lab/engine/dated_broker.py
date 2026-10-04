"""SDK order intents bridged to the shared dated cash executor.

The existing Lab loop drives this broker. Signals observed at T close are
submitted at the next session opening; country arithmetic remains registered.
"""

from copy import deepcopy
from decimal import Decimal
import pandas as pd
from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from backend.shared.stock_utils import StockCodeUtil
from ..runner.result_collector import EquityPoint, TradeRecord, PositionSnapshot
from ..sdk.position import Position


class DatedLabBroker:
    def __init__(self, ctx, provider, cash):
        self.ctx, self.provider = ctx, provider
        if ctx.tax_sell or ctx.transfer_fee:
            raise ValueError("Native cash fee settings must use the selected market")
        self.executor = DatedCashBacktestAccount.create(
            provider.reader,
            cash,
            market=provider.market,
            commission_rate=ctx.commission,
            slippage_bps=Decimal(str(ctx.slippage)) * 10000,
        )
        self.pending = []
        self._trades, self._equity = [], []
        self.sequence = 0

    @property
    def equity(self):
        return self.executor.account["total_asset"]

    @property
    def trades(self):
        return deepcopy(self._trades)

    @property
    def equity_curve(self):
        return deepcopy(self._equity)

    def prepare_day(self, today):
        result = self.executor.execute_day(today.date(), self.pending)
        self.pending = []
        for order in result["orders"]:
            if order["status"] != "filled":
                self.ctx.log(
                    f"Rejected {order['symbol']}: {order.get('reason')}",
                    level="warning",
                )
                continue
            fill = order["fill"]
            self._trades.append(
                TradeRecord(
                    date=fill["trade_date"],
                    symbol=fill["symbol"],
                    direction=fill["side"],
                    qty=fill["quantity"],
                    price=float(fill["price"]),
                    pnl=float(fill["realized_pnl"])
                    if fill.get("realized_pnl") is not None
                    else None,
                    reason=order.get("reason", ""),
                    detail={"fee": fill["fee"], "currency": self.provider.currency},
                )
            )
        account = self.executor.account
        self.ctx._update_cash_equity(account["cash"], account["total_asset"])
        self.ctx._positions.clear()
        for symbol, position in account["positions"].items():
            code = StockCodeUtil.to_prefix(symbol, market=self.provider.market)
            self.ctx._set_position(
                Position(
                    symbol=code,
                    qty=position["volume"],
                    cost=position["cost"] * position["volume"],
                    market_value=position["market_value"],
                    last_price=position["price"],
                )
            )

    def register_risk_rules(self, rules):
        if rules:
            raise NotImplementedError(
                "Native daily SDK execution does not support intraday risk orders"
            )

    def process_day(self, today, orders):
        requests = []

        def add(symbol, side, quantity, reason):
            if not quantity:
                return
            if isinstance(quantity, bool) or int(quantity) != quantity or quantity < 0:
                raise ValueError("SDK cash order quantity must be a positive integer")
            self.sequence += 1
            requests.append(
                {
                    "order_id": f"sdk-{self.sequence}",
                    "symbol": symbol,
                    "side": side,
                    "quantity": int(quantity),
                    "signal_date": str(today.date()),
                    "reason": reason,
                }
            )

        def quantity_for(symbol, weight):
            code = StockCodeUtil.to_suffix(symbol, market=self.provider.market)
            bar = self.provider.reader.get_bar(code, today.date())
            if bar is None or bar.close <= 0:
                raise ValueError(f"Signal-day price unavailable for {symbol}")
            unit = self.provider.reader.matching_rules(code, today.date()).lot_size(bar)
            return int(self.equity * weight / bar.close / unit) * unit

        holdings = self.ctx._positions
        for order in orders:
            if order.side == "set_target_holdings":
                targets = [
                    StockCodeUtil.to_prefix(s, market=self.provider.market)
                    for s in order.targets
                ]
                for symbol, position in holdings.items():
                    if symbol not in targets:
                        add(symbol, "SELL", position.qty, order.reason)
                for symbol in targets:
                    target = quantity_for(symbol, 1 / len(targets))
                    delta = target - holdings.get(symbol, Position(symbol)).qty
                    add(
                        symbol, "BUY" if delta > 0 else "SELL", abs(delta), order.reason
                    )
                continue
            symbol = StockCodeUtil.to_prefix(order.symbol, market=self.provider.market)
            held = holdings.get(symbol, Position(symbol)).qty
            if order.side == "set_position":
                delta = quantity_for(symbol, order.weight or 0) - held
                add(symbol, "BUY" if delta > 0 else "SELL", abs(delta), order.reason)
            elif order.side == "buy":
                add(
                    symbol,
                    "BUY",
                    order.qty
                    if order.qty is not None
                    else quantity_for(symbol, order.weight or 0),
                    order.reason,
                )
            elif order.side == "sell":
                qty = held if order.all else order.qty
                if qty is None:
                    qty = int(held * (order.weight or 0))
                add(symbol, "SELL", qty, order.reason)
            else:
                raise ValueError(f"Unsupported SDK cash order intent: {order.side}")
        # Stable sell-before-buy ordering matches the existing cash backtest contract.
        self.pending = sorted(requests, key=lambda order: order["side"] != "SELL")
        benchmark = self.provider.benchmark_history(self.ctx.benchmark, 1, today)
        self._equity.append(
            EquityPoint(
                str(today.date()),
                self.equity,
                float(benchmark.iloc[-1]) if len(benchmark) else None,
            )
        )
        if self.pending and today.date() == pd.Timestamp(self.ctx.end).date():
            self.ctx.log(
                "End-date signals have no next-session execution inside the requested backtest",
                level="warning",
            )

    def positions_snapshot(self, today):
        return [
            PositionSnapshot(
                str(today.date()), p.symbol, p.qty, p.cost, p.market_value, p.pnl_pct
            )
            for p in self.ctx._positions.values()
        ]
