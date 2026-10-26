"""Opt-in finite real-host smoke; never approves server execution requests."""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from goalanchor.codex_host import CodexClient


def answer_fixture(client: CodexClient, request: dict, directory: str) -> dict:
    from goalanchor.codex_controller import CodexController
    from goalanchor.workflow import Workflow, WorkflowPolicy
    from goalanchor.workflow_protocol import Baseline, Criterion, EventAuthority, canonical
    from goalanchor.workflow_store import WorkflowStore

    user = EventAuthority(secrets.token_bytes(32), "explicit-question-fixture")
    observer = EventAuthority(secrets.token_bytes(32), "host-controller")
    runtime = Workflow(WorkflowStore(Path(directory).parent / "questions.db"), user, observer,
                       WorkflowPolicy(directory, ()))
    base = Baseline("question-smoke", 1, "Ask a direction", "Wait for a verified fixture answer", (
        Criterion("answer", "Direction selected", canonical("Alpha"), "question"),))
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict()))
    params = request["params"]
    controller = CodexController(client, params["threadId"], params["turnId"])
    records = controller.questions(request, runtime, base.goal_id)
    events = {}
    for record in records:
        if not record.question.options:
            raise ValueError("Fixture requires explicit options")
        question = record.question
        events[question.question_id] = user.sign("user.answer", question.fingerprint, {
            "answer_id": "fixture-" + question.question_id,
            "selected": [question.options[0].option_id], "custom": None})
    controller.answer_questions(request["id"], runtime, events)
    return {"question_callback_answered": True, "question_answer_path": "signed-core-direction",
            "question_created_grants": len(runtime.store.list("grant", base.goal_id))}


