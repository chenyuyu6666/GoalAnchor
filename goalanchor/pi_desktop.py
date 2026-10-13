"""PI-Desktop package builder and versioned, host-owned JSON bridge.

No desktop credentials or SDK dependencies enter the Python domain layer.
The JS host answers completion requests; existing review policy owns verdicts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import fields
from pathlib import Path
from typing import Any

from ._version import __version__
from .agent_runtime import AgentRequest, run_review_cycle_with_adapter
from .compat import _require_risk_declaration, _run_gate, find_skill_source
from .core import GoalContract, LifecycleState, WorkItem, transition
from .gates import ExecutePreflight, StructureReview, VerifyRecord, check_execute, check_verify, evaluate_plan
from .platforms import extract_result_payload, render_agent_prompt
from .review_service import ArbiterResult, MetaReviewResult, ReviewContext, ReviewerResult, ReviewRisk

PROTOCOL_VERSION = 1
MAX_BYTES = 1024 * 1024
_ASSETS = Path(__file__).parent / "_pi_desktop"
_GATES = frozenset({"plan", "verify", "execute", "plan-change", "evidence", "conclusion",
                    "experiment", "design", "artifact", "provenance", "closure", "review", "review-cycle"})


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def evaluate_preflight(record: Mapping[str, Any]) -> dict[str, Any]:
    """Check all gates before a human can approve exact operations."""
    record = _object(record, "record")
    contract = GoalContract.from_dict(record["contract"])
    if contract.stage is not LifecycleState.PLAN:
        raise ValueError("preflight requires the original PLAN contract")
    operations = record.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 32:
        raise ValueError("operations must contain 1 to 32 exact tool calls")
    for operation in operations:
        operation = _object(operation, "operation")
        tool = operation.get("tool")
        if (not isinstance(tool, str) or not tool.strip() or tool.startswith("goalanchor_")
                or tool in {"plugin_local_goalanchor_goalanchor_gate", "plugin_local_goalanchor_goalanchor_review"}):
            raise ValueError("operation tool must name a non-GoalAnchor tool")
        _object(operation.get("input"), "operation input")
        if len(json.dumps(operation, ensure_ascii=False).encode()) > 32768:
            raise ValueError("operation exceeds 32 KiB")
    items = tuple(WorkItem.from_dict(item) for item in record.get("work_items", ()))
    structure = StructureReview.from_dict(record["structure_review"])
    plan = evaluate_plan(contract, items, structure)
    verify = check_verify(VerifyRecord.from_dict(record["verify"]))
    prepared = transition(contract, LifecycleState.VERIFY)
    execute = check_execute(prepared, ExecutePreflight.from_dict(record["execute"]))
    return {
        "passed": plan.passed and verify.passed and execute.passed,
        "plan": plan.to_dict(), "verify": verify.to_dict(), "execute": execute.to_dict(),
        "contract": prepared.to_dict(), "operations": operations,
    }


class DesktopAdapter:
    def __init__(self, invoke: Callable[[AgentRequest], Mapping[str, Any]], model: str) -> None:
        self.call = invoke
        self.model = model
        self.bindings: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def invoke(self, request: AgentRequest) -> Mapping[str, Any]:
        raw = _object(self.call(request), "completion")
        if raw.get("modelKey") != self.model:
            raise ValueError("completion model does not match the current session model")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_BYTES:
            raise ValueError("completion text is empty or exceeds the bridge limit")
        result = extract_result_payload(text)
        types = {"reviewer": ReviewerResult, "arbiter": ArbiterResult, "meta_reviewer": MetaReviewResult}
        # Ignore noncritical future result fields; required fields still pass the domain validator.
        allowed = {field.name for field in fields(types[request.role.value])}
        result = {key: value for key, value in result.items() if key in allowed}
        with self.lock:
            self.bindings.append({
                "agent_id": request.agent_id, "profile_id": request.agent_id,
                "platform": "pi-desktop", "model": self.model, "role": request.role.value,
                "round_number": request.round_number, "usage": raw.get("usage"),
            })
            self.responses.append({"request": request.to_dict(), "result": result})
        return result

    def usage_report(self) -> list[dict[str, Any]]:
        return list(self.bindings)


def dispatch(
    payload: Mapping[str, Any], *, invoke: Callable[[AgentRequest], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = _object(payload, "bridge request")
    version = payload.get("protocol_version")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ValueError("unsupported PI-Desktop bridge protocol_version")
    operation = payload.get("operation")
    if operation == "preflight":
        return evaluate_preflight(payload["record"])
    if operation == "gate":
        phase = payload.get("phase")
        if phase not in _GATES:
            raise ValueError("unsupported gate phase")
        record = _object(payload.get("record"), "gate record")
        with tempfile.TemporaryDirectory(prefix="goalanchor-gate-") as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            return _run_gate(str(phase), path)
    if operation != "review":
        raise ValueError("unsupported bridge operation")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip() or "/" not in model:
        raise ValueError("review requires the current provider/model identity")
    request = _object(payload.get("request"), "review request")
    _require_risk_declaration(request)
    rounds = request.get("max_rounds", 2)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds not in (1, 2):
        raise ValueError("max_rounds must be 1 or 2")
    if invoke is None:
        raise ValueError("review requires a host completion transport")
    adapter = DesktopAdapter(invoke, model)
    result = run_review_cycle_with_adapter(
        ReviewContext.from_dict(request["context"]), ReviewRisk(request["risk"]), adapter, max_rounds=rounds,
    ).to_dict()
    result.update({"dispatch_mode": "host-completion", "main_model": model,
                   "protocol_version": PROTOCOL_VERSION, "agent_results": adapter.responses,
                   "warnings": [{"code": "VISIBLE_SUBAGENTS_UNAVAILABLE",
                                 "message": "Review uses isolated completions, not desktop child sessions."}]})
    return result


class StdioTransport:
    """Correlate parallel reviewer requests without sharing their contexts."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: dict[str, Future[Any]] = {}
        self.failed: Exception | None = None
        # Host closes stdin after the terminal result. A joined non-daemon reader
        # avoids CPython's buffered-stdin shutdown race on Windows.
        threading.Thread(target=self._read, daemon=False).start()

    def _read(self) -> None:
        try:
            while True:
                message = _read_json()
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    raise ValueError("completion response id must be a string")
                with self.lock:
                    future = self.pending.pop(request_id, None)
                if future is None:
                    raise ValueError("unknown or duplicate completion response id")
                if message.get("type") != "agent_response":
                    future.set_exception(ValueError("invalid completion response type"))
                elif message.get("error"):
                    future.set_exception(ValueError(str(message["error"])))
                else:
                    future.set_result(message.get("response"))
        except Exception as exc:
            with self.lock:
                self.failed = exc
                pending = list(self.pending.values())
                self.pending.clear()
            for future in pending:
                future.set_exception(exc)

    def invoke(self, request: AgentRequest) -> Mapping[str, Any]:
        request_id = uuid.uuid4().hex
        future: Future[Any] = Future()
        with self.lock:
            if self.failed is not None:
                raise self.failed
            self.pending[request_id] = future
            _write_json({"type": "agent_request", "id": request_id,
                         "prompt": render_agent_prompt(request), "request": request.to_dict()})
        return _object(future.result(timeout=95), "host completion response")


