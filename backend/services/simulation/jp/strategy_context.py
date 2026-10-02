"""Register Japan's immutable raw-price view for public strategy execution."""

import os

from backend.services.engine.data_platform.quantjp_hub import _resolve_quantjp_data_dir
from backend.services.engine.qlib_app.services.market_strategy_context import (
    StrategyContextSpec,
)
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)
from backend.shared.stock_utils import StockCodeUtil
from .service import execution_data


def to_provider_instrument(code):
    if str(code).upper() in {"TOPIX", "JP_TOPIX"}:
        return "jp_topix"
    return StockCodeUtil.to_qlib(code, market="JP")


def prepare_context(request):
    data = execution_data(request.jp_data_version)
    root = _resolve_quantjp_data_dir()
    provider = prepare_jp_rd_provider(
        root, publication=data.hub.data_dir, price_basis="raw"
    )
    request.jp_data_version = data.hub.data_dir.name
    environment = {"QM_QUANTJP_DATA_DIR": str(root)}
    if os.environ.get("QM_JP_TRADING_UNITS_FILE"):
        environment["QM_JP_TRADING_UNITS_FILE"] = os.environ["QM_JP_TRADING_UNITS_FILE"]
    return StrategyContextSpec(
        provider_uri=str(provider),
        region="cn",
        data_version=request.jp_data_version,
        instrument_mapper="backend.services.simulation.jp.strategy_context.to_provider_instrument",
        environment=environment,
        feature_snapshot_reader="backend.services.simulation.jp.feature_snapshot.create_reader",
    )
