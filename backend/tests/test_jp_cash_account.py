"""Pure Japan ticks, limits and historical session hours retained after cash protocol retirement."""

from datetime import date
from decimal import Decimal

from backend.services.simulation.jp.rules import (
    daily_limit_width,
    round_price,
    session_close,
    tick_size,
)


def test_dated_ticks_and_session_extension():
    mid = {"scale_category": "TOPIX Mid400"}
    assert tick_size(Decimal(900), date(2023, 6, 2), mid) == 1
    assert tick_size(Decimal(900), date(2023, 6, 5), mid) == Decimal(".1")
    assert round_price(Decimal("3000.01"), "BUY", date(2026, 9, 2), mid) == 3001
    assert daily_limit_width(Decimal(99)) == 30
    assert daily_limit_width(Decimal(100)) == 50
    assert session_close(date(2024, 11, 1)).isoformat() == "15:00:00"
    assert session_close(date(2024, 11, 5)).isoformat() == "15:30:00"
