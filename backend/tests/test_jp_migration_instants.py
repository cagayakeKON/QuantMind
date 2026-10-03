"""PostgreSQL JSON fractional timestamps in the new one-time JP migration."""

from datetime import datetime, timezone

import pytest

from backend.services.simulation.jp.replay_migration import _utc_instant


@pytest.mark.parametrize("fraction", ["1", "12", "123", "1234", "12345", "123456"])
@pytest.mark.parametrize("offset,hour", [("Z", 0), ("+08:00", 8), ("+09:00", 9)])
def test_postgres_fraction_preserves_the_exact_aware_utc_instant(
    fraction, offset, hour
):
    value = f"2026-10-04T{hour:02d}:00:00.{fraction}{offset}"
    expected = datetime(
        2026, 10, 4, microsecond=int(fraction.ljust(6, "0")), tzinfo=timezone.utc
    )
    assert _utc_instant(value) == expected


def test_missing_timezone_still_rejected():
    with pytest.raises(ValueError, match="explicit UTC offset"):
        _utc_instant("2026-10-04T09:00:00")
