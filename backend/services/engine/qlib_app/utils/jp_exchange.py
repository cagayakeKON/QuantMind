"""Japan ordinary stocks on Qlib's standard Exchange and numeric factor API."""

from datetime import date
from collections import defaultdict
from pathlib import Path

import pandas as pd
from qlib.backtest.exchange import Exchange

from backend.services.engine.data_platform.jp_trading_units import read_trading_units
from backend.shared.stock_utils import StockCodeUtil
from .cn_exchange import CnExchange


class JpExchange(CnExchange):
    """Normal Qlib initialization, positions, decisions, fills and trade logging.

    Qlib's adjusted-share portfolio is retained, including its corporate-action
    economics. This daily-bar simulation has no separate settlement cash ledger.
    """

    def __init__(self, *, trading_units_path=None, **kwargs):
        # Public research configurations cross JSON and YAML, whose sequences
        # are lists. Restore Qlib's documented tuple arguments at this boundary.
        for field in ("limit_threshold", "volume_threshold"):
            if isinstance(kwargs.get(field), list):
                kwargs[field] = tuple(kwargs[field])
        # Factor templates declare standard Qlib fees. CnExchange's existing
        # fill callback consumes commission/min_commission instead.
        open_cost = kwargs.pop("open_cost", None)
        close_cost = kwargs.pop("close_cost", None)
        min_cost = kwargs.pop("min_cost", None)
        if open_cost is not None or close_cost is not None:
            kwargs.setdefault(
                "commission", open_cost if open_cost is not None else close_cost
            )
        if min_cost is not None:
            kwargs.setdefault("min_commission", min_cost)
        kwargs.setdefault("trade_unit", None)
        self.trading_units = (
            read_trading_units(Path(trading_units_path).read_bytes())
            if trading_units_path
            else {}
        )
        super().__init__(**kwargs)

    def _lot(self, stock_id, start_time):
        day = pd.Timestamp(start_time).date()
        symbol = StockCodeUtil.to_prefix(stock_id, market="JP")
        for item in self.trading_units.get(symbol, []):
            if item["valid_from"] <= day <= item["valid_to"]:
                return item["lot_size"]
        if day >= date(2018, 10, 1):
            return 100
        raise ValueError(f"Historical JP trading unit is unavailable: {symbol}/{day}")

    def get_factor(self, stock_id, start_time, end_time):
        self._lot(stock_id, start_time)
        return Exchange.get_factor(self, stock_id, start_time, end_time)

    def round_amount_by_trade_unit(
        self, deal_amount, factor=None, stock_id=None, start_time=None, end_time=None
    ):
        if stock_id is None or start_time is None:
            return super().round_amount_by_trade_unit(
                deal_amount, factor, stock_id, start_time, end_time
            )
        previous_unit = self.trade_unit
        self.trade_unit = self._lot(stock_id, start_time)
        try:
            return super().round_amount_by_trade_unit(
                deal_amount, factor, stock_id, start_time, end_time
            )
        finally:
            self.trade_unit = previous_unit

    def get_amount_of_trade_unit(
        self, factor=None, stock_id=None, start_time=None, end_time=None
    ):
        if stock_id is None or start_time is None:
            return super().get_amount_of_trade_unit(
                factor, stock_id, start_time, end_time
            )
        previous_unit = self.trade_unit
        self.trade_unit = self._lot(stock_id, start_time)
        try:
            return super().get_amount_of_trade_unit(
                factor, stock_id, start_time, end_time
            )
        finally:
            self.trade_unit = previous_unit

    # Keep Qlib's suspension and same-session quotation behavior. The China
    # Exchange's previous-quote fallback and percentage board limits do not apply.
    get_close = Exchange.get_close
    get_deal_price = Exchange.get_deal_price
    check_stock_limit = Exchange.check_stock_limit

    def get_volume(self, stock_id, start_time, end_time, method="sum"):
        # Qlib orders are expressed in price-adjusted shares. Published Japan
        # volume uses split adjustment, so convert via ordinary feature values.
        return self.quote.get_data(
            stock_id,
            start_time,
            end_time,
            field="$volume * $volume_factor / $factor",
            method=method,
        )

    def _calc_trade_info_by_order(self, order, position, dealt_order_amount):
        # The official order callback carries the instrument/date; factors remain
        # ordinary floats. Final Qlib cash/volume clipping rounds at the dated lot.
        previous_unit = self.trade_unit
        self.trade_unit = self._lot(order.stock_id, order.start_time)
        try:
            return super()._calc_trade_info_by_order(
                order,
                position,
                # Native TopK previews sell costs without the executor's daily
                # accumulator. Actual fills retain the supplied cumulative map.
                dealt_order_amount
                if dealt_order_amount is not None
                else defaultdict(float),
            )
        finally:
            self.trade_unit = previous_unit
