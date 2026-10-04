"""Versioned Qlib research caches derived from immutable Japan publications."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import uuid

from backend.services.engine.data_platform.jp_file_lock import exclusive_file_lock
from backend.services.engine.data_platform.quantjp_hub import (
    QuantJPDataHub,
    _resolve_quantjp_data_dir,
)
from backend.services.engine.qlib_data_builder import QlibDataBuilder
from backend.shared.stock_utils import StockCodeUtil


JP_PROVIDER_CONTRACT_VERSION = 3
JP_PROVIDER_CACHE_DIR = "qlib_v3"
JP_RAW_PROVIDER_CACHE_DIR = "qlib_raw_v3"


def _validate_provider(path: Path, symbols: set[str]) -> None:
    calendar = path / "calendars/day.txt"
    instruments = path / "instruments/all.txt"
    if not calendar.is_file() or not calendar.read_text().strip():
        raise ValueError("JP research provider has no trading calendar")
    if not instruments.is_file():
        raise ValueError("JP research provider has no instrument history")
    actual = {
        line.split("\t")[0] for line in instruments.read_text().splitlines() if line
    }
    if not symbols or actual != symbols:
        raise ValueError("JP research provider instrument history is incomplete")
    for symbol in symbols:
        for field in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "factor",
            "jp_limit_buy",
            "jp_limit_sell",
        ):
            binary = path / "features" / symbol / f"{field}.day.bin"
            if not binary.is_file() or binary.stat().st_size < 8:
                raise ValueError(f"JP research provider is missing {symbol}/{field}")
    benchmark = path / "features/jp_topix/close.day.bin"
    if not benchmark.is_file() or benchmark.stat().st_size < 8:
        raise ValueError("JP research provider has no TOPIX benchmark")


def prepare_jp_rd_provider(
    data_dir: str | Path | None = None,
    *,
    publication: Path | None = None,
    price_basis: str = "adjusted",
) -> Path:
    """Build once per publication; never reuse an unversioned market cache."""
    if price_basis not in {"adjusted", "raw"}:
        raise ValueError("JP provider price basis must be adjusted or raw")
    root = Path(data_dir or _resolve_quantjp_data_dir()).resolve()
    publication = (publication or QuantJPDataHub(root).data_dir).resolve()
    if not publication.is_relative_to(root):
        raise ValueError("JP research publication escapes its source root")
    manifest = publication / "manifest.json"
    if not manifest.is_file():
        raise ValueError("JP research requires a complete immutable publication")
    hub = QuantJPDataHub(publication)
    symbols = {
        StockCodeUtil.to_qlib(symbol, market="JP")
        for symbol in hub.fetch_instrument_periods().symbol.unique()
    }
    identity = {
        "market": "JP",
        "data_version": publication.name,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "contract_version": JP_PROVIDER_CONTRACT_VERSION,
    }
    if price_basis == "raw":
        identity.update(
            price_basis="raw", contract_version=JP_PROVIDER_CONTRACT_VERSION
        )
    cache = root / ".rd_cache" / publication.name
    if not cache.resolve().is_relative_to(root):
        raise ValueError("JP research cache escapes its source root")
    output = cache / (
        JP_RAW_PROVIDER_CACHE_DIR if price_basis == "raw" else JP_PROVIDER_CACHE_DIR
    )
    with exclusive_file_lock(cache / ".qlib.lock"):
        if not output.resolve().is_relative_to(cache.resolve()):
            raise ValueError("JP research provider escapes its cache directory")
        if output.exists():
            metadata = output / "research_source.json"
            if not metadata.is_file() or json.loads(metadata.read_text()) != identity:
                raise ValueError("JP research provider source identity does not match")
            _validate_provider(output, symbols)
            return output
        staging = cache / (".qlib-" + uuid.uuid4().hex)
        try:
            QlibDataBuilder(
                hub, staging, market="JP", price_basis=price_basis
            ).build_all()
            _validate_provider(staging, symbols)
            (staging / "research_source.json").write_text(
                json.dumps(identity, ensure_ascii=False), encoding="utf-8"
            )
            staging.replace(output)
            return output
        finally:
            if staging.exists():
                if not staging.resolve().is_relative_to(cache.resolve()):
                    raise ValueError("JP research staging escapes its cache directory")
                shutil.rmtree(staging)
