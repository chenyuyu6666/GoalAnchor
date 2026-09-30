from __future__ import annotations

import re

from goalanchor import __version__


def test_version_is_semver() -> None:
    assert re.fullmatch(r"0\.\d+\.\d+", __version__)