def approve_fixture(client: CodexClient, request: dict, directory: str, host_binding: str,
                    dispatch_timeout: float = 60, verify_one_shot: bool = True,
                    review_model: str | None = None) -> dict:
    """Explicit test fixture, not a real user's authorization or model review."""
    from goalanchor.codex_controller import CodexController
    from goalanchor.codex_host import CodexTransportError, command_approval
    from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewVerdict
    from goalanchor.workflow import Workflow, WorkflowPolicy
    from goalanchor.workflow_protocol import Baseline, Criterion, EventAuthority, EvidenceSnapshot, Operation, canonical
    from goalanchor.workflow_store import WorkflowStore

    params = request["params"]
    try:
        command_approval(params)
    except CodexTransportError:
        client.respond(request["id"], {"decision": "decline"})
        return {"fixture_status": "UNSUPPORTED_SCOPE_DECLINED", "callback_fields": list(params),
                "scope_fields": {key: params.get(key) for key in ("additionalPermissions", "environmentId",
                                                                  "networkApprovalContext")}}
    allowed = {"Write-Output GOALANCHOR_APPROVAL_TEST", "Write-Output 'GOALANCHOR_APPROVAL_TEST'",
               'Write-Output "GOALANCHOR_APPROVAL_TEST"', "echo GOALANCHOR_APPROVAL_TEST"}
    pwsh = shutil.which("pwsh")
    if pwsh:
        # The callback command carries single backslashes as-is; only the
        # doubled variant (from JSON-escaped sources) needs normalization.
        for executable in {pwsh, pwsh.replace("\\", "\\\\")}:
            for flag in ("-Command", "-NoProfile -Command"):
                allowed.add(f'"{executable}" {flag} \'Write-Output GOALANCHOR_APPROVAL_TEST\'')
    if (params.get("command", "").strip().casefold() not in {value.casefold() for value in allowed}
            or Path(params["cwd"]).resolve() != Path(directory).resolve()):
        client.respond(request["id"], {"decision": "decline"})
        return {"fixture_status": "UNSUPPORTED_COMMAND_DECLINED", "fixture_command": params.get("command")}
    user = EventAuthority(secrets.token_bytes(32), "explicit-test-user-fixture")
    observer = EventAuthority(secrets.token_bytes(32), "host-controller")
    runtime = Workflow(WorkflowStore(Path(directory).parent / "controller.db"), user, observer,
                       WorkflowPolicy(directory, ("codex.command",), host_binding=host_binding))
    base = Baseline("host-smoke", 1, "Print a fixed marker", "Approve exact callback once", (
        Criterion("exit", "Observed command exit code is zero", canonical(0), "host-command"),
    ), required_operations=("host-command",))
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict()))
    snapshot = EvidenceSnapshot.capture("callback", "host-request", canonical(request).encode(),
                                        source="codex-app-server")
    runtime.add_evidence(base.goal_id, snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": base.goal_id, "snapshot": snapshot.to_dict()}))
    ctx = runtime.review_context(base.goal_id)
    review_meta: dict[str, Any] = {}
    if review_model:
        bundle, review_meta = _real_review_bundle(review_model, ctx, directory)
    else:
        names = ("A", "B")
        bundle = {"context": ctx.to_dict(), "risk": "HIGH", "round_number": 1, "max_rounds": 2,
                  "reviewer_results": [ReviewerResult(n, 1, ReviewVerdict.PASS, (), True, ctx.fingerprint).to_dict()
                                       for n in names],
                  "arbiter": ArbiterResult("arbiter", 1, ReviewVerdict.PASS, "fixture", "exact print", names,
                                           ctx.fingerprint, False).to_dict(),
                  "meta_review": MetaReviewResult("meta", 1, "arbiter", ReviewVerdict.PASS, "fixture", (),
                                                   ctx.fingerprint, True).to_dict(), "dispositions": {},
                  "bindings": [{"agent_id": n, "session_id": "fixture-" + n, "model": "synthetic-fixture"}
                               for n in (*names, "arbiter", "meta")]}
        from goalanchor.review_delivery import build_packets
        from goalanchor.workflow_protocol import fingerprint
        packets = build_packets(ctx, bundle, [])
        bundle["delivery_receipts"] = [
            {"agent_id": binding["agent_id"], "session_id": binding["session_id"],
             "input_digest": fingerprint(packets[binding["agent_id"]]),
             "transport_receipt_id": "synthetic-" + binding["session_id"], "status": "DELIVERED"}
            for binding in bundle["bindings"]]
    runtime.submit_review(base.goal_id, bundle, observer.sign("observer.review", ctx.fingerprint, bundle))
    operation = Operation("host-command", base.goal_id, base.fingerprint, "codex.command", canonical(params),
                          directory, "host-item")
    runtime.register_operation(operation)
    question = runtime.request_authorization(operation.operation_id).question
    runtime.answer(question.question_id, user.sign("user.answer", question.fingerprint,
                   {"answer_id": "fixture-answer", "selected": ["approve"], "custom": None}))
    controller = CodexController(client, params["threadId"], params["turnId"], host_binding=host_binding)
    controller.command(request)
    receipt = controller.dispatch(request["id"], runtime, operation.operation_id, "host-attempt",
                                  timeout=dispatch_timeout)
    result = {"fixture_status": receipt["state"],
              "review_provider": f"real-codex:{review_model}" if review_model else "synthetic-fixture",
              "approval_outcome": controller.approval_outcomes.get(request["id"], "NOT_ATTEMPTED")}
    if review_meta:
        result["real_review"] = review_meta
    if receipt["state"] == "SUCCEEDED":
        runtime.observe(base.goal_id, observer.sign("observer.observation", base.fingerprint, {
            "goal_id": base.goal_id, "baseline_digest": base.fingerprint, "criterion_id": "exit",
            "object_ref": "host-command", "actual": receipt["result"]["exit_code"], "attempt_id": "host-attempt"}))
        result["closure"] = runtime.close(base.goal_id)["state"]
    if verify_one_shot:
        try:
            controller.dispatch(request["id"], runtime, operation.operation_id, "host-attempt-2", timeout=60)
            result["second_attempt_without_new_approval"] = "DISPATCHED_VIOLATION"
        except Exception as exc:  # noqa: BLE001 - fixture records the blocking exception type only
            result["second_attempt_without_new_approval"] = "BLOCKED:" + type(exc).__name__
    return result


def _codex_exec_reply(model: str, prompt: str, cwd: str) -> tuple[str, str, int | None]:
    """Run one real codex exec turn; return (reply_text, thread_id, tokens)."""
    import subprocess

    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("codex executable not found")
    path = Path(executable)
    argv = None
    if path.suffix.lower() in {".cmd", ".ps1", ".bat"} or path.suffix == "":
        node = shutil.which("node")
        script = path.parent / "node_modules/@openai/codex/bin/codex.js"
        if node and script.is_file():
            argv = [node, str(script), "exec", "-m", model, "--json", "--skip-git-repo-check", prompt]
    if argv is None:
        argv = [executable, "exec", "-m", model, "--json", "--skip-git-repo-check", prompt]
    completed = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                               shell=False, timeout=240, cwd=cwd)
    reply, thread_id, tokens = "", None, None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        usage = event.get("usage")
        if isinstance(usage, dict) and all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")):
            tokens = usage["input_tokens"] + usage["output_tokens"]
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                reply = item["text"].strip()
    if completed.returncode != 0 or not reply:
        raise RuntimeError(f"codex exec failed with code {completed.returncode}; diagnostics withheld")
    return reply, thread_id or "unknown-thread", tokens


