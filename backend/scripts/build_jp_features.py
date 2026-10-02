"""Generate the price-only JP training dataset in an isolated Qlib process."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.quantjp_hub import _resolve_quantjp_data_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=_resolve_quantjp_data_dir())
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    result = build_jp_features(
        **vars(args),
        progress=lambda phase, value: print(f"{phase}: {value}", flush=True),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