def _read_json() -> Mapping[str, Any]:
    line = sys.stdin.buffer.readline(MAX_BYTES + 2)
    if not line or len(line) > MAX_BYTES:
        raise ValueError("bridge input ended or exceeds 1 MiB")
    return _object(json.loads(line), "bridge message")


def _write_json(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def serve() -> int:
    try:
        payload = _read_json()
        transport = StdioTransport() if payload.get("operation") == "review" else None
        result = dispatch(payload, invoke=transport.invoke if transport else None)
        _write_json({"type": "result", "protocol_version": PROTOCOL_VERSION, "result": result})
        return 0
    except Exception as exc:
        _write_json({"type": "error", "protocol_version": PROTOCOL_VERSION, "error": str(exc)})
        return 2


def build_plugin(destination: Path, *, archive: Path | None = None) -> dict[str, str]:
    """Produce a portable directory; refuse overwrites and use stored zip for host compatibility."""
    destination = Path(destination).resolve()
    if destination.exists() or archive is not None and Path(archive).exists():
        raise FileExistsError("plugin destination or archive already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".goalanchor-build-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "plugin"
        shutil.copytree(_ASSETS, stage)
        shutil.copytree(find_skill_source(), stage / "skills" / "goalanchor")
        library = stage / "runtime" / "goalanchor"
        for path in Path(__file__).parent.rglob("*.py"):
            relative = path.relative_to(Path(__file__).parent)
            if relative.parts[0].startswith("_") and relative.parts[0] not in (
                "__init__.py", "__main__.py", "_version.py",
            ):
                continue
            target = library / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        shutil.copytree(find_skill_source(), library / "_skill")
        (stage / "runtime" / "bridge.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "from goalanchor.pi_desktop import serve\nraise SystemExit(serve())\n", encoding="utf-8",
        )
        manifest_path = stage / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = __version__
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        checksums = {str(path.relative_to(stage)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(stage.rglob("*")) if path.is_file()}
        (stage / "checksums.json").write_text(
            json.dumps({"algorithm": "sha256", "files": checksums}, indent=2) + "\n", encoding="utf-8",
        )
        shutil.move(str(stage), str(destination))
    if archive is not None:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED) as package:
            for path in sorted(destination.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(destination).as_posix())
    return {"directory": str(destination), "archive": str(archive) if archive else ""}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build a PI-Desktop plugin directory and optional .piplug")
    build.add_argument("--destination", type=Path, required=True)
    build.add_argument("--archive", type=Path)
    sub.add_parser("bridge", help="Internal versioned JSON-over-stdio bridge")
    args = parser.parse_args()
    if args.command == "bridge":
        return serve()
    try:
        print(json.dumps(build_plugin(args.destination, archive=args.archive), ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
