"""Dated Japan price, unit and fee rules for the common daily matcher."""

from decimal import Decimal, ROUND_CEILING

from backend.services.simulation.services.ashare_matcher import MatchConfig
from backend.services.simulation.services.local_market_data import DailyBar

from .rules import RuleDataMissing, daily_limit_width, lot_size, round_price


def _decimal(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Non-finite JPY amount")
    return amount


class JapanDailyMatchRules:
    def __init__(self, metadata: dict, raw_bar: dict, used_volume: int = 0):
        self.metadata = metadata
        self.raw_bar = raw_bar
        self.used_volume = used_volume

    def lot_size(self, bar: DailyBar) -> int:
        return lot_size(bar.trade_date, self.metadata)

    def validate(self, side: str, quantity: int, bar: DailyBar) -> None:
        if side not in {"buy", "sell"}:
            raise ValueError("Invalid JP order side")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("JP quantity must be a positive integer")
        if self.metadata.get("product_category") != "011":
            raise ValueError("JP simulation is limited to domestic ordinary stocks")
        unit = self.lot_size(bar)
        if quantity % unit:
            raise ValueError(f"JP quantity must be a multiple of {unit}")
        if any(self.raw_bar.get(k) is None for k in ("open", "close", "volume")):
            raise ValueError("No daily trade/bar; no forward-filled execution")
        opened = _decimal(self.raw_bar["open"])
        if opened <= 0 or _decimal(self.raw_bar["volume"]) <= 0:
            raise ValueError("No valid opening trade")
        if self.used_volume + quantity > _decimal(self.raw_bar["volume"]):
            raise ValueError("JP simulated fills exceed observed daily volume")
        flag = "upper_limit_touched" if side == "buy" else "lower_limit_touched"
        if not self.raw_bar.get(flag):
            return
        base = self.metadata.get("limit_base_price")
        if base is None:
            base = self.metadata.get("previous_close")
            if base is not None:
                base = _decimal(base) * _decimal(self.raw_bar.get("adj_factor", "1"))
        locked = all(
            self.raw_bar.get(k) == self.raw_bar["open"]
            for k in ("high", "low", "close")
        )
        extreme = self.raw_bar.get("high" if side == "buy" else "low")
        if extreme is None or _decimal(extreme) == opened:
            raise ValueError("Opening at a touched limit; queue execution unavailable")
        if locked or base is None:
            raise ValueError("Limit liquidity cannot be established from daily data")
        width = daily_limit_width(_decimal(base))
        limit = _decimal(base) + (width if side == "buy" else -width)
        if (side == "buy" and opened >= limit) or (side == "sell" and opened <= limit):
            raise ValueError("Opening at price limit; queue execution unavailable")

    def price(self, side: str, bar: DailyBar, cfg: MatchConfig) -> Decimal:
        if cfg.price_mode != "open":
            raise RuleDataMissing("JP daily matching requires an opening trade")
        opened = _decimal(self.raw_bar["open"])
        slip = _decimal(cfg.slippage_bps) / 10000
        price = round_price(
            opened * (1 + slip if side == "buy" else 1 - slip),
            side.upper(),
            bar.trade_date,
            self.metadata,
        )
        if price <= 0:
            raise ValueError("Nonpositive execution price")
        return price

    def fees(
        self, quantity: int, price: Decimal, side: str, cfg: MatchConfig
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        fee = (price * quantity * _decimal(cfg.commission_rate)).to_integral_value(
            rounding=ROUND_CEILING
        )
        return fee, Decimal(0), Decimal(0), fee