def _real_review_bundle(model: str, ctx, cwd: str) -> tuple[dict, dict]:
    """Real four-role review dispatch; every reply is parsed and identity-bound."""
    from zcode_goalanchor_bridge import _ARBITER_FIELDS as _ARBITER_ALLOWED
    from zcode_goalanchor_bridge import _META_FIELDS as _META_ALLOWED
    from zcode_goalanchor_bridge import _REVIEWER_FIELDS as _REVIEWER_ALLOWED

    from goalanchor.agent_runtime import AgentRequest, AgentRole, ReviewRisk
    from goalanchor.platforms.platform_prompt import render_agent_prompt
    from goalanchor.review_delivery import build_packets
    from goalanchor.review_service import ArbiterResult, ReviewerResult
    from goalanchor.workflow_protocol import fingerprint

    meta: dict[str, Any] = {"review_roles": [], "review_tokens": {}, "tolerated_extra_fields": {}}
    reviewer_dicts = []
    for reviewer_id in ("real-codex-reviewer-1", "real-codex-reviewer-2"):
        request = AgentRequest(role=AgentRole.REVIEWER, agent_id=reviewer_id, round_number=1,
                               risk=ReviewRisk.HIGH, context=ctx)
        reply, thread_id, tokens = _codex_exec_reply(model, render_agent_prompt(request, language="en"), cwd)
        raw = json.loads(reply)
        extra = sorted(set(raw) - _REVIEWER_ALLOWED)
        if extra:
            meta["tolerated_extra_fields"][reviewer_id] = extra
        parsed = {k: raw[k] for k in raw if k in _REVIEWER_ALLOWED}
        parsed.setdefault("context_fingerprint", ctx.fingerprint)
        reviewer_dicts.append(parsed)
        meta["review_roles"].append({"agent_id": reviewer_id, "verdict": parsed.get("verdict"),
                                     "thread_id": thread_id})
        meta["review_tokens"][reviewer_id] = tokens
    reviewer_results = tuple(ReviewerResult.from_dict(item) for item in reviewer_dicts)
    arbiter_request = AgentRequest(role=AgentRole.ARBITER, agent_id="real-codex-arbiter", round_number=1,
                                   risk=ReviewRisk.HIGH, context=ctx, reviewer_results=reviewer_results)
    reply, arbiter_thread, arbiter_tokens = _codex_exec_reply(
        model, render_agent_prompt(arbiter_request, language="en"), cwd)
    raw = json.loads(reply)
    extra = sorted(set(raw) - _ARBITER_ALLOWED)
    if extra:
        meta["tolerated_extra_fields"]["real-codex-arbiter"] = extra
    arbiter = {k: raw[k] for k in raw if k in _ARBITER_ALLOWED}
    arbiter.setdefault("context_fingerprint", ctx.fingerprint)
    meta["review_roles"].append({"agent_id": "real-codex-arbiter", "verdict": arbiter.get("verdict"),
                                 "thread_id": arbiter_thread})
    meta["review_tokens"]["real-codex-arbiter"] = arbiter_tokens
    meta_request = AgentRequest(role=AgentRole.META_REVIEWER, agent_id="real-codex-meta", round_number=1,
                                risk=ReviewRisk.HIGH, context=ctx, reviewer_results=reviewer_results,
                                arbiter_result=ArbiterResult.from_dict(arbiter))
    reply, meta_thread, meta_tokens = _codex_exec_reply(
        model, render_agent_prompt(meta_request, language="en"), cwd)
    raw = json.loads(reply)
    extra = sorted(set(raw) - _META_ALLOWED)
    if extra:
        meta["tolerated_extra_fields"]["real-codex-meta"] = extra
    meta_review = {k: raw[k] for k in raw if k in _META_ALLOWED}
    meta_review.setdefault("context_fingerprint", ctx.fingerprint)
    meta["review_roles"].append({"agent_id": "real-codex-meta", "verdict": meta_review.get("verdict"),
                                 "thread_id": meta_thread})
    meta["review_tokens"]["real-codex-meta"] = meta_tokens

    bundle = {"context": ctx.to_dict(), "risk": "HIGH", "round_number": 1, "max_rounds": 2,
              "reviewer_results": reviewer_dicts, "arbiter": arbiter, "meta_review": meta_review,
              "dispositions": {},
              "bindings": [{"agent_id": role["agent_id"], "session_id": role["thread_id"], "model": model}
                           for role in meta["review_roles"]]}
    packets = build_packets(ctx, bundle, [])
    bundle["delivery_receipts"] = [
        {"agent_id": binding["agent_id"], "session_id": binding["session_id"],
         "input_digest": fingerprint(packets[binding["agent_id"]]),
         "transport_receipt_id": binding["session_id"], "status": "DELIVERED"}
        for binding in bundle["bindings"]]
    return bundle, meta


