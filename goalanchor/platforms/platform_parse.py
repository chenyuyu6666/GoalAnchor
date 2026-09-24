"""Subprocess execution and platform-output parsing.

Keeps the trust boundary in one place: platform CLIs are untrusted producers,
so their stdout goes through envelope-aware parsing before it can be treated
as a review result, and command construction/errors are normalized to the
runtime error type.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from ..agent_runtime import AgentRuntimeError
from .platform_common import _ENVELOPE_KEYS, _MAX_ENVELOPE_DEPTH, _RESULT_KEYS


def _json_candidates(text: str):
    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                yield text[start : index + 1]
                start = None


def _unwrap_payload(payload: Mapping[str, object], *, depth: int) -> Mapping[str, object]:
    if depth > _MAX_ENVELOPE_DEPTH:
        raise AgentRuntimeError(
            f"agent platform output nests platform envelopes more than {_MAX_ENVELOPE_DEPTH} levels"
        )
    if any(key in payload for key in _RESULT_KEYS):
        return payload
    for key in _ENVELOPE_KEYS:
        inner = payload.get(key)
        if isinstance(inner, Mapping):
            return _unwrap_payload(inner, depth=depth + 1)
        if isinstance(inner, str) and inner.strip():
            try:
                return extract_result_payload(inner)
            except AgentRuntimeError:
                continue
    return payload


def extract_result_payload(stdout: str) -> Mapping[str, object]:
    """Normalize one agent CLI's stdout into the GoalAnchor result object.

    Handles: a direct JSON object, a platform envelope such as Claude's
    ``{"type": "result", "result": "<text>"}``, and JSON embedded in prose or
    fenced code blocks. CLIs like ``codex exec`` echo the whole prompt (which
    itself contains JSON) into stdout before the final answer, so candidates
    carrying result keys are preferred and the last one wins.
    """

    text = (stdout or "").strip()
    if not text:
        raise AgentRuntimeError("agent platform returned empty stdout")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, Mapping):
        return _unwrap_payload(decoded, depth=1)
    last_result: Mapping[str, object] | None = None
    first_mapping: Mapping[str, object] | None = None
    for candidate in _json_candidates(text):
        try:
            inner = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(inner, Mapping):
            continue
        payload = _unwrap_payload(inner, depth=1)
        if any(key in payload for key in _RESULT_KEYS):
            last_result = payload
        if first_mapping is None:
            first_mapping = payload
    if last_result is not None:
        return last_result
    if first_mapping is not None:
        return first_mapping
    raise AgentRuntimeError("agent platform stdout does not contain a JSON result object")


def _run_subprocess(
    argv: Sequence[str],
    *,
    input_text: str | None,
    timeout_seconds: float,
    env: Mapping[str, str],
) -> tuple[str, str]:
    resolved = (shutil.which(argv[0]) or argv[0],) + tuple(argv[1:])
    merged_env = {**os.environ, **env}
    try:
        completed = subprocess.run(
            resolved,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=merged_env,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentRuntimeError(f"agent platform command failed for {argv[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:]
        raise AgentRuntimeError(
            f"agent platform command exited with code {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    if not completed.stdout.strip():
        # Empty stdout is a transport-level signal (truncated write, wrapper
        # glitch), so it is raised here to stay inside the retry loop.
        raise AgentRuntimeError("agent platform returned empty stdout")
    return completed.stdout, completed.stderr


def _materialize_argv(argv: Sequence[str], prompt: str) -> tuple[str, ...]:
    return tuple(prompt if item == "{prompt}" else item for item in argv)


_TOKENS_USED_RE = re.compile(
    r"tokens\s+used\s*:?\s*\n?\s*([0-9][0-9,]*)", re.IGNORECASE
)


def _extract_tokens_used(stderr: str) -> int | None:
    """Best-effort token usage from platform stderr (e.g. ``codex exec``)."""

    match = _TOKENS_USED_RE.search(stderr or "")
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))
