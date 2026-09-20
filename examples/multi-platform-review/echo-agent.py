"""Offline smoke-test agent for the multi-platform dispatch example.

It reads the GoalAnchor review prompt (argv ``-p`` or stdin), detects the
requested role, and replies with one valid GoalAnchor result JSON. Use it to
verify dispatch wiring before pointing profiles at real platform CLIs.
"""

from __future__ import annotations

import json
import re
import sys


def main() -> int:
    argv = sys.argv[1:]
    mode = argv[0] if argv else "clean"
    if "-p" in argv:
        prompt = argv[argv.index("-p") + 1]
    else:
        prompt = sys.stdin.read()
    role_match = re.search(r"# GoalAnchor (\w+) review", prompt)
    id_match = re.search(r'agent_id="([^"]+)"', prompt)
    fingerprint_match = re.search(r'context_fingerprint="([0-9a-f]{64})"', prompt)
    round_match = re.search(r"round (\d+)", prompt)
    if not (role_match and id_match and fingerprint_match and round_match):
        print("echo-agent: prompt does not look like a GoalAnchor request", file=sys.stderr)
        return 2
    role = role_match.group(1)
    agent_id = id_match.group(1)
    fingerprint = fingerprint_match.group(1)
    round_number = int(round_match.group(1))
    if role == "reviewer":
        payload = {
            "reviewer_id": agent_id,
            "round_number": round_number,
            "verdict": "PASS",
            "findings": [],
            "isolated": True,
            "context_fingerprint": fingerprint,
        }
    elif role == "arbiter":
        reviewer_ids = sorted(set(re.findall(r'"reviewer_id": "(reviewer-\d+)"', prompt)))
        payload = {
            "arbiter_id": agent_id,
            "round_number": round_number,
            "verdict": "PASS",
            "summary": "offline echo agent agrees with reviewers",
            "rationale": "smoke test",
            "reviewer_ids": reviewer_ids,
            "context_fingerprint": fingerprint,
            "disagrees_with_reviewers": False,
        }
    else:
        arbiter_match = re.search(r"arbiter_id: (\S+)", prompt)
        payload = {
            "reviewer_id": agent_id,
            "round_number": round_number,
            "arbiter_id": arbiter_match.group(1) if arbiter_match else "arbiter",
            "verdict": "PASS",
            "summary": "offline echo agent agrees with arbiter",
            "findings": [],
            "context_fingerprint": fingerprint,
            "agrees_with_arbiter": True,
            "isolated": True,
        }
    text = json.dumps(payload, ensure_ascii=False)
    if mode == "claude-like":
        print(json.dumps({"type": "result", "result": text}))
    elif mode == "prose-like":
        print("Review complete:\n" + text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
