"""Convert an observed JP raw close into a later session's raw price basis."""

import math

from backend.services.simulation.jp.rules import RuleDataMissing


def raw_reference_price(previous, current, *, adjustment_product=1.0) -> float:
    """Use publication factors; old factorless partitions use accrued AdjFactor.

    This is a valuation/limit reference, never a replacement execution quote.
    Price-factor ratios include intervening splits and rights and cancel factors
    for actions after the current session.
    """

    def positive(value, label):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuleDataMissing(f"JP {label} is unavailable") from exc
        if not math.isfinite(number) or number <= 0:
            raise RuleDataMissing(f"JP {label} is unavailable")
        return number

    close = positive(previous.get("close"), "reference close")
    if "price_factor" in previous and "price_factor" in current:
        ratio = positive(previous["price_factor"], "previous price factor") / positive(
            current["price_factor"], "current price factor"
        )
    else:
        ratio = positive(adjustment_product, "accrued adjustment factor")
    return positive(close * ratio, "reference price")
