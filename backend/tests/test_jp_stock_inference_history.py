"""JP identifiers and market defaults join the common inference history reader."""

from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from backend.services.api.routers import model_training as models
from backend.services.api import stock_terminal_sources
from backend.shared.stock_utils import StockCodeUtil


def test_common_pred_reader_keeps_alphanumeric_codes_and_security_class(tmp_path):
    pd.DataFrame(
        {
            "instrument": [
                StockCodeUtil.to_qlib("JP130A0"),
                "JP130A1",
                "72030.JP",
                StockCodeUtil.to_qlib("JP130A0"),
            ],
            "datetime": pd.to_datetime(["2026-09-28"] * 3 + ["2026-09-30"]),
            "pred": [0.3, 0.8, 0.5, 0.9],
        }
    ).to_parquet(tmp_path / "pred.parquet", index=False)
    models._PRED_HIST_CACHE.clear()
    result = models._read_stock_pred_history(
        str(tmp_path), "JP130A0", date(2026, 9, 1), "model", date(2026, 9, 29)
    )
    assert len(result) == 1
    assert result[0]["fusion_score"] == 0.3
    assert result[0]["score_rank"] == 3
    assert result[0]["total_in_market"] == 3
    assert result[0]["trade_date"] == "2026-09-28"
    other_class = models._read_stock_pred_history(
        str(tmp_path), "130A1.JP", date(2026, 9, 1), "model"
    )
    assert other_class[0]["fusion_score"] == 0.8


def test_legacy_pred_reader_retains_numeric_matching_and_ranking(tmp_path):
    pd.DataFrame(
        {
            "instrument": ["sh600519", "SZ000001", "BJ830001"],
            "datetime": pd.to_datetime(["2026-09-28"] * 3),
            "pred": [0.3, 0.8, 0.9],
        }
    ).to_parquet(tmp_path / "pred.parquet", index=False)
    models._PRED_HIST_CACHE.clear()
    result = models._read_stock_pred_history(
        str(tmp_path), "600519", date(2026, 9, 1), "model"
    )
    assert result[0]["fusion_score"] == 0.3
    assert result[0]["score_rank"] == 2
    assert result[0]["total_in_market"] == 2


@pytest.mark.asyncio
async def test_jp_default_lookup_uses_existing_market_argument(tmp_path, monkeypatch):
    record = {
        "model_id": "jp-default",
        "storage_path": str(tmp_path),
        "metadata_json": {"market": "JP"},
    }
    lookup = AsyncMock(return_value=record)

    def reader(*args):
        return [{"fusion_score": 0.5}]

    monkeypatch.setattr(models.model_registry_service, "get_default_model", lookup)
    monkeypatch.setattr(models, "_read_stock_pred_history", reader)
    items, model = await models._load_stock_pred_history(
        tenant_id="t",
        user_id="u",
        model_id=None,
        sym="JP130A0",
        cutoff=date(2026, 9, 1),
    )
    lookup.assert_awaited_once_with(tenant_id="t", user_id="u", market="JP")
    assert items == [{"fusion_score": 0.5}]
    assert model == record
    record["metadata_json"]["market"] = "CN"
    assert await models._load_stock_pred_history(
        tenant_id="t",
        user_id="u",
        model_id=None,
        sym="JP130A0",
        cutoff=date(2026, 9, 1),
    ) == ([], None)


@pytest.mark.asyncio
async def test_shared_history_route_keeps_jp_identity_and_model_options(
    tmp_path, monkeypatch
):
    pd.DataFrame(
        {
            "instrument": [StockCodeUtil.to_qlib("JP130A0")],
            "datetime": pd.to_datetime(["2026-09-29"]),
            "pred": [0.5],
        }
    ).to_parquet(tmp_path / "pred.parquet", index=False)
    record = {
        "model_id": "jp-default",
        "storage_path": str(tmp_path),
        "metadata_json": {"market": "JP"},
    }
    defaults = AsyncMock(return_value=record)
    options = AsyncMock(return_value=[record])
    monkeypatch.setattr(models.model_registry_service, "get_default_model", defaults)
    monkeypatch.setattr(models.model_registry_service, "list_models", options)
    params_seen = []

    class Result:
        def mappings(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

    class Session:
        async def execute(self, statement, params=None):
            params_seen.append(params or {})
            return Result()

    @asynccontextmanager
    async def session(**kwargs):
        yield Session()

    monkeypatch.setattr(models, "get_session", session)
    source = SimpleNamespace(
        universe=lambda asof: (
            pd.DataFrame(
                {
                    "Symbol": ["130A0.JP"],
                    "Name": ["日本銘柄"],
                    "rs_hyname": ["業種"],
                    "board": ["Growth"],
                }
            ),
            asof,
        )
    )
    monkeypatch.setattr(
        stock_terminal_sources, "terminal_source", lambda **kwargs: source
    )
    response = await models.get_stock_inference_history(
        symbol="JP130A0",
        days=180,
        model_id=None,
        end_date="2026-09-29",
        current_user={"tenant_id": "t", "user_id": "u"},
    )
    assert response["normalized_symbol"] == "JP130A0"
    assert response["name"] == "日本銘柄"
    assert response["board"] == "Growth"
    assert response["items"][0]["fusion_score"] == 0.5
    assert response["score_source"] == "pred_parquet"
    assert response["models"][0]["model_id"] == "jp-default"
    options.assert_awaited_once_with(tenant_id="t", user_id="u", market="JP")
    assert set(params_seen[0]["syms"]) == {"130A0.JP", "JP130A0", "130a0.jp", "jp130a0"}