def reconcile_fixture(client: CodexClient, request: dict, directory: str, host_binding: str) -> dict:
    """Force dispatch to UNKNOWN via a tiny timeout, then reconcile with observer proof."""
    from goalanchor.codex_controller import CodexController
    from goalanchor.codex_host import command_approval
    from goalanchor.review_service import ArbiterResult, MetaReviewResult, ReviewerResult, ReviewVerdict
    from goalanchor.workflow import Workflow, WorkflowPolicy
    from goalanchor.workflow_protocol import (
        Baseline,
        Criterion,
        EventAuthority,
        EvidenceSnapshot,
        Operation,
        canonical,
        fingerprint,
    )
    from goalanchor.workflow_store import WorkflowStore

    params = request["params"]
    command_approval(params)
    user = EventAuthority(secrets.token_bytes(32), "explicit-test-user-fixture")
    observer = EventAuthority(secrets.token_bytes(32), "host-controller")
    runtime = Workflow(WorkflowStore(Path(directory).parent / "controller.db"), user, observer,
                       WorkflowPolicy(directory, ("codex.command",), host_binding=host_binding))
    base = Baseline("host-smoke", 1, "Print a fixed marker", "Approve exact callback once", (
        Criterion("exit", "Observed command exit code is zero", canonical(0), "host-command"),
    ), required_operations=("host-command",))
    runtime.start_baseline(base, user.sign("user.baseline", base.fingerprint, base.to_dict()))
    snapshot = EvidenceSnapshot.capture("callback", "host-request", canonical(request).encode(),
                                        source="codex-app-server")
    runtime.add_evidence(base.goal_id, snapshot, observer.sign("observer.evidence", snapshot.fingerprint,
                         {"goal_id": base.goal_id, "snapshot": snapshot.to_dict()}))
    ctx = runtime.review_context(base.goal_id)
    names = ("A", "B")
    bundle = {"context": ctx.to_dict(), "risk": "HIGH", "round_number": 1, "max_rounds": 2,
              "reviewer_results": [ReviewerResult(n, 1, ReviewVerdict.PASS, (), True, ctx.fingerprint).to_dict()
                                   for n in names],
              "arbiter": ArbiterResult("arbiter", 1, ReviewVerdict.PASS, "fixture", "exact print", names,
                                       ctx.fingerprint, False).to_dict(),
              "meta_review": MetaReviewResult("meta", 1, "arbiter", ReviewVerdict.PASS, "fixture", (),
                                              ctx.fingerprint, True).to_dict(), "dispositions": {},
              "bindings": [{"agent_id": n, "session_id": "fixture-" + n, "model": "synthetic-fixture"}
                           for n in (*names, "arbiter", "meta")]}
    from goalanchor.review_delivery import build_packets
    packets = build_packets(ctx, bundle, [])
    bundle["delivery_receipts"] = [
        {"agent_id": binding["agent_id"], "session_id": binding["session_id"],
         "input_digest": fingerprint(packets[binding["agent_id"]]),
         "transport_receipt_id": "synthetic-" + binding["session_id"], "status": "DELIVERED"}
        for binding in bundle["bindings"]]
    runtime.submit_review(base.goal_id, bundle, observer.sign("observer.review", ctx.fingerprint, bundle))
    operation = Operation("host-command", base.goal_id, base.fingerprint, "codex.command", canonical(params),
                          directory, "host-item")
    runtime.register_operation(operation)
    question = runtime.request_authorization(operation.operation_id).question
    runtime.answer(question.question_id, user.sign("user.answer", question.fingerprint,
                   {"answer_id": "fixture-answer", "selected": ["approve"], "custom": None}))
    controller = CodexController(client, params["threadId"], params["turnId"], host_binding=host_binding)
    controller.command(request)
    receipt = controller.dispatch(request["id"], runtime, operation.operation_id, "host-attempt",
                                  timeout=0.01)
    result: dict[str, Any] = {"unknown_receipt_state": receipt["state"],
                              "approval_outcome": controller.approval_outcomes.get(request["id"], "NOT_ATTEMPTED")}
    if receipt["state"] != "UNKNOWN":
        return result
    completion = _await_completion(controller, request["id"], timeout=60)
    if completion is None:
        result["completion_after_unknown"] = "TIMEOUT"
        return result
    exit_code = completion
    source_request_id = operation.arguments.get("request_id", operation.operation_id) \
        if isinstance(operation.arguments, dict) else operation.operation_id
    evidence = EvidenceSnapshot.capture("reconcile-proof", source_request_id,
                                        canonical({"itemId": params["itemId"], "exit_code": exit_code}).encode(),
                                        source="codex-app-server")
    runtime.add_evidence(base.goal_id, evidence,
                         observer.sign("observer.evidence", evidence.fingerprint,
                                       {"goal_id": base.goal_id, "snapshot": evidence.to_dict()}))
    reconciled = runtime.reconcile("host-attempt", observer.sign("observer.reconcile", "host-attempt", {
        "attempt_id": "host-attempt",
        "operation_digest": operation.fingerprint,
        "state": "SUCCEEDED" if exit_code == 0 else "FAILED",
        "result": {"exit_code": exit_code, "object_ref": operation.operation_id,
                   "source_request_id": str(request["id"]), "item_id": params["itemId"]},
        "evidence_id": evidence.evidence_id,
        "source_request_id": source_request_id,
    }))
    result["reconciled_state"] = reconciled["state"]
    if reconciled["state"] == "SUCCEEDED":
        runtime.observe(base.goal_id, observer.sign("observer.observation", base.fingerprint, {
            "goal_id": base.goal_id, "baseline_digest": base.fingerprint, "criterion_id": "exit",
            "object_ref": "host-command", "actual": exit_code, "attempt_id": "host-attempt"}))
        try:
            result["closure"] = runtime.close(base.goal_id)["state"]
        except Exception as exc:  # noqa: BLE001 - records the evidence-freeze boundary honestly
            result["closure"] = "BLOCKED:" + type(exc).__name__
            result["closure_boundary"] = ("reconcile evidence is collected after review; close re-verifies the "
                                          "live review context, so reconciled goals cannot close under the "
                                          "current evidence-freeze semantics")
    return result


