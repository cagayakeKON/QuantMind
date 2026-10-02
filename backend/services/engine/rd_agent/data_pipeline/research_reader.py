"""Read a configured market provider without changing the engine's Qlib state."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pandas as pd

from ..market_adapters.base import DataConfig


def copy_research_hdf(source: str, target: Path, instruments: list[str]) -> None:
    """Materialize a selected pool in the common task HDF format."""
    frame = pd.read_hdf(source, key="data", where=f"instrument in {instruments!r}")
    if frame.empty:
        raise ValueError("Selected research pool has no HDF observations")
    with pd.HDFStore(source, mode="r") as original:
        attrs = original.get_storer("data").attrs
        identity = {
            key: getattr(attrs, key)
            for key in ("market", "data_version", "contract_version")
        }
    frame.to_hdf(target, key="data", mode="w", format="table")
    with pd.HDFStore(target, mode="a") as output:
        for key, value in identity.items():
            setattr(output.get_storer("data").attrs, key, value)


def read_research_features(
    data: DataConfig, region: str, start: str, end: str
) -> pd.DataFrame:
    """Return the common factor-backtest frame from an isolated Qlib reader."""
    script = """
import json, sys
import qlib
from qlib.data import D
config = json.loads(sys.argv[1])
qlib.init(provider_uri=config['data']['provider_uri'], region=config['region'])
universe = config['data']['market']
instruments = D.instruments(market=universe) if isinstance(universe, str) else universe
fields = ['$open', '$high', '$low', '$close', '$volume', '$amount', '$factor']
frame = D.features(instruments, fields,
                   start_time=config['start'], end_time=config['end'], freq='day')
if frame.empty:
    raise ValueError('Configured market research data is empty')
frame.to_hdf(sys.argv[2], key='data', mode='w')
"""
    config = {"data": asdict(data), "region": region, "start": start, "end": end}
    with tempfile.TemporaryDirectory(prefix="rd-provider-") as directory:
        output = Path(directory) / "features.h5"
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(config), str(output)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(
                f"Research provider read failed: {result.stderr[-2000:]}"
            )
        return pd.read_hdf(output, key="data")
