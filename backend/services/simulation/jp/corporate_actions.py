"""Split events on ordinary raw-share replay positions, before daily orders."""

from copy import deepcopy
from datetime import date
import math

from backend.services.simulation.jp.rules import RuleDataMissing
from backend.shared.stock_utils import StockCodeUtil


def adjust_replay_splits(account, bars, trade_date: date):
    """Preserve cash and cost basis; repeated previews must not grant shares twice.

    The date stays on the ordinary position, alongside first_buy_date, so the
    existing closing snapshot also restores it after a Redis cache eviction.
    No fills, cash flows or separate execution account are created.
    """
    result = deepcopy(account)
    day = trade_date.isoformat()
    for symbol, position in (result.get("positions") or {}).items():
        if not StockCodeUtil.is_jp_symbol(symbol):
            continue
        bar = bars.get(symbol)
        if bar is None:
            continue
        adjusted = str(position.get("split_adjusted_date") or "")
        acquired = str(position.get("first_buy_date") or "")[:10]
        if adjusted >= day or acquired >= day:
            continue
        factor = float(bar.split_factor)
        if not math.isfinite(factor) or factor <= 0:
            raise RuleDataMissing(f"Invalid JP split factor: {symbol}/{day}")
        if factor != 1.0:
            volume = float(position.get("volume") or 0.0) / factor
            if not math.isclose(volume, round(volume), abs_tol=1e-7, rel_tol=0):
                raise RuleDataMissing(
                    f"JP reverse split requires fractional-share disposition "
                    f"data: {symbol}/{day}"
                )
            position["volume"] = float(round(volume))
            if position.get("available_volume") is not None:
                position["available_volume"] = (
                    float(position["available_volume"]) / factor
                )
            for field in ("cost", "price"):
                position[field] = float(position.get(field) or 0.0) * factor
            position["market_value"] = position["volume"] * float(
                position.get("price") or 0.0
            )
        position["split_adjusted_date"] = day
    return result