def _await_completion(controller: Any, request_id: Any, *, timeout: float) -> int | None:
    """Drain events until the approved command's completion carries an exit code."""
    from goalanchor.codex_host import CodexTransportError

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = controller.client.next_event(timeout=max(.001, deadline - time.monotonic()))
        except CodexTransportError:
            return None
        method = event.get("method", "")
        if "id" in event:
            controller.client.respond(event["id"], {"decision": "decline"})
        if method == "item/completed":
            item = event.get("params", {}).get("item", {})
            if item.get("type") == "commandExecution":
                exit_code = item.get("exit_code", item.get("exitCode"))
                if isinstance(exit_code, int):
                    return exit_code
        if method == "turn/completed":
            return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario",
                        choices=("smoke", "question", "cancel", "decline", "accept", "reconcile", "real-review"),
                        default="smoke")
    parser.add_argument("--model", default=None,
                        help="override the Codex config default model for this subprocess only")
    args = parser.parse_args()
    import shutil
    argv = None
    if args.model:
        executable = shutil.which("codex")
        if not executable:
            raise SystemExit("codex executable not found")
        argv = [executable, "-c", "model=" + args.model, "app-server"]
    record: dict[str, object] = {"level": "real-host-model-smoke", "scenario": args.scenario,
                                 "approval_provider": "fixture" if args.scenario == "question" else "none",
                                 "native_ui_verified": False, "approved_requests": 0}
    with tempfile.TemporaryDirectory(prefix="goalanchor-codex-smoke-") as root:
        directory = str(Path(root) / "workspace")
        Path(directory).mkdir()
        with CodexClient(argv=argv, cwd=directory, timeout=30) as client:
            initialized = client.initialize()
            record["platform"] = initialized.get("platformOs")
            thread = client.request("thread/start", {"cwd": directory, "ephemeral": True,
                                                     "approvalPolicy": "untrusted", "sandbox": "read-only"})
            thread_id = thread["thread"]["id"]
            record["model"] = thread.get("model")
            host_binding = json.dumps({"host": "codex", "user_agent": initialized["userAgent"],
                                       "model": thread["model"],
                                       "mode": "app-server-plan" if args.scenario == "question"
                                       else "app-server-command-approval",
                                       "hook_enabled": True, "hook_trust": "controller-owned"})
            record["host_binding"] = json.loads(host_binding)
            prompt = "Do not call tools. Reply exactly GOALANCHOR_HOST_OK."
            if args.scenario == "question":
                prompt = ("Use request_user_input now to ask whether the artifact should be Alpha or Beta. "
                          "Do not choose yourself. After my answer, repeat it and stop. Do not run commands.")
            elif args.scenario in ("decline", "accept", "reconcile", "real-review"):
                prompt = ("Test an approval callback only. Request an escalated shell command with "
                          "sandbox_permissions=require_escalated and command exactly "
                          "Write-Output GOALANCHOR_APPROVAL_TEST. "
                          "Omit prefix_rule. Do not run anything else or modify files. If denied, stop without retry.")
            params = {"threadId": thread_id, "input": [
                {"type": "text", "text": prompt, "text_elements": []}]}
            if args.scenario == "question":
                params["collaborationMode"] = {"mode": "plan", "settings": {
                    "model": thread["model"], "reasoning_effort": None, "developer_instructions": None}}
            started = client.request("turn/start", params)
            record["turn_id"] = started["turn"]["id"]
            if args.scenario == "cancel":
                client.request("turn/interrupt", {"threadId": thread_id, "turnId": record["turn_id"]})
                record["interrupt_sent"] = True
            methods: list[str] = []
            diagnostics: list[dict[str, Any]] = []
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                event = client.next_event(timeout=min(30, max(.1, deadline - time.monotonic())))
                method = event.get("method", "")
                methods.append(method)
                if method in ("error", "warning") or method.startswith("item/"):
                    diagnostics.append({"method": method, "params": event.get("params", {})})
                if "id" in event:
                    if args.scenario in ("accept", "real-review") and method == "item/commandExecution/requestApproval":
                        record.update(approve_fixture(client, event, directory, host_binding,
                                                       review_model=args.model
                                                       if args.scenario == "real-review" else None))
                        record["approval_provider"] = "explicit-test-fixture"
                        record["approved_requests"] = int(record["approved_requests"]) + int(
                            record.get("approval_outcome") == "SENT")
                        record["unknown_approval_requests"] = int(record.get("unknown_approval_requests", 0)) + int(
                            record.get("approval_outcome") == "UNKNOWN")
                    elif args.scenario == "reconcile" and method == "item/commandExecution/requestApproval":
                        record.update(reconcile_fixture(client, event, directory, host_binding))
                        record["approval_provider"] = "explicit-test-fixture"
                        record["approved_requests"] = int(record["approved_requests"]) + int(
                            record.get("approval_outcome") == "SENT")
                    elif args.scenario == "question" and method == "item/tool/requestUserInput":
                        record.update(answer_fixture(client, event, directory))
                    else:
                        client.respond(event["id"], {"decision": "decline"})
                        record["declined_request"] = method
                if method == "turn/completed":
                    record["turn_status"] = event["params"]["turn"]["status"]
                    break
            else:
                client.request("turn/interrupt", {"threadId": thread_id, "turnId": record["turn_id"]})
                record["turn_status"] = "TIMEOUT"
            record["methods"] = methods
            record["diagnostics"] = diagnostics
    args.output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
