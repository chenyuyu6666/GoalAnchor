"""Run with: python examples/workflow_demo.py --directory NEW_DIRECTORY.

The review reports are explicitly synthetic; this is a local protocol example.
"""
from __future__ import annotations

import sys

from goalanchor.workflow_cli import main

if __name__ == '__main__':
    raise SystemExit(main(['demo', *sys.argv[1:]]))
