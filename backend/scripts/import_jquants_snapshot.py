"""Read-only bootstrap of local J-Quants data into QuantMind's JP dataset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path("data/quantjp"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    report = import_jquants_snapshot(
        args.source,
        args.destination,
        start=args.start,
        end=args.end,
        progress=lambda name, count: print(f"{name}: {count} partitions", flush=True),
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
