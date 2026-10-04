"""JP adjusted Qlib and original SDK order units; shared publication fixture."""

from datetime import date
import duckdb
import pytest
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture
DAYS = [date(2026, 9, d) for d in (28, 29, 30)]


@pytest.fixture
def native(snapshot, tmp_path, monkeypatch):
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    with duckdb.connect(str(snapshot)) as db:
        db.execute("UPDATE research.daily_prices SET AdjFactor=1,ExRT='',Vo=100000")
        db.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-25','1'),('2026-10-01','1'),('2026-10-02','1')"
        )
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return snapshot, root
