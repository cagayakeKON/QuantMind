"""Japan data and experiment parameters for the existing research workflow."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path

from backend.services.engine.data_platform.quantjp_hub import (
    QuantJPDataHub,
    _resolve_quantjp_data_dir,
)
from backend.services.engine.data_platform.jp_qlib_limits import (
    execution_limit_expressions,
)
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    JP_PROVIDER_CACHE_DIR,
    JP_PROVIDER_CONTRACT_VERSION,
)

from backend.shared.stock_utils import StockCodeUtil

from . import register_adapter
from .base import BacktestConfig, DataConfig, MarketAdapter


@register_adapter
class JapanAdapter(MarketAdapter):
    market_id = "japan"
    market_name = "日股"
    description = "日本股票市场，J-Quants 日线与 TOPIX，沿用公共因子研究流程"

    def __init__(self):
        self.root = Path(_resolve_quantjp_data_dir()).resolve()
        self.publication = QuantJPDataHub(self.root).data_dir.resolve()
        self.cache = self.root / ".rd_cache" / self.publication.name

    def get_data_config(self) -> DataConfig:
        return DataConfig(
            provider_uri=self.get_qlib_provider_uri(),
            data_dir=str(self.publication),
            market="all",
            extra={
                "data_version": self.publication.name,
                "benchmark": "jp_topix",
                "research_context": (
                    "研究市场是日本股票（JP），原币 JPY，基准 TOPIX。"
                    "输入是固定发布版本的复权日线 OHLCV、成交额和真实复权因子，"
                    "保留退市证券及字母证券代码。公共 Qlib 研究组合用于因子评价，"
                    "其收益不能当作日本现金模拟成交或交收结果。"
                ),
                "rd_data_files": {
                    "all": str(self.cache / "daily_pv_all.h5"),
                    "debug": str(self.cache / "daily_pv_debug.h5"),
                },
            },
        )

    def get_qlib_provider_uri(self) -> str:
        return str(self.cache / JP_PROVIDER_CACHE_DIR)

    def get_backtest_config(self) -> BacktestConfig:
        # Qlib research portfolios are theoretical, not historical cash fills.
        # Region US supplies Qlib's generic stock defaults; the JP calendar and
        # benchmark come from this publication. Standard daily backtests use Qlib.
        from backend.services.engine.data_platform.jp_trading_units import (
            read_published_trading_units,
        )

        # Use the same integrity-checked dated units as ordinary JP backtests.
        read_published_trading_units(self.publication)
        manifest = json.loads((self.publication / "manifest.json").read_text("utf-8"))
        units = manifest.get("trading_units")
        units_path = str(self.publication / units["path"]) if units else None
        return BacktestConfig(
            region="us",
            limit_threshold=1,
            commission_rate=0.001,
            min_commission=0,
            extra={
                "exchange_kwargs": {
                    "trade_unit": None,
                    "exchange": {
                        "class": "JpExchange",
                        "module_path": "backend.services.engine.qlib_app.utils.jp_exchange",
                        "kwargs": {
                            "stamp_duty": 0,
                            "transfer_fee": 0,
                            "min_transfer_fee": 0,
                            "impact_cost_coefficient": 0,
                            "trading_units_path": units_path,
                        },
                    },
                    # The existing factor templates evaluate close-price
                    # portfolios; retain that convention and JP price bounds.
                    "deal_price": "close",
                    "limit_threshold": list(execution_limit_expressions("close")),
                    "volume_threshold": ["cum", "$volume * $volume_factor / $factor"],
                }
            },
        )

    def get_factor_set(self) -> dict[str, str]:
        from qlib.contrib.data.loader import Alpha158DL

        expressions, names = Alpha158DL.get_feature_config()
        return dict(zip(names, expressions, strict=True))

    def get_prop_setting_class(self) -> str:
        return "rdagent.app.qlib_rd_loop.conf.FactorBasePropSetting"

    def get_env_overrides(self) -> dict[str, str]:
        return {
            "QLIB_PROVIDER_URI": self.get_qlib_provider_uri(),
            "QLIB_FACTOR_RUNNER": (
                "backend.services.engine.rd_agent.configured_runner.MarketFactorRunner"
            ),
            "QLIB_FACTOR_TRAIN_END": os.getenv("QLIB_FACTOR_TRAIN_END", "2020-12-31"),
            "CHAT_STREAM": "false",
        }

    def get_research_config(
        self, universe: str, *, user_id: str | None = None, tenant_id: str | None = None
    ) -> dict:
        data = self.get_data_config()
        ref = (universe or "all").strip()
        if ref not in ("all", "pool:all"):
            from backend.shared.stock_pool.resolver import resolve_pool_sync

            if not ref.startswith(("pool:", "pool_id:", "list:", "file:")):
                ref = "pool:" + ref
            snapshot = resolve_pool_sync(
                ref,
                market="JP" if ref.startswith(("list:", "file:")) else None,
                strict=True,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            if snapshot.market != "JP" or snapshot.unfiltered or not snapshot.symbols:
                raise ValueError("请选择日本市场股票池，或使用全市场 all")
            symbols = sorted(
                {StockCodeUtil.to_qlib(s, market="JP") for s in snapshot.symbols}
            )
            history = Path(data.provider_uri) / "instruments/all.txt"
            available = {
                line.split("\t")[0] for line in history.read_text().splitlines() if line
            }
            if not set(symbols) <= available:
                raise ValueError("股票池包含当前日本发布版本没有行情的证券")
            data.market = symbols
        return {
            "data": asdict(data),
            "backtest": asdict(self.get_backtest_config()),
            "benchmark": "jp_topix",
        }

    def prepare_data(self) -> bool:
        from ..data_pipeline.jp_data import prepare_jp_rd_data
        from ..data_pipeline.jp_provider import prepare_jp_rd_provider

        prepare_jp_rd_provider(self.root, publication=self.publication)
        prepare_jp_rd_data(self.root, publication=self.publication)
        prepare_jp_rd_data(self.root, publication=self.publication, debug=True)
        return True

    def is_data_ready(self) -> bool:
        try:
            import hashlib
            import pandas as pd

            provider = Path(self.get_qlib_provider_uri())
            metadata = json.loads((provider / "research_source.json").read_text())
            if metadata != {
                "market": "JP",
                "data_version": self.publication.name,
                "manifest_sha256": hashlib.sha256(
                    (self.publication / "manifest.json").read_bytes()
                ).hexdigest(),
                "contract_version": JP_PROVIDER_CONTRACT_VERSION,
            }:
                return False
            if not all(
                (provider / p).is_file()
                for p in (
                    "calendars/day.txt",
                    "instruments/all.txt",
                    "features/jp_topix/close.day.bin",
                )
            ):
                return False
            for path in self.get_data_config().extra["rd_data_files"].values():
                with pd.HDFStore(path, mode="r") as store:
                    attrs = store.get_storer("data").attrs
                    if (
                        attrs.market != "JP"
                        or attrs.data_version != self.publication.name
                        or attrs.contract_version != 1
                    ):
                        return False
            return True
        except (OSError, ValueError, KeyError, AttributeError):
            return False
