"""Every persistence read preserves JP prices and the existing market display."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.services.engine.qlib_app.services import backtest_persistence as storage
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("market", ["JP", "CN"])
async def test_raw_jp_prices_on_single_and_batch_reads(monkeypatch, multiple, market):
    payload = {
        "backtest_id": "saved",
        "market": market,
        "config": {"market": market},
        "trades": [
            {"symbol": "JP216A0" if market == "JP" else "SH600036", "price": "100.5"}
        ],
    }
    rows = SimpleNamespace(
        mappings=lambda: SimpleNamespace(
            first=lambda: {"result_json": payload, "result_file_path": None}
        ),
        all=lambda: [(payload, "u", None, None)],
    )

    async def execute(*args):
        return rows

    @asynccontextmanager
    async def session(**kwargs):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(storage, "get_session", session)
    calls = []

    def normalize(trades):
        calls.append(trades)
        return [{**trades[0], "price": 3.86}]

    monkeypatch.setattr(RiskAnalyzer, "normalize_trades_for_display", normalize)
    store = storage.BacktestPersistence()
    result = (
        (await store.get_multiple_results(["saved"]))[0]
        if multiple
        else await store.get_result("saved")
    )
    assert result.trades[0]["price"] == ("100.5" if market == "JP" else 3.86)
    assert bool(calls) == (market != "JP")
