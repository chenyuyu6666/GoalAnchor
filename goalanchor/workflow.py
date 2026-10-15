"""Strict, host-controlled workflow over versioned SQLite records.

The host owns authorities, policy and executors. None is supplied by model JSON.
This API is an embedding boundary, not a sandbox against its own administrator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .core import ContractValidationError
from .host_contract import validate_host_binding
from .questions import Question, QuestionAnswer, QuestionOption, QuestionRecord
from .review_delivery import build_packets, validate_receipts
from .review_service import (
    ArbiterResult,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    evaluate_review_cycle,
)
from .workflow_protocol import (
    Baseline,
    EventAuthority,
    EvidenceSnapshot,
    Operation,
    SignedEvent,
    canonical,
    fingerprint,
    parse_time,
    shape,
    strict_loads,
    strings,
    utc_now,
)
from .workflow_store import WorkflowStore

Payload = dict[str, Any]


@dataclass(frozen=True)
class WorkflowPolicy:
    workspace: str
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...] = ()
    grant_ttl_seconds: int = 3600
    acceptable_risks: tuple[str, ...] = ()
    host_binding: str | None = None

    def __post_init__(self) -> None:
        if self.host_binding is not None:
            object.__setattr__(self, "host_binding", validate_host_binding(self.host_binding))
        object.__setattr__(self, "workspace", str(Path(self.workspace).resolve()))
        for name in ("allowed_tools", "denied_tools", "acceptable_risks"):
            object.__setattr__(self, name, strings(getattr(self, name), name))
        if isinstance(self.grant_ttl_seconds, bool) or not isinstance(self.grant_ttl_seconds, int):
            raise ContractValidationError("grant TTL must be an integer")
        if not 1 <= self.grant_ttl_seconds <= 86400:
            raise ContractValidationError("grant TTL must be between 1 and 86400 seconds")

    @property
    def fingerprint(self) -> str:
        return fingerprint(dict(self.__dict__))


class Workflow:
    def __init__(
        self,
        store: WorkflowStore,
        user_authority: EventAuthority,
        observer_authority: EventAuthority,
        policy: WorkflowPolicy,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store
        self.user_authority = user_authority
        self.observer_authority = observer_authority
        self.policy = policy
        self.clock = clock

    def _get(self, kind: str, key: str) -> Payload:
        value = self.store.get(kind, key)
        if value is None:
            raise ContractValidationError(f"missing {kind} record: {key}")
        return value

    def _goal(self, goal_id: str, *, active: bool = True) -> Payload:
        goal = self._get("goal", goal_id)
        if goal["policy_digest"] != self.policy.fingerprint:
            raise ContractValidationError("workflow policy changed; explicit revalidation required")
        if active and goal["state"] in ("CLOSED", "STOPPED"):
            raise ContractValidationError("terminal workflow cannot execute")
        return goal

    def _verify(self, authority: EventAuthority, event: SignedEvent, kind: str, subject: str) -> Payload:
        payload = authority.verify(event, kind=kind, subject=subject)
        if parse_time(event.created_at) > parse_time(self.clock()) + timedelta(seconds=5):
            raise ContractValidationError("event is from the future")
        return payload

    def _seen(self, event: SignedEvent) -> bool:
        old = self.store.get_event(event.event_id)
        if old is not None and canonical(old) != canonical(event.to_dict()):
            raise ContractValidationError("event identity conflict")
        return old is not None

    def _commit(
        self,
        changes: list[tuple[str, str, Payload]],
        before: Mapping[tuple[str, str], Payload | None],
        event: SignedEvent,
    ) -> bool:
        return self.store.commit(
            changes,
            {key: None if value is None else self.store.fingerprint(value) for key, value in before.items()},
            event.event_id,
            event.to_dict(),
            **({"valid_until": event.payload["valid_until"], "clock": lambda: self.clock()}
               if event.kind == "controller.close" else {}),
        )

    def start_baseline(self, baseline: Baseline, event: SignedEvent) -> Payload:
        payload = self._verify(self.user_authority, event, "user.baseline", baseline.fingerprint)
        if canonical(payload) != canonical(baseline.to_dict()):
            raise ContractValidationError("baseline event payload mismatch")
        old = self.store.get("goal", baseline.goal_id)
        if self._seen(event):
            return self._get("goal", baseline.goal_id)
        if old is not None and baseline.revision <= old["revision"]:
            raise ContractValidationError("baseline revision must advance")
        goal = {
            "goal_id": baseline.goal_id,
            "revision": baseline.revision,
            "baseline": baseline.to_dict(),
            "baseline_digest": baseline.fingerprint,
            "policy_digest": self.policy.fingerprint,
            "state": "PLAN",
            "baseline_created_at": event.created_at,
            "baseline_event_id": event.event_id,
            "supersedes": None if old is None else old["baseline_digest"],
        }
        self._commit(
            [("baseline", baseline.fingerprint, baseline.to_dict()), ("goal", baseline.goal_id, goal)],
            {("baseline", baseline.fingerprint): None, ("goal", baseline.goal_id): old},
            event,
        )
        return goal

    def add_evidence(self, goal_id: str, snapshot: EvidenceSnapshot, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        payload = self._verify(self.observer_authority, event, "observer.evidence", snapshot.fingerprint)
        if canonical(payload) != canonical({"goal_id": goal_id, "snapshot": snapshot.to_dict()}):
            raise ContractValidationError("evidence event payload mismatch")
        if not snapshot.valid_at(self.clock()):
            raise ContractValidationError("evidence expired or not yet applicable")
        key = goal_id + ":" + snapshot.evidence_id
        record = {"goal_id": goal_id, "snapshot": snapshot.to_dict(), "source_event_id": event.event_id}
        if self._seen(event):
            return self._get("evidence", key)
        old = self.store.get("evidence", key)
        if old is not None:
            if old["snapshot"] != record["snapshot"] or old["goal_id"] != goal_id:
                raise ContractValidationError("evidence identity conflict")
            record = old
        self._commit([("evidence", key, record)], {("evidence", key): old, ("goal", goal_id): goal}, event)
        return record

    def review_context(self, goal_id: str) -> ReviewContext:
        goal = self._goal(goal_id)
        baseline = Baseline.from_dict(goal["baseline"])
        snapshots = [EvidenceSnapshot.from_dict(item["snapshot"]) for item in self.store.list("evidence", goal_id)]
        if not snapshots or any(not item.valid_at(self.clock()) for item in snapshots):
            raise ContractValidationError("review requires current verified evidence")
        snapshots.sort(key=lambda item: item.evidence_id)
        return ReviewContext(
            goal=baseline.objective,
            prompt=baseline.plan,
            constraints=baseline.constraints,
            evidence_refs=tuple(item.evidence_id for item in snapshots),
            candidate=canonical(
                {
                    "baseline": baseline.to_dict(),
                    "baseline_digest": baseline.fingerprint,
                    "snapshots": [item.to_dict() for item in snapshots],
                }
            ),
            evidence_digests=tuple((item.evidence_id, item.sha256) for item in snapshots),
        )

    def review_packets(self, goal_id: str, bundle: Payload) -> dict[str, Payload]:
        context = self.review_context(goal_id)
        previous = self.store.get("review", goal_id)
        prior = previous["history"] if previous and previous["context_fingerprint"] == context.fingerprint else []
        return build_packets(context, bundle, prior)

    def submit_review(self, goal_id: str, bundle: Payload, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        ctx = self.review_context(goal_id)
        payload = self._verify(self.observer_authority, event, "observer.review", ctx.fingerprint)
        if canonical(payload) != canonical(bundle):
            raise ContractValidationError("review event payload mismatch")
        expected_keys = {
            "context",
            "risk",
            "round_number",
            "max_rounds",
            "reviewer_results",
            "arbiter",
            "meta_review",
            "dispositions",
            "bindings",
            "delivery_receipts",
        }
        shape(bundle, expected_keys)
        if ReviewContext.from_dict(bundle["context"]) != ctx:
            raise ContractValidationError("review context is stale")
        previous = self.store.get("review", goal_id)
        if self._seen(event):
            return self._get("review", goal_id)
        validate_receipts(bundle, self.review_packets(goal_id, bundle))
        same_context = previous is not None and previous["context_fingerprint"] == ctx.fingerprint
        prior_round = previous["round_number"] if same_context and previous is not None else 0
        if bundle["round_number"] != prior_round + 1 or bundle["round_number"] > 2:
            raise ContractValidationError("invalid or exhausted review round")
        if previous is not None and previous["status"] == "REVIEW_REQUIRED" and not same_context:
            raise ContractValidationError("changed disputed context requires explicit new baseline")
        reviewers = tuple(ReviewerResult.from_dict(item) for item in bundle["reviewer_results"])
        arbiter = ArbiterResult.from_dict(bundle["arbiter"])
        meta = MetaReviewResult.from_dict(bundle["meta_review"])
        bindings = bundle["bindings"]
        ids = {item.reviewer_id for item in reviewers} | {arbiter.arbiter_id, meta.reviewer_id}
        if len(ids) != len(reviewers) + 2 or not isinstance(bindings, list):
            raise ContractValidationError("independent roles required")
        if {item.get("agent_id") for item in bindings} != ids or len(bindings) != len(ids):
            raise ContractValidationError("bindings must cover all independent roles")
        sessions = [item.get("session_id") for item in bindings]
        models = [item.get("model") for item in bindings]
        if any(not isinstance(item, str) or not item for item in sessions + models):
            raise ContractValidationError("unknown review identity or model")
        if len(set(sessions)) != len(sessions) or len(set(models)) != 1:
            raise ContractValidationError("review isolation/model mismatch")
        history = list(previous["history"]) if same_context and previous is not None else []
        if history and set(sessions) & {item["session_id"] for old in history for item in old["bindings"]}:
            raise ContractValidationError("review round must use fresh independent sessions")
        result = evaluate_review_cycle(
            ctx,
            bundle["risk"],
            reviewers,
            arbiter,
            meta,
            round_number=bundle["round_number"],
            max_rounds=bundle["max_rounds"],
        )
        history.append(bundle)
        findings: dict[str, Payload] = {}
        for old in history:
            for report in old["reviewer_results"]:
                for item in report["findings"]:
                    key = item["finding_id"]
                    if key in findings and findings[key] != item:
                        raise ContractValidationError("finding identity conflict")
                    findings[key] = item
        dispositions = bundle["dispositions"]
        if not isinstance(dispositions, dict) or set(dispositions) - set(findings):
            raise ContractValidationError("unknown finding disposition")
        evidence_ids = set(ctx.evidence_refs)
        unresolved = []
        for key in findings:
            disposition = dispositions.get(key, {})
            refs = disposition.get("evidence_refs", [])
            status = disposition.get("status")
            if status in ("FIXED", "REFUTED") and refs and set(refs) <= evidence_ids:
                if status == "REFUTED" and bundle["round_number"] < 2:
                    unresolved.append(key)
            elif status == "RISK_ACCEPTED" and key in self.policy.acceptable_risks:
                if not self._risk_valid(goal_id, key, goal["baseline_digest"]):
                    unresolved.append(key)
            else:
                unresolved.append(key)
        if result.outcome.value == "PASS" and not unresolved:
            status = "PASSED"
        elif bundle["round_number"] >= bundle["max_rounds"] or result.outcome.value == "STOPPED":
            status = "STOPPED"
        else:
            status = "REVIEW_REQUIRED"
        record = {
            "goal_id": goal_id,
            "baseline_digest": goal["baseline_digest"],
            "context_fingerprint": ctx.fingerprint,
            "round_number": bundle["round_number"],
            "status": status,
            "history": history,
            "unresolved": unresolved,
            "result": result.to_dict(),
            "source_event_id": event.event_id,
        }
        updated_goal = {**goal, "state": "STOPPED" if status == "STOPPED" else "VERIFY"}
        self._commit(
            [("review", goal_id, record), ("goal", goal_id, updated_goal)],
            {("review", goal_id): previous, ("goal", goal_id): goal},
            event,
        )
        return record

    def _review(self, goal_id: str) -> Payload:
        review = self._get("review", goal_id)
        goal = self._goal(goal_id)
        if review["status"] != "PASSED" or review["baseline_digest"] != goal["baseline_digest"]:
            raise ContractValidationError("valid review required")
        if self.review_context(goal_id).fingerprint != review["context_fingerprint"]:
            raise ContractValidationError("evidence changed since review")
        source = SignedEvent.from_dict(self._get_event(review["source_event_id"]))
        payload = self._verify(self.observer_authority, source, "observer.review", review["context_fingerprint"])
        if canonical(payload) != canonical(review["history"][-1]):
            raise ContractValidationError("review source mismatch")
        for finding_id, disposition in payload["dispositions"].items():
            if disposition["status"] == "RISK_ACCEPTED" and not self._risk_valid(
                goal_id, finding_id, goal["baseline_digest"]
            ):
                raise ContractValidationError("accepted risk expired or no longer valid")
        return review

    def _risk_valid(self, goal_id: str, finding_id: str, baseline_digest: str) -> bool:
        risk = self.store.get("risk", goal_id + ":" + finding_id)
        if (
            risk is None
            or finding_id not in self.policy.acceptable_risks
            or risk["baseline_digest"] != baseline_digest
            or parse_time(risk["expires_at"]) <= parse_time(self.clock())
        ):
            return False
        source = SignedEvent.from_dict(self._get_event(risk["source_event_id"]))
        payload = self._verify(self.user_authority, source, "user.risk", baseline_digest)
        return payload == {key: value for key, value in risk.items() if key != "source_event_id"}

    def _grant_deadline(self, goal_id: str, review: Payload, requested: str) -> str:
        """Freeze all time-based dependencies into the transactional grant expiry."""
        deadlines = [parse_time(requested)]
        bundle = review["history"][-1]
        for evidence_id in bundle["context"]["evidence_refs"]:
            snapshot = self._get("evidence", goal_id + ":" + evidence_id)["snapshot"]
            if snapshot.get("expires_at") is not None:
                deadlines.append(parse_time(snapshot["expires_at"]))
        for finding_id, disposition in bundle["dispositions"].items():
            if disposition["status"] == "RISK_ACCEPTED":
                deadlines.append(parse_time(self._get("risk", goal_id + ":" + finding_id)["expires_at"]))
        deadline = min(deadlines)
        if deadline <= parse_time(self.clock()):
            raise ContractValidationError("authorization dependency expired")
        return deadline.isoformat()

    def _check_operation(self, operation: Operation) -> Payload:
        goal = self._goal(operation.goal_id)
        if operation.baseline_digest != goal["baseline_digest"]:
            raise ContractValidationError("operation baseline is stale")
        if operation.tool not in self.policy.allowed_tools or operation.tool in self.policy.denied_tools:
            raise ContractValidationError("operation forbidden by hard policy")
        workspace = Path(self.policy.workspace)
        cwd = Path(operation.cwd).resolve()
        if not cwd.is_relative_to(workspace):
            raise ContractValidationError("operation cwd outside policy workspace")
        for raw, expected in operation.resource_preconditions:
            path = (cwd / raw).resolve()
            if not path.is_relative_to(workspace):
                raise ContractValidationError("resource outside policy workspace")
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if actual != expected:
                raise ContractValidationError("resource precondition changed")
        return goal

    def register_operation(self, operation: Operation) -> Payload:
        goal = self._check_operation(operation)
        old = self.store.get("operation", operation.operation_id)
        if old is not None:
            if old != operation.to_dict():
                raise ContractValidationError("operation identity conflict")
            return old
        event = self.observer_authority.sign(
            "controller.operation", operation.fingerprint, operation.to_dict(), at=self.clock()
        )
        self._commit(
            [("operation", operation.operation_id, operation.to_dict())],
            {("operation", operation.operation_id): None, ("goal", operation.goal_id): goal},
            event,
        )
        return operation.to_dict()

    def request_question(self, question: Question) -> QuestionRecord:
        goal = self._goal(question.goal_id)
        if question.plan_revision != goal["revision"] or question.baseline_digest != goal["baseline_digest"]:
            raise ContractValidationError("question baseline mismatch")
        old = self.store.get("question", question.question_id)
        if old is not None:
            record = QuestionRecord.from_dict(old["record"])
            if record.question != question:
                raise ContractValidationError("question identity conflict")
            return record
        for item in self.store.list("question", question.goal_id):
            record = QuestionRecord.from_dict(item["record"])
            if record.question.plan_revision != goal["revision"]:
                continue
            if record.question.dedupe_key == question.dedupe_key or (
                question.kind == "AUTHORIZATION" and record.question.operation_digest == question.operation_digest
            ):
                return record
        record = QuestionRecord(question).wait()
        payload = {"goal_id": question.goal_id, "baseline_digest": goal["baseline_digest"], "record": record.to_dict()}
        event = self.observer_authority.sign("controller.question", question.fingerprint, payload, at=self.clock())
        self._commit(
            [("question", question.question_id, payload)],
            {("question", question.question_id): None, ("goal", question.goal_id): goal},
            event,
        )
        return record

    def update_presentation(self, goal_id: str, proof: Payload, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        payload = self._verify(self.observer_authority, event, "observer.presentation", goal["baseline_digest"])
        shape(payload, {"goal_id", "baseline_digest", "context_fingerprint", "presentation", "no_impact_reason"})
        shape(payload["presentation"], {"title"})
        title = payload["presentation"]["title"]
        reason = payload["no_impact_reason"]
        if (canonical(payload) != canonical(proof) or payload["goal_id"] != goal_id
                or payload["baseline_digest"] != goal["baseline_digest"]
                or payload["context_fingerprint"] != self.review_context(goal_id).fingerprint
                or not isinstance(title, str) or not 0 < len(title.strip()) <= 200
                or not isinstance(reason, str) or not reason.strip()):
            raise ContractValidationError("Presentation proof does not establish unchanged direct dependencies")
        if self._seen(event):
            return self._get("presentation", goal_id)
        old = self.store.get("presentation", goal_id)
        record = {**payload, "source_event_id": event.event_id}
        self._commit([("presentation", goal_id, record)],
                     {("presentation", goal_id): old, ("goal", goal_id): goal}, event)
        return record

    def request_authorization(self, operation_id: str) -> QuestionRecord:
        operation = Operation.from_dict(self._get("operation", operation_id))
        goal = self._check_operation(operation)
        self._review(operation.goal_id)
        return self.request_question(
            Question(
                "auth-" + operation.fingerprint,
                operation.goal_id,
                goal["revision"],
                "Approve exact operation",
                canonical(operation.to_dict()),
                (QuestionOption("approve", "Allow once"), QuestionOption("deny", "Reject")),
                allow_custom=True,
                blocking_items=(operation.work_item_id,),
                kind="AUTHORIZATION",
                operation_digest=operation.fingerprint,
                approval_option_id="approve",
                baseline_digest=operation.baseline_digest,
            )
        )

    def answer(self, question_id: str, event: SignedEvent) -> QuestionRecord:
        old = self._get("question", question_id)
        record = QuestionRecord.from_dict(old["record"])
        question = record.question
        payload = self._verify(self.user_authority, event, "user.answer", question.fingerprint)
        shape(payload, {"answer_id", "selected", "custom"})
        goal = self._goal(question.goal_id)
        if goal["baseline_digest"] != question.baseline_digest or goal["revision"] != question.plan_revision:
            raise ContractValidationError("late answer belongs to an old baseline")
        if self._seen(event):
            return record
        answer = QuestionAnswer(
            payload["answer_id"],
            question.question_id,
            question.fingerprint,
            selected=payload["selected"],
            custom=payload["custom"],
            source_event_id=event.event_id,
        )
        updated = record.receive(answer)
        changes = [("question", question_id, {**old, "record": updated.to_dict()})]
        before: dict[tuple[str, str], Payload | None] = {
            ("question", question_id): old,
            ("goal", question.goal_id): goal,
        }
        if updated.decision == "APPROVE":
            review = self._review(question.goal_id)
            operation = next(
                (
                    item
                    for item in self.store.list("operation", question.goal_id)
                    if Operation.from_dict(item).fingerprint == question.operation_digest
                ),
                None,
            )
            if operation is None:
                raise ContractValidationError("authorization has no exact operation")
            self._check_operation(Operation.from_dict(operation))
            grant_id = "grant-" + event.event_id
            grant = {
                "authorization_id": grant_id,
                "goal_id": question.goal_id,
                "plan_revision": question.plan_revision,
                "baseline_digest": question.baseline_digest,
                "operation_digest": question.operation_digest,
                "state": "ACTIVE",
                "kind": "ONE_TIME",
                "expires_at": self._grant_deadline(
                    question.goal_id,
                    review,
                    (parse_time(event.created_at) + timedelta(seconds=self.policy.grant_ttl_seconds)).isoformat(),
                ),
                "source_event_id": event.event_id,
                "source_question_id": question_id,
                "review_digest": self.store.fingerprint(review),
            }
            changes.append(("grant", grant_id, grant))
            before[("grant", grant_id)] = None
            before[("review", question.goal_id)] = review
        self._commit(changes, before, event)
        return updated

    def work_item_decision(self, goal_id: str, work_item_id: str) -> str:
        goal = self._goal(goal_id)
        blocked = False
        for item in self.store.list("question", goal_id):
            record = QuestionRecord.from_dict(item["record"])
            q = record.question
            if q.baseline_digest != goal["baseline_digest"] or work_item_id not in q.blocking_items:
                continue
            if record.decision == "DENY":
                return "DENY"
            blocked |= record.state != "ANSWERED" or record.decision == "ANSWERED" and q.kind == "AUTHORIZATION"
        return "WAIT" if blocked else "ALLOW"

    def transition_question(self, question_id: str, state: str, event: SignedEvent) -> QuestionRecord:
        old = self._get("question", question_id)
        record = QuestionRecord.from_dict(old["record"])
        goal = self._goal(record.question.goal_id)
        payload = self._verify(self.user_authority, event, "user.question", record.question.fingerprint)
        if payload != {"question_id": question_id, "state": state}:
            raise ContractValidationError("question transition binding mismatch")
        if self._seen(event):
            return record
        transitions = {"CANCELLED": record.cancel, "EXPIRED": record.expire, "SUPERSEDED": record.supersede}
        if state not in transitions or record.question.baseline_digest != goal["baseline_digest"]:
            raise ContractValidationError("invalid question transition")
        updated = transitions[state]()
        self._commit(
            [("question", question_id, {**old, "record": updated.to_dict()})],
            {("question", question_id): old, ("goal", record.question.goal_id): goal},
            event,
        )
        return updated

    def authorize_scope(self, goal_id: str, scope_id: str, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        review = self._review(goal_id)
        payload = self._verify(self.user_authority, event, "user.scope", goal["baseline_digest"])
        shape(payload, {"goal_id", "baseline_digest", "scope_id", "allowed_tools", "workspace", "expires_at"})
        tools = strings(payload["allowed_tools"], "allowed_tools")
        workspace = str(Path(payload["workspace"]).resolve())
        if (
            payload["goal_id"] != goal_id
            or payload["baseline_digest"] != goal["baseline_digest"]
            or payload["scope_id"] != scope_id
            or not scope_id
            or not tools
            or not set(tools) <= set(self.policy.allowed_tools)
            or set(tools) & set(self.policy.denied_tools)
            or not Path(workspace).is_relative_to(Path(self.policy.workspace))
            or parse_time(payload["expires_at"]) <= parse_time(self.clock())
            or parse_time(payload["expires_at"])
            > parse_time(event.created_at) + timedelta(seconds=self.policy.grant_ttl_seconds)
        ):
            raise ContractValidationError("scope exceeds current policy, baseline or expiry")
        if self._seen(event):
            return self._get("grant", scope_id)
        grant = {
            **payload,
            "authorization_id": scope_id,
            "kind": "AUTONOMY_GRANT",
            "state": "ACTIVE",
            "workspace": workspace,
            "allowed_tools": list(tools),
            "plan_revision": goal["revision"],
            "source_event_id": event.event_id,
            "review_digest": self.store.fingerprint(review),
            "expires_at": self._grant_deadline(goal_id, review, payload["expires_at"]),
        }
        self._commit(
            [("grant", scope_id, grant)],
            {("grant", scope_id): None, ("goal", goal_id): goal, ("review", goal_id): review},
            event,
        )
        return grant

    def _validate_scope(self, scope: Payload, operation: Operation, goal: Payload, review: Payload) -> None:
        source = SignedEvent.from_dict(self._get_event(scope["source_event_id"]))
        payload = self._verify(self.user_authority, source, "user.scope", goal["baseline_digest"])
        if (
            scope["kind"] != "AUTONOMY_GRANT"
            or scope["state"] != "ACTIVE"
            or scope.get("revoked_at")
            or scope["baseline_digest"] != goal["baseline_digest"]
            or scope["goal_id"] != operation.goal_id
            or scope["review_digest"] != self.store.fingerprint(review)
            or operation.tool not in payload["allowed_tools"]
            or not Path(operation.cwd).resolve().is_relative_to(Path(payload["workspace"]).resolve())
            or parse_time(scope["expires_at"]) <= parse_time(self.clock())
            or parse_time(scope["expires_at"]) > parse_time(payload["expires_at"])
            or any(scope[key] != payload[key] for key in payload if key not in ("workspace", "expires_at"))
            or Path(scope["workspace"]).resolve() != Path(payload["workspace"]).resolve()
        ):
            raise ContractValidationError("scope does not authorize operation")

    def _scope_grant(
        self, operation: Operation, goal: Payload, review: Payload
    ) -> tuple[Payload | None, Payload | None]:
        for parent in self.store.list("grant", operation.goal_id):
            if parent.get("kind") != "AUTONOMY_GRANT":
                continue
            try:
                self._validate_scope(parent, operation, goal, review)
            except ContractValidationError:
                continue
            key = parent["authorization_id"] + ":" + operation.fingerprint
            old = self.store.get("grant", key)
            if old is not None:
                if old["state"] == "ACTIVE":
                    return old, parent
                continue
            grant = {
                "authorization_id": key,
                "goal_id": operation.goal_id,
                "plan_revision": goal["revision"],
                "baseline_digest": goal["baseline_digest"],
                "operation_digest": operation.fingerprint,
                "state": "ACTIVE",
                "kind": "ONE_TIME",
                "expires_at": parent["expires_at"],
                "source_event_id": parent["source_event_id"],
                "parent_id": parent["authorization_id"],
                "review_digest": parent["review_digest"],
            }
            event = self.observer_authority.sign("controller.derive", operation.fingerprint, grant, at=self.clock())
            self._commit(
                [("grant", key, grant)],
                {
                    ("grant", key): None,
                    ("grant", parent["authorization_id"]): parent,
                    ("goal", operation.goal_id): goal,
                    ("review", operation.goal_id): review,
                },
                event,
            )
            return grant, parent
        return None, None

    def revoke_authorization(self, grant_id: str, event: SignedEvent) -> Payload:
        payload = self._verify(self.user_authority, event, "user.revoke", grant_id)
        if payload != {"grant_id": grant_id}:
            raise ContractValidationError("revocation binding mismatch")
        old = self._get("grant", grant_id)
        if self._seen(event):
            return old
        grant = {**old, "revoked_at": event.created_at}
        if old["state"] in ("ACTIVE", "RESERVED"):
            grant["state"] = "REVOKED"
        self._commit([("grant", grant_id, grant)], {("grant", grant_id): old}, event)
        return grant

    def cancel(self, goal_id: str, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id, active=False)
        payload = self._verify(self.user_authority, event, "user.stop", goal["baseline_digest"])
        shape(payload, {"goal_id", "reason"})
        if payload["goal_id"] != goal_id or not isinstance(payload["reason"], str) or not payload["reason"].strip():
            raise ContractValidationError("invalid cancellation")
        if self._seen(event):
            return goal
        if goal["state"] == "CLOSED":
            raise ContractValidationError("closed baseline cannot be cancelled")
        stopped = {**goal, "state": "STOPPED", "stop_event_id": event.event_id}
        self._commit([("goal", goal_id, stopped)], {("goal", goal_id): goal}, event)
        return stopped

    def reconcile(self, attempt_id: str, event: SignedEvent) -> Payload:
        payload = self._verify(self.observer_authority, event, "observer.reconcile", attempt_id)
        shape(payload, {"attempt_id", "operation_digest", "state", "result", "evidence_id", "source_request_id"})
        receipt = self._get("receipt", attempt_id)
        if self._seen(event):
            return receipt
        operation = next(
            (
                Operation.from_dict(item)
                for item in self.store.list("operation", receipt["goal_id"])
                if Operation.from_dict(item).fingerprint == receipt["operation_digest"]
            ),
            None,
        )
        evidence = EvidenceSnapshot.from_dict(
            self._get("evidence", receipt["goal_id"] + ":" + payload["evidence_id"])["snapshot"]
        )
        result = payload["result"]
        if (
            receipt["state"] != "UNKNOWN"
            or operation is None
            or payload["attempt_id"] != attempt_id
            or payload["operation_digest"] != receipt["operation_digest"]
            or payload["source_request_id"] != operation.arguments.get("request_id", operation.operation_id)
            or evidence.object_ref != payload["source_request_id"]
            or not evidence.valid_at(self.clock())
            or parse_time(evidence.collected_at) < parse_time(receipt["created_at"])
            or payload["state"] not in ("SUCCEEDED", "FAILED")
            or not isinstance(result, dict)
            or type(result.get("exit_code")) is not int
            or (result["exit_code"] == 0) != (payload["state"] == "SUCCEEDED")
        ):
            raise ContractValidationError("reconciliation lacks correlated proof")
        updated = {
            **receipt,
            "state": payload["state"],
            "result": result,
            "updated_at": event.created_at,
            "reconcile_event_id": event.event_id,
            "evidence_id": evidence.evidence_id,
        }
        self._commit([("receipt", attempt_id, updated)], {("receipt", attempt_id): receipt}, event)
        return updated

    def accept_risk(self, goal_id: str, finding_id: str, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        payload = self._verify(self.user_authority, event, "user.risk", goal["baseline_digest"])
        shape(payload, {"goal_id", "baseline_digest", "finding_id", "reason", "expires_at"})
        review = self._get("review", goal_id)
        known = {
            f["finding_id"]
            for bundle in review["history"]
            for report in bundle["reviewer_results"]
            for f in report["findings"]
        }
        if (
            payload["goal_id"] != goal_id
            or payload["baseline_digest"] != goal["baseline_digest"]
            or payload["finding_id"] != finding_id
            or finding_id not in known
            or finding_id not in self.policy.acceptable_risks
            or not isinstance(payload["reason"], str)
            or not payload["reason"].strip()
            or parse_time(payload["expires_at"]) <= parse_time(self.clock())
        ):
            raise ContractValidationError("risk cannot override hard constraints or missing evidence")
        key = goal_id + ":" + finding_id
        if self._seen(event):
            return self._get("risk", key)
        old = self.store.get("risk", key)
        record = {**payload, "source_event_id": event.event_id}
        self._commit(
            [("risk", key, record)], {("risk", key): old, ("goal", goal_id): goal, ("review", goal_id): review}, event
        )
        return record

    def execute(self, operation_id: str, attempt_id: str, executor: Callable[[Operation], Payload]) -> Payload:
        operation = Operation.from_dict(self._get("operation", operation_id))
        existing = self.store.get("receipt", attempt_id)
        if existing is not None:
            if existing["operation_digest"] != operation.fingerprint:
                raise ContractValidationError("attempt identity conflict")
            return existing
        goal = self._check_operation(operation)
        review = self._review(operation.goal_id)
        if self.work_item_decision(operation.goal_id, operation.work_item_id) != "ALLOW":
            raise ContractValidationError("dependent decision is not satisfied")
        grants = self.store.list("grant", operation.goal_id)
        for candidate in grants:
            required = {"authorization_id", "kind", "goal_id", "baseline_digest", "state", "expires_at",
                        "source_event_id", "review_digest"}
            if candidate.get("kind") != "AUTONOMY_GRANT":
                required |= {"operation_digest"}
                if not candidate.get("parent_id"):
                    required.add("source_question_id")
            if not required <= candidate.keys():
                raise ContractValidationError("LEGACY_UNVERIFIED: authorization lacks trusted provenance")
        grant = next(
            (
                item
                for item in grants
                if item.get("operation_digest") == operation.fingerprint and item["state"] == "ACTIVE"
            ),
            None,
        )
        parent = None
        if grant is None:
            grant, parent = self._scope_grant(operation, goal, review)
        if grant is None or grant["baseline_digest"] != goal["baseline_digest"]:
            raise ContractValidationError("no active exact authorization")
        if grant["review_digest"] != self.store.fingerprint(review):
            raise ContractValidationError("authorization review changed")
        source = SignedEvent.from_dict(self._get_event(grant["source_event_id"]))
        if parent is None and grant.get("parent_id"):
            parent = self._get("grant", grant["parent_id"])
        if parent is not None:
            self._validate_scope(parent, operation, goal, review)
        else:
            record = QuestionRecord.from_dict(self._get("question", grant["source_question_id"])["record"])
            question = record.question
            payload = self._verify(self.user_authority, source, "user.answer", question.fingerprint)
            if (
                record.decision != "APPROVE"
                or record.answer is None
                or record.answer.source_event_id != source.event_id
                or question.operation_digest != operation.fingerprint
                or question.baseline_digest != operation.baseline_digest
                or payload
                != {
                    "answer_id": record.answer.answer_id,
                    "selected": list(record.answer.selected),
                    "custom": record.answer.custom,
                }
            ):
                raise ContractValidationError("authorization source association mismatch")
        expected = {
            ("goal", operation.goal_id): self.store.fingerprint(goal),
            ("review", operation.goal_id): self.store.fingerprint(review),
            ("operation", operation_id): self.store.fingerprint(operation.to_dict()),
        }
        if parent is not None:
            expected[("grant", parent["authorization_id"])] = self.store.fingerprint(parent)
        won, receipt = self.store.reserve(
            grant["authorization_id"], attempt_id, operation.fingerprint, self.clock(), expected,
            clock=lambda: self.clock(),
        )
        if not won:
            return receipt
        self.store.consume(attempt_id, self.clock(), expected, clock=lambda: self.clock())
        # This trusted invocation has entered the dispatcher; external completion is still unknown.
        self.store.finish(attempt_id, "RUNNING", {"operation_id": operation_id}, self.clock())
        try:
            result = executor(operation)
            if not isinstance(result, dict) or type(result.get("exit_code")) is not int:
                raise ContractValidationError("executor must provide an observed integer exit code")
            state = "SUCCEEDED" if result["exit_code"] == 0 else "FAILED"
        except Exception as exc:
            result = {"error_type": type(exc).__name__, "operation_id": operation_id}
            state = "UNKNOWN"
        return self.store.finish(attempt_id, state, result, self.clock())

    def _get_event(self, event_id: str) -> Payload:
        event = self.store.get_event(event_id)
        if event is None:
            raise ContractValidationError("missing trusted event")
        return event

    def resume_transport(self, attempt_id: str, event: SignedEvent, proof: SignedEvent,
                         executor: Callable[[Operation], Payload]) -> Payload:
        """One explicitly authorized, tested same-backend/key recovery transport.

        An acknowledged transport never resolves the original UNKNOWN receipt.
        Only the separate trusted reconciliation path can establish its outcome.
        """
        request = self._verify(self.user_authority, event, "user.resume", attempt_id)
        attestation = self._verify(self.observer_authority, proof, "observer.idempotency", attempt_id)
        shape(request, {"attempt_id", "operation_digest", "recovery_id", "backend", "key"})
        shape(attestation, {"attempt_id", "operation_digest", "backend", "key", "valid_until", "test_evidence_id"})
        old = self.store.get("recovery", attempt_id)
        if old is not None:
            if old["source_event_id"] != event.event_id or old["proof"] != proof.to_dict():
                raise ContractValidationError("Recovery slot already reserved or consumed")
            if self._get_event(event.event_id) != event.to_dict():
                raise ContractValidationError("Recovery event identity conflict")
            return old
        receipt = self._get("receipt", attempt_id)
        if receipt["state"] != "UNKNOWN" or not receipt["dispatch_right"]:
            raise ContractValidationError("Recovery requires an UNKNOWN dispatched attempt")
        operations = [Operation.from_dict(item) for item in self.store.list("operation", receipt["goal_id"])]
        operation = next((item for item in operations if item.fingerprint == receipt["operation_digest"]), None)
        if operation is None:
            raise ContractValidationError("Recovery has no original operation")
        goal = self._check_operation(operation)
        review = self._review(operation.goal_id)
        if self.work_item_decision(operation.goal_id, operation.work_item_id) != "ALLOW":
            raise ContractValidationError("Recovery blocked by pending decision")
        for name in ("attempt_id", "operation_digest", "backend", "key"):
            if request[name] != attestation[name]:
                raise ContractValidationError("Recovery proof and permission differ")
        if (request["attempt_id"] != attempt_id or request["operation_digest"] != operation.fingerprint
                or not isinstance(request["recovery_id"], str) or not request["recovery_id"].strip()
                or not isinstance(request["backend"], str) or not request["backend"].strip()
                or not isinstance(request["key"], str) or not request["key"].strip()
                or operation.arguments.get("backend") != request["backend"]
                or operation.arguments.get("idempotency_key") != request["key"]):
            raise ContractValidationError("Recovery changes original backend, key or operation")
        grant = self._get("grant", receipt["authorization_id"])
        if (grant.get("state") != "CONSUMED" or grant.get("revoked_at")
                or grant.get("operation_digest") != operation.fingerprint
                or grant.get("baseline_digest") != goal["baseline_digest"]
                or grant.get("review_digest") != self.store.fingerprint(review)):
            raise ContractValidationError("Original authorization no longer permits recovery")
        before: dict[tuple[str, str], Payload | None] = {
            ("goal", operation.goal_id): goal, ("review", operation.goal_id): review,
            ("receipt", attempt_id): receipt, ("operation", operation.operation_id): operation.to_dict(),
            ("grant", receipt["authorization_id"]): grant, ("recovery", attempt_id): None,
        }
        deadlines = [grant["expires_at"], attestation["valid_until"]]
        if grant.get("parent_id"):
            parent = self._get("grant", grant["parent_id"])
            self._validate_scope(parent, operation, goal, review)
            before[("grant", grant["parent_id"])] = parent
            deadlines.append(parent["expires_at"])
        else:
            if not {"source_event_id", "source_question_id"} <= grant.keys():
                raise ContractValidationError("LEGACY_UNVERIFIED: recovery lacks original permission")
            question_record = self._get("question", grant["source_question_id"])
            record = QuestionRecord.from_dict(question_record["record"])
            source = SignedEvent.from_dict(self._get_event(grant["source_event_id"]))
            answer = self._verify(self.user_authority, source, "user.answer", record.question.fingerprint)
            if (record.decision != "APPROVE" or record.answer is None
                    or record.answer.source_event_id != source.event_id
                    or record.question.operation_digest != operation.fingerprint
                    or answer != {"answer_id": record.answer.answer_id, "selected": list(record.answer.selected),
                                  "custom": record.answer.custom}):
                raise ContractValidationError("Recovery lacks exact original user permission")
            before[("question", record.question.question_id)] = question_record
        key = operation.goal_id + ":" + attestation["test_evidence_id"]
        tested_record = self._get("evidence", key)
        snapshot = EvidenceSnapshot.from_dict(tested_record["snapshot"])
        registered = SignedEvent.from_dict(self._get_event(tested_record["source_event_id"]))
        evidence_payload = self._verify(self.observer_authority, registered, "observer.evidence", snapshot.fingerprint)
        if evidence_payload != {"goal_id": operation.goal_id, "snapshot": snapshot.to_dict()}:
            raise ContractValidationError("Idempotency test evidence is not authenticated")
        tested = strict_loads(snapshot.content.decode("utf-8"))
        shape(tested, {"contract", "backend", "key", "operation_digest", "duplicate_same_key_same_parameters",
                       "reject_changed_parameters", "logical_effects"})
        if (not snapshot.valid_at(self.clock()) or snapshot.object_ref != request["backend"]
                or tested["contract"] != "goalanchor.idempotency-test.v1"
                or any(tested[name] != request[name] for name in ("backend", "key", "operation_digest"))
                or tested["duplicate_same_key_same_parameters"] is not True
                or tested["reject_changed_parameters"] is not True
                or type(tested["logical_effects"]) is not int or tested["logical_effects"] != 1):
            raise ContractValidationError("No fresh tested idempotency contract for original backend/key")
        before[("evidence", key)] = tested_record
        if snapshot.expires_at is not None:
            deadlines.append(snapshot.expires_at)
        valid_until = self._grant_deadline(operation.goal_id, review, min(deadlines, key=parse_time))
        recovery = {**request, "goal_id": operation.goal_id, "state": "UNKNOWN", "phase": "RESERVED",
                    "transport_right": False, "source_event_id": event.event_id, "proof": proof.to_dict(),
                    "valid_until": valid_until}

        def commit(record: Payload, source_event: SignedEvent, expected_record: Payload | None) -> None:
            expected = {**before, ("recovery", attempt_id): expected_record}
            self.store.commit([("recovery", attempt_id, record)],
                              {key: None if value is None else self.store.fingerprint(value)
                               for key, value in expected.items()}, source_event.event_id, source_event.to_dict(),
                              valid_until=valid_until, clock=lambda: self.clock())

        commit(recovery, event, None)
        consumed = {**recovery, "phase": "CONSUMED", "transport_right": True}
        consumed_event = self.observer_authority.sign(
            "controller.recovery.consume", attempt_id, consumed, at=self.clock())
        commit(consumed, consumed_event, recovery)
        try:
            result = executor(operation)
            if (not isinstance(result, dict) or type(result.get("exit_code")) is not int
                    or result.get("backend") != request["backend"] or result.get("key") != request["key"]):
                raise ContractValidationError("Recovery transport result lacks exact backend/key")
            canonical(result)
            state = "AWAITING_RECONCILIATION"
        except Exception as exc:
            result = {"error_type": type(exc).__name__}
            state = "UNKNOWN"
        completed = {**consumed, "state": state, "result": result}
        completed_event = self.observer_authority.sign(
            "controller.recovery.result", attempt_id, completed, at=self.clock())
        # Preserve the observed result even when cancellation/revocation follows dispatch.
        self._commit([("recovery", attempt_id, completed)], {("recovery", attempt_id): consumed}, completed_event)
        return completed

    def observe(self, goal_id: str, event: SignedEvent) -> Payload:
        goal = self._goal(goal_id)
        payload = self._verify(self.observer_authority, event, "observer.observation", goal["baseline_digest"])
        shape(
            payload,
            {"goal_id", "baseline_digest", "criterion_id", "object_ref", "actual", "attempt_id"},
            {"object_digest"},
        )
        if payload["goal_id"] != goal_id or payload["baseline_digest"] != goal["baseline_digest"]:
            raise ContractValidationError("observation baseline mismatch")
        baseline = Baseline.from_dict(goal["baseline"])
        criterion = next((item for item in baseline.criteria if item.criterion_id == payload["criterion_id"]), None)
        if criterion is None or criterion.object_ref != payload["object_ref"]:
            raise ContractValidationError("unknown observation object or criterion")
        receipt = self._get("receipt", payload["attempt_id"])
        finished_at = receipt["updated_at"] if "updated_at" in receipt else receipt["created_at"]
        if parse_time(event.created_at) < parse_time(finished_at):
            raise ContractValidationError("observation predates execution result")
        if receipt["goal_id"] != goal_id or receipt["state"] != "SUCCEEDED":
            raise ContractValidationError("observation requires a successful related attempt")
        op = next(
            (
                item
                for item in self.store.list("operation", goal_id)
                if Operation.from_dict(item).fingerprint == receipt["operation_digest"]
            ),
            None,
        )
        if op is None or op["baseline_digest"] != baseline.fingerprint:
            raise ContractValidationError("observation attempt belongs to old baseline")
        if receipt["result"].get("object_ref") != criterion.object_ref:
            raise ContractValidationError("receipt and observed object differ")
        actual = payload["actual"]
        raw = actual.encode("utf-8") if isinstance(actual, str) else canonical(actual).encode("utf-8")
        object_digest = hashlib.sha256(raw).hexdigest()
        if payload.get("object_digest", object_digest) != object_digest:
            raise ContractValidationError("observation object digest mismatch")
        if receipt["result"].get("object_digest", object_digest) != object_digest:
            raise ContractValidationError("observation does not match executed artifact")
        key = baseline.fingerprint + ":" + criterion.criterion_id
        record = {
            **payload,
            "status": "PASS" if canonical(actual) == criterion.expected_json else "FAIL",
            "object_digest": object_digest,
            "source_event_id": event.event_id,
            "observed_at": event.created_at,
        }
        if self._seen(event):
            return self._get("observation", key)
        old = self.store.get("observation", key)
        self._commit(
            [("observation", key, record)],
            {("observation", key): old, ("goal", goal_id): goal, ("receipt", payload["attempt_id"]): receipt},
            event,
        )
        return record

    def _reuse_record(self, goal_id: str, event: SignedEvent) -> tuple[Payload, dict[tuple[str, str], Payload | None]]:
        goal = self._goal(goal_id, active=False)
        payload = self._verify(self.observer_authority, event, "observer.reuse_observation", goal["baseline_digest"])
        shape(payload, {"goal_id", "baseline_digest", "source_baseline_digest", "criterion_id", "object_ref",
                        "actual", "object_digest", "no_impact_reason", "source_observation_fingerprint"})
        current = Baseline.from_dict(goal["baseline"])
        old_base_record = self._get("baseline", payload["source_baseline_digest"])
        prior = Baseline.from_dict(old_base_record)
        criterion = next((item for item in current.criteria if item.criterion_id == payload["criterion_id"]), None)
        previous = next((item for item in prior.criteria if item.criterion_id == payload["criterion_id"]), None)
        if (payload["goal_id"] != goal_id or payload["baseline_digest"] != current.fingerprint
                or prior.goal_id != goal_id or prior.revision >= current.revision
                or criterion is None or criterion != previous or criterion.object_ref != payload["object_ref"]
                or not isinstance(payload["no_impact_reason"], str) or not payload["no_impact_reason"].strip()
                or parse_time(event.created_at) < parse_time(goal["baseline_created_at"])):
            raise ContractValidationError("Reuse requires unchanged criterion and a current no-impact proof")
        source_key = prior.fingerprint + ":" + criterion.criterion_id
        source = self._get("observation", source_key)
        original_event = SignedEvent.from_dict(self._get_event(source["source_event_id"]))
        original_payload = self._verify(
            self.observer_authority, original_event, "observer.observation", prior.fingerprint)
        if (source.get("observation_kind") == "REUSED" or source["status"] != "PASS"
                or self.store.fingerprint(source) != payload["source_observation_fingerprint"]
                or any(source.get(key) != value for key, value in original_payload.items())
                or source["baseline_digest"] != prior.fingerprint
                or source["criterion_id"] != criterion.criterion_id
                or canonical(payload["actual"]) != canonical(source["actual"])
                or canonical(payload["actual"]) != criterion.expected_json):
            raise ContractValidationError("Reuse source observation is changed or unverifiable")
        raw = payload["actual"].encode("utf-8") if isinstance(payload["actual"], str) else canonical(
            payload["actual"]).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if payload["object_digest"] != digest or source["object_digest"] != digest:
            raise ContractValidationError("Reuse object version changed")
        receipt = self._get("receipt", source["attempt_id"])
        if (receipt["state"] != "SUCCEEDED" or receipt["goal_id"] != goal_id
                or receipt["result"].get("object_ref") != criterion.object_ref):
            raise ContractValidationError("Reuse source execution is not verified")
        record = {**payload, "observation_kind": "REUSED", "status": "PASS", "attempt_id": source["attempt_id"],
                  "source_event_id": event.event_id, "observed_at": event.created_at,
                  "source_observation": source}
        before: dict[tuple[str, str], Payload | None] = {
            ("goal", goal_id): goal, ("baseline", prior.fingerprint): old_base_record,
            ("observation", source_key): source, ("receipt", source["attempt_id"]): receipt,
        }
        return record, before

    def reuse_observation(self, goal_id: str, event: SignedEvent) -> Payload:
        self._goal(goal_id)
        record, before = self._reuse_record(goal_id, event)
        key = record["baseline_digest"] + ":" + record["criterion_id"]
        if self._seen(event):
            return self._get("observation", key)
        old = self.store.get("observation", key)
        self._commit([("observation", key, record)], {**before, ("observation", key): old}, event)
        return record

    def close(self, goal_id: str) -> Payload:
        goal = self._goal(goal_id, active=False)
        if goal["state"] == "CLOSED":
            return self._get("closure", goal["baseline_digest"])
        review = self._review(goal_id)
        baseline = Baseline.from_dict(goal["baseline"])
        before: dict[tuple[str, str], Payload | None] = {("goal", goal_id): goal, ("review", goal_id): review}
        receipts = self.store.list("receipt", goal_id)
        if any(
            item["state"] in ("UNKNOWN", "RUNNING") or item["dispatch_right"] and item["state"] == "NOT_STARTED"
            for item in receipts
        ):
            raise ContractValidationError("unknown or in-progress effects block closure")
        for operation_id in baseline.required_operations:
            operation = self._get("operation", operation_id)
            if operation["baseline_digest"] != baseline.fingerprint or not any(
                item["state"] == "SUCCEEDED" and item["operation_digest"] == Operation.from_dict(operation).fingerprint
                for item in receipts
            ):
                raise ContractValidationError("required operation not verified")
            before[("operation", operation_id)] = operation
        for criterion in baseline.criteria:
            key = baseline.fingerprint + ":" + criterion.criterion_id
            observation = self._get("observation", key)
            if observation["status"] != "PASS" or observation["baseline_digest"] != baseline.fingerprint:
                raise ContractValidationError("acceptance criterion not verified")
            event = SignedEvent.from_dict(self._get_event(observation["source_event_id"]))
            if observation.get("observation_kind") == "REUSED":
                expected_observation, reuse_before = self._reuse_record(goal_id, event)
                if expected_observation != observation:
                    raise ContractValidationError("Reused observation provenance changed")
                if any(key in before and before[key] != value for key, value in reuse_before.items()):
                    raise ContractValidationError("Closure dependencies changed during reuse validation")
                before.update(reuse_before)
            else:
                self._verify(self.observer_authority, event, "observer.observation", baseline.fingerprint)
            before[("observation", key)] = observation
        if any(
            self.work_item_decision(goal_id, item["record"]["question"]["blocking_items"][0]) != "ALLOW"
            for item in self.store.list("question", goal_id)
            if item["baseline_digest"] == baseline.fingerprint and item["record"]["question"]["blocking_items"]
        ):
            raise ContractValidationError("unresolved decision blocks closure")
        closed = {**goal, "state": "CLOSED"}
        event = self.observer_authority.sign(
            "controller.close",
            baseline.fingerprint,
            {"goal_id": goal_id, "baseline_digest": baseline.fingerprint,
             "valid_until": self._grant_deadline(goal_id, review, "9999-12-31T23:59:59+00:00")},
            at=self.clock(),
        )
        self._commit(
            [("goal", goal_id, closed), ("closure", baseline.fingerprint, closed)],
            {**before, ("closure", baseline.fingerprint): None},
            event,
        )
        return closed
