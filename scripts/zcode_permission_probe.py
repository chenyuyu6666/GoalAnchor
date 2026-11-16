"""Probe: does the ZCode app-server ask for permission before a tool call?

This is an experiment, not a product API. It subclasses the existing ProbeClient
and, instead of denying every host interaction, records
``interaction/requestPermission`` and answers "allow" so the tool call can
proceed. The goal is one fact: whether the host asks the client before running
a tool, which would make a zero-plugin enforcement path possible (the host
itself gates the call and GoalAnchor decides the answer).

Run:
    python scripts/zcode_permission_probe.py --output <new json path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.zcode_protocol_probe import ProbeClient, ProtocolError  # noqa: E402

CLI = Path(r"D:\AIWorkspace\Tools\ZCode\resources\glm\zcode.cjs")
PROVIDER = "magpie"
MODEL = "auto/deepseek-v4.1-flash"


class PermissionObservingClient(ProbeClient):
    """Records host permission requests and allows them so the tool call runs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.permission_requests: list[dict[str, Any]] = []
        self.user_input_requests: list[dict[str, Any]] = []

    def _callback(self, frame: dict[str, Any], deadline: float) -> None:
        method = frame.get("method")
        if method == "interaction/requestPermission":
            self.permission_requests.append(frame)
            self._send({"id": frame["id"], "result": {"decision": "allow"}}, deadline)
            return
        if method == "interaction/requestUserInput":
            self.user_input_requests.append(frame)
            self._send({"id": frame["id"], "result": {"action": "cancel"}}, deadline)
            return
        super()._callback(frame, deadline)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--timeout", default=120.0, type=float)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists")

    from scripts.zcode_protocol_probe import NdjsonTransport

    transport = NdjsonTransport([args.node, str(CLI), "app-server"], cwd=args.workspace.resolve())
    client = PermissionObservingClient(
        transport,
        workspace=args.workspace.resolve(),
        provider=PROVIDER,
        model=MODEL,
        timeout=args.timeout,
        reasoning_level="medium",
        prompt=args.prompt,
        capture_response=True,
    )
    try:
        result = client.run(send=True)
    except ProtocolError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        try:
            transport.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    evidence = {
        "question": "Does the ZCode app-server request permission before a tool call?",
        "provider": PROVIDER,
        "model": MODEL,
        "prompt": args.prompt,
        "permission_requests_seen": len(client.permission_requests),
        "permission_requests": client.permission_requests[:5],
        "user_input_requests_seen": len(client.user_input_requests),
        "tool_frames": [f for f in client.frames if "tool" in json.dumps(f).lower()][:10],
        "run_result": result,
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({
        "permission_requests_seen": evidence["permission_requests_seen"],
        "user_input_requests_seen": evidence["user_input_requests_seen"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
