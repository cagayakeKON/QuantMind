"""JP model entry keeps the original Qlib builder and research interfaces."""

from datetime import date
import pytest
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.backtest_service_runtime import (
    QlibBacktestServiceRuntimeMixin,
)

# Compatibility for fixture-only imports in unrelated publication tests.
from backend.tests.jp_standard_fixtures import model_data, runtime_factory  # noqa: F811

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


@pytest.mark.asyncio
async def test_research_jp_reads_dated_publication_without_cn_table(model_data):  # noqa: F811
    from backend.services.api.routers import research_service as research

    records = await research._load_sdl_day_map(None, date(2026, 9, 29), market="JP")
    assert records["JP72030"]["close"] == 50
    assert records["JP72030"]["currency"] == "JPY"
    assert all(code.startswith("JP") for code in records)
    with pytest.raises(ValueError, match="dated publication"):
        research._get_sdl_table("JP")
    assert research._get_sdl_table("CN") == "stock_daily_latest"
    assert research._get_sdl_table("HK") == "stock_daily_latest_hk"


@pytest.mark.asyncio
async def test_dispatch_retains_existing_market_runtime(runtime_factory):  # noqa: F811
    def original_cleanup():
        raise RuntimeError("original runtime reached")

    service = runtime_factory("store")
    service._cleanup_stale_runs = original_cleanup
    for market in ("JP", "CN"):
        with pytest.raises(RuntimeError, match="original runtime reached"):
            await QlibBacktestServiceRuntimeMixin.run_backtest(
                service, QlibBacktestRequest(market=market)
            )
