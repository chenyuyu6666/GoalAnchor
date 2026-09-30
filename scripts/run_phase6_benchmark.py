"""Run GoalAnchor's deterministic Phase 6 benchmark."""

from __future__ import annotations

import json

from goalanchor.benchmark import run_phase6_benchmark


def main() -> int:
    result = run_phase6_benchmark()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["aligned_count"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
