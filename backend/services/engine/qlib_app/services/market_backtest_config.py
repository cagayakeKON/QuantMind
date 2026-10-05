"""Market data and Exchange parameters for the existing Qlib execution path."""

import asyncio
import hashlib
import json

from backend.services.engine.data_platform.jp_publication import publication_path
from backend.services.engine.data_platform.jp_qlib_limits import (
    execution_limit_expressions,
)
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)


def _prepare_jp_request(request):
    if "min_commission" not in request.model_fields_set:
        request.min_commission = 0.0
    hub = QuantJPDataHub()
    root = hub._publication_root.resolve()
    if request.jp_data_version:
        publication = (root / "versions" / request.jp_data_version).resolve()
        if not publication.is_relative_to(root / "versions"):
            raise ValueError("JP publication escapes its source root")
    else:
        publication = publication_path(root, raw=True)
    # The standard adjusted provider uses Qlib's numeric $factor contract.
    provider = prepare_jp_rd_provider(root, publication=publication)
    request.market = "JP"
    request.jp_data_version = publication.name
    request.qlib_provider_uri = str(provider)
    request.qlib_region = "cn"
    if request.benchmark.upper() in {"SH000300", "SH000300.SH", "TOPIX", "JP_TOPIX"}:
        request.benchmark = "jp_topix"


async def prepare_market_batch_request(request):
    """Pin a data publication before both standalone runs and optimizer clones."""
    if (
        request.market == "JP"
        or "jp_data" in str(request.qlib_provider_uri or "").lower()
    ):
        await asyncio.to_thread(_prepare_jp_request, request)


def serialize_market_batch_request(request):
    return request.model_dump(mode="json")


def configure_market_strategy(request, strategy):
    """Bind Japan's pinned features through the existing filter extension."""
    if request.market != "JP":
        return strategy
    from qlib.strategy.base import BaseStrategy
    from qlib.utils import init_instance_by_config
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )
    from backend.services.simulation.jp.feature_snapshot import JPFeatureSnapshotReader
    from backend.shared.fundamental_aligner import FundamentalAligner

    strategy = init_instance_by_config(strategy, accept_types=BaseStrategy)
    if getattr(strategy, "use_fundamental_filter", False):
        strategy._market_fundamental_aligner = FundamentalAligner(
            snapshot_loader=JPFeatureSnapshotReader(
                LOCAL_MARKET_PROVIDERS["JP"].open(request.jp_data_version)
            )
        )
    return strategy


def configure_market_exchange(request, exchange):
    """Existing markets keep their original Exchange configuration verbatim."""
    if request.market != "JP":
        return exchange
    hub = QuantJPDataHub()
    publication = hub._publication_root / "versions" / request.jp_data_version
    manifest = json.loads((publication / "manifest.json").read_text("utf-8"))
    units = manifest.get("trading_units")
    units_path = None
    if units:
        units_path = (publication / units["path"]).resolve()
        if (
            not units_path.is_relative_to(publication.resolve())
            or hashlib.sha256(units_path.read_bytes()).hexdigest() != units["sha256"]
        ):
            raise ValueError("Published JP trading units fail integrity validation")
    commission = (
        request.buy_cost if request.buy_cost is not None else request.commission
    )
    return {
        **exchange,
        "class": "JpExchange",
        "module_path": "backend.services.engine.qlib_app.utils.jp_exchange",
        "kwargs": {
            **exchange["kwargs"],
            "freq": "day",
            "start_time": request.start_date,
            "end_time": request.end_date,
            "deal_price": request.deal_price,
            "commission": commission,
            "min_commission": request.min_commission,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "min_transfer_fee": 0.0,
            "impact_cost_coefficient": request.impact_cost_coefficient,
            # Factor-only strategy proposals have no instrument/date context.
            # The official order callback applies the published dated unit.
            "trade_unit": None,
            "limit_threshold": execution_limit_expressions(request.deal_price),
            "volume_threshold": ("cum", "$volume * $volume_factor / $factor"),
            "backtest_id": exchange["kwargs"]["backtest_id"],
            "trading_units_path": str(units_path) if units_path else None,
        },
    }
