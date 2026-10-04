"""Keep valid suspended holdings separate from executable daily prices."""

from copy import deepcopy

from backend.shared.stock_utils import StockCodeUtil

from .cash_rules import money
from .rules import RuleDataMissing


def held_mark(previous_price, *, suspended, close, symbol, day):
    # Preparation has already adjusted this price and inventory for share actions.
    price = money(previous_price if suspended else close)
    if not price.is_finite() or price <= 0:
        raise RuleDataMissing(
            f"Exact dated closing mark unavailable for {symbol} on {day}"
        )
    return float(price)


def closing_marks(reader, account, day):
    projection = deepcopy(account)
    bars = reader.load_date(day, list(projection["positions"]))
    stale = []
    for symbol, position in projection["positions"].items():
        canonical = StockCodeUtil.to_suffix(symbol, market="JP")
        bar = bars.get(canonical)
        # A missing raw row or master is not evidence of a suspension.
        if bar is None or bar.trade_date != day or bar.symbol != canonical:
            raise RuleDataMissing(
                f"Exact dated closing mark unavailable for {symbol} on {day}"
            )
        position["price"] = held_mark(
            position["price"],
            suspended=bar.suspended,
            close=bar.close,
            symbol=symbol,
            day=day,
        )
        if bar.suspended:
            stale.append(StockCodeUtil.to_prefix(symbol, market="JP"))
    return projection, stale
