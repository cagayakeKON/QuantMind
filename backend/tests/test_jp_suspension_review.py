"""Sourced Japan tick rules survive retirement of the cash executor."""

from datetime import date
from decimal import Decimal
import pytest
from backend.services.simulation.jp.rules import RuleDataMissing, round_price, tick_size


@pytest.mark.parametrize("category", ["TOPIX Core30", "TOPIX Large70"])
@pytest.mark.parametrize(
    "day,price,expected",
    [
        (date(2010, 1, 4), 999, "1"),
        (date(2014, 1, 13), 4500, "5"),
        (date(2014, 1, 14), 999, "1"),
        (date(2014, 7, 21), 4500, "1"),
        (date(2014, 7, 22), 999, ".1"),
        (date(2014, 7, 22), 4500, ".5"),
        (date(2015, 9, 23), 4500, ".5"),
        (date(2015, 9, 24), 4500, "1"),
        (date(2014, 1, 14), 45000, "5"),
        (date(2015, 9, 24), 45000, "10"),
    ],
)
def test_official_topix100_three_phase_tick_boundaries(category, day, price, expected):
    assert tick_size(Decimal(price), day, {"scale_category": category}) == Decimal(
        expected
    )


def test_unsupported_early_ticks_and_existing_normal_mid400_rules():
    with pytest.raises(RuleDataMissing, match="before 2010"):
        tick_size(Decimal(999), date(2009, 12, 30), {"scale_category": "TOPIX Core30"})
    assert (
        round_price(
            Decimal("999.11"),
            "BUY",
            date(2013, 12, 30),
            {"scale_category": "TOPIX Core30"},
        )
        == 1000
    )
    assert round_price(
        Decimal("999.11"), "BUY", date(2014, 7, 22), {"scale_category": "TOPIX Core30"}
    ) == Decimal("999.2")
    assert tick_size(Decimal(4500), date(2014, 7, 22), {"scale_category": "-"}) == 5
    assert (
        tick_size(Decimal(999), date(2023, 6, 2), {"scale_category": "TOPIX Mid400"})
        == 1
    )
    assert tick_size(
        Decimal(999), date(2023, 6, 5), {"scale_category": "TOPIX Mid400"}
    ) == Decimal(".1")
