#!/usr/bin/env python3
# GoalAnchor modification (Apache-2.0): adds bounded GitHub requests and readable network/JSON errors; see UPSTREAM_SOURCES.md.
"""Shared GitHub helpers for skill install scripts."""

from __future__ import annotations

import os
import urllib.request


DEFAULT_TIMEOUT_SECONDS = 30.0


def github_request(
    url: str,
    user_agent: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch a GitHub resource with a bounded network wait.

    A timeout is deliberately part of the shared helper so both the listing and
    installation flows fail with a bounded, catchable network error instead of
    hanging indefinitely on a proxy or unavailable GitHub endpoint.
    """
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    headers = {"User-Agent": user_agent}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def github_api_contents_url(repo: str, path: str, ref: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
