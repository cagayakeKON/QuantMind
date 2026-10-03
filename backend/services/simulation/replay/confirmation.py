"""Dated rule hooks for the original replay confirmation algorithm.

One confirmation scope owns a disposable funded account projection. Matching
and cash calculations reuse the registered rules; nothing is persisted here.
"""

from uuid import uuid4


class RegisteredReplayConfirmationRules:
    def __init__(self, context, cash_rules, account):
        if (
            context.market != cash_rules.market
            or context.data_version != cash_rules.data_version
        ):
            raise ValueError("Replay confirmation requires the same market/publication")
        self.context = context
        self.cash_rules = cash_rules
        self.required_errors = context.reader.execution_data_errors
        self._account = cash_rules.prepare_day(account, context.trade_date)

    def symbol(self, code):
        return self.context.symbol(code)

    def quantity(self, symbol, side, quantity):
        bar = self.context.reader.get_bar(symbol, self.context.trade_date)
        if bar is None:
            raise ValueError("NO_MARKET_DATA")
        unit = self.context.trading_unit(symbol, {bar.symbol: bar})
        return self.cash_rules.confirmation_quantity(side, quantity, unit)

    def validate_order(self, symbol, side, quantity, proposal):
        if proposal.get("origin") == "stop_loss":
            raise NotImplementedError(
                "Registered replay requires its intraday stop data adapter"
            )
        bar = self.context.reader.get_bar(symbol, self.context.trade_date)
        if bar is None:
            return "NO_MARKET_DATA"
        position = self._account["positions"].get(symbol) or {}
        matched = self.context.match(
            symbol=symbol,
            quantity=quantity,
            side=side.lower(),
            bar=bar,
            cfg=self.cash_rules.match_config,
            available_volume=(
                position.get("available_volume", 0) if side == "SELL" else None
            ),
            used_volume=self.cash_rules.filled_volume(
                self._account, self.context.trade_date, symbol
            ),
        )
        if not matched.success:
            return matched.reason
        try:
            self._account = self.cash_rules.apply_fill(
                self._account,
                self.context.trade_date,
                symbol,
                side.lower(),
                matched,
                "confirmation:" + uuid4().hex,
            )
        except self.required_errors:
            raise
        except ValueError as error:
            return str(error)
        return None
