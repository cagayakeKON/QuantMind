"""JP data catalogue, preview and user-controlled J-Quants sync schedule."""

from .global_market_console import make_market_router

router = make_market_router(
    market="JP", env_var="QM_QUANTJP_DATA_DIR", default_dir="/data/quantjp",
    sync_entry="backend.scripts.quantjp_daily_sync",
)
