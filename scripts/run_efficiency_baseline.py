"""Run the offline GoalAnchor efficiency baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from goalanchor.efficiency import run_efficiency_baseline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_efficiency_baseline(repeat=args.repeat)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
