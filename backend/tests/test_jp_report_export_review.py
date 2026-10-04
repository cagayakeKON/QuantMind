"""JP reports use the analysis-date master through the registered raw provider."""

from pathlib import Path
from unittest.mock import Mock

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.trading_agents import report_exporter as exporter
from backend.services.engine.trading_agents.progress import ProgressTracker
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture

snapshot = snapshot_fixture


@pytest.fixture
def publication(snapshot, tmp_path, monkeypatch):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.master SET CoName='Historical name' "
            "WHERE Date <= '2026-09-29'"
        )
        conn.execute(
            "UPDATE research.master SET CoName='Future name' WHERE Date='2026-09-30'"
        )
    root = tmp_path / "published"
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", "")
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.setattr(exporter, "_RESULTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(exporter, "_convert_to_pdf", Mock(return_value=False))
    return root


@pytest.mark.parametrize("ticker", ["JP72030", "72030.JP", "7203.T", "JP216A0"])
def test_reports_resolve_historical_company_name_and_common_market_directory(
    publication, ticker
):
    result = exporter.export_report_files(
        ticker, "2026-09-29", "BUY", ProgressTracker(), "JP"
    )
    assert result["error"] is None
    assert Path(result["dir"]).parts[-2:] == ("日本市场", "Historical name")
    report = Path(result["md"])
    assert report.name == f"Historical name{ticker}_2026-09-29_投研分析报告.md"
    text = report.read_text(encoding="utf-8")
    assert text.startswith(f"# Historical name({ticker}) 投研分析报告")
    assert "Future name" not in text
    assert "交易日期**: 2026-09-29" in text


def test_report_names_do_not_fallback_to_future_or_retired_master(publication):
    assert exporter._resolve_stock_name("JP72030", "JP", "2010-01-01") == ""
    assert exporter._resolve_stock_name("JP72030", "JP") == ""
    assert (
        exporter._resolve_stock_name("JP13370", "JP", "2026-09-28") == "Historical name"
    )
    assert exporter._resolve_stock_name("JP13370", "JP", "2026-09-29") == ""
    assert exporter._resolve_stock_name("JP72030", "JP", "2026-09-30") == "Future name"


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_existing_markets_keep_original_two_argument_lookup(
    tmp_path, monkeypatch, market
):
    lookup = Mock(return_value="Original name")
    monkeypatch.setattr(exporter, "_resolve_stock_name", lookup)
    monkeypatch.setattr(exporter, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(exporter, "_convert_to_pdf", Mock(return_value=False))
    result = exporter.export_report_files(
        "original", "2026-09-29", "BUY", ProgressTracker(), market
    )
    assert result["error"] is None
    lookup.assert_called_once_with("original", market)
