"""Sync the bundled Skill copy (``goalanchor/_skill``) from ``skills/goalanchor``.

``skills/goalanchor`` is the single source; the copy inside the package is
what ships in the wheel so ``python -m goalanchor install`` works for pip users.
Run this after editing the Skill, and use ``--check`` in CI to detect drift.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "skills" / "goalanchor"
BUNDLED = REPO_ROOT / "goalanchor" / "_skill"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[Path, str]:
    return {
        item.relative_to(root): _hash(item)
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def sync() -> None:
    BUNDLED.mkdir(parents=True, exist_ok=True)
    for relative in _files(SOURCE):
        src = SOURCE / relative
        dst = BUNDLED / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    # remove files no longer present at the source
    source_files = set(_files(SOURCE))
    for stale in set(_files(BUNDLED)) - source_files:
        (BUNDLED / stale).unlink()


def check() -> bool:
    return _files(SOURCE) == _files(BUNDLED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the bundle drifted")
    args = parser.parse_args()
    if args.check:
        if check():
            print("bundled skill is in sync")
            return 0
        print("goalanchor/_skill drifted from skills/goalanchor; run scripts/sync_skill_bundle.py")
        return 1
    sync()
    print(f"synced {BUNDLED.relative_to(REPO_ROOT)} from {SOURCE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
