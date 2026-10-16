"""Cross-host installation and discovery helpers for the GoalAnchor skill.

The skill content is host-neutral.  This module only maps that content to the
filesystem roots used by common Agent Skills hosts; it does not invoke a host
CLI, shell, model, or subagent API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SKILL_ID = "goalanchor"
_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class HostSpec:
    """Filesystem mapping for one host family."""

    name: str
    user_env: str | None
    user_dir: str
    project_dir: tuple[str, ...]
    notes: str


HOSTS: dict[str, HostSpec] = {
    # Codex accepts the shared Agent Skills root for project-local skills.  A
    # user install still follows its native CODEX_HOME root.
    "codex": HostSpec(
        name="codex",
        user_env="CODEX_HOME",
        user_dir=".codex",
        project_dir=(".agents", "skills"),
        notes="Codex user root or shared project .agents/skills root",
    ),
    "agents": HostSpec(
        name="agents",
        user_env=None,
        user_dir=".agents",
        project_dir=(".agents", "skills"),
        notes="Shared Agent Skills-compatible root",
    ),
    "claude": HostSpec(
        name="claude",
        user_env=None,
        user_dir=".claude",
        project_dir=(".claude", "skills"),
        notes="Claude Code project or user skills root",
    ),
    "dsh": HostSpec(
        name="dsh",
        user_env="DSH_HOME",
        user_dir=".dsh",
        project_dir=(".dsh", "skills"),
        notes="DeepSeek Harness project or user skills root",
    ),
    "cursor": HostSpec(
        name="cursor",
        user_env=None,
        user_dir=".cursor",
        project_dir=(".cursor", "skills"),
        notes="Cursor project or user skills root",
    ),
    "gemini": HostSpec(
        name="gemini",
        user_env=None,
        user_dir=".gemini",
        project_dir=(".gemini", "skills"),
        notes="Gemini CLI project or user skills root",
    ),
    "opencode": HostSpec(
        name="opencode",
        user_env=None,
        user_dir=str(Path(".config") / "opencode"),
        project_dir=(".opencode", "skills"),
        notes="OpenCode project or user skills root",
    ),
    "pi": HostSpec(
        name="pi",
        user_env=None,
        user_dir=str(Path(".pi") / "agent"),
        project_dir=(".pi", "skills"),
        notes="Pi project or user skills root",
    ),
    "zcode": HostSpec(
        name="zcode",
        user_env=None,
        user_dir="",
        project_dir=(),
        notes="Zcode requires an explicit skill root because its native discovery path is host-configured",
    ),
    # A custom destination is the honest fallback for tools with a different
    # discovery convention.  It keeps this module from making unsupported
    # claims about every host in the ecosystem.
    "custom": HostSpec(
        name="custom",
        user_env=None,
        user_dir="",
        project_dir=(),
        notes="Use --destination for a host-specific skill root",
    ),
}


def _home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user's home without relying on a shell-specific variable."""

    if environ:
        # Tests and embedding applications can provide an explicit home while
        # retaining the platform's normal Path.home() behavior by default.
        keys = ("USERPROFILE", "HOME") if os.name == "nt" else ("HOME", "USERPROFILE")
        for key in keys:
            value = environ.get(key)
            if value:
                return Path(value).expanduser()
    return Path.home()


def _user_base(spec: HostSpec, environ: Mapping[str, str], home: Path) -> Path:
    if spec.user_env:
        configured = environ.get(spec.user_env)
        if configured:
            return Path(configured).expanduser()
    if not spec.user_dir:
        raise ValueError(f"{spec.name} host requires an explicit destination")
    return home / spec.user_dir


def resolve_destination(
    target: str,
    *,
    scope: str = "user",
    project_root: str | Path | None = None,
    destination: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> Path:
    """Resolve the final ``<skill-root>/goalanchor`` path for a host.

    The result is a ``Path`` and never a shell command.  ``destination`` is
    intentionally explicit for hosts not listed in :data:`HOSTS` or for
    deployments with a custom skill root.
    """

    normalized_target = target.strip().lower()
    if normalized_target not in HOSTS:
        allowed = ", ".join(sorted(HOSTS))
        raise ValueError(f"unknown target {target!r}; choose one of: {allowed}")
    if scope not in {"user", "project"}:
        raise ValueError("scope must be 'user' or 'project'")

    if destination is not None:
        return Path(destination).expanduser() / SKILL_ID

    spec = HOSTS[normalized_target]
    env = dict(os.environ if environ is None else environ)
    home_path = _home(env) if home is None else Path(home).expanduser()
    if scope == "user":
        root = _user_base(spec, env, home_path) / "skills"
    else:
        if project_root is None:
            raise ValueError("project scope requires project_root")
        if not spec.project_dir:
            raise ValueError("custom host requires --destination")
        root = Path(project_root).expanduser().resolve() / Path(*spec.project_dir)
    return root / SKILL_ID


def find_skill_source(source: str | Path | None = None) -> Path:
    """Find a repository skill directory containing ``SKILL.md``."""

    if source is not None:
        candidates = [Path(source).expanduser()]
    else:
        package_dir = Path(__file__).resolve().parent
        package_root = package_dir.parent
        # Prefer the skill bundled inside the installed package (works for pip
        # installs); fall back to the repository checkout when running from a
        # clone, then the current working directory.
        candidates = [
            package_dir / "_skill",
            package_root / "skills" / SKILL_ID,
            Path.cwd() / "skills" / SKILL_ID,
        ]
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir() and (candidate / _SKILL_FILE).is_file():
            return candidate
    searched = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(f"could not find {_SKILL_FILE}; searched: {searched}")


def _frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    lines = content.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def validate_skill_directory(skill_dir: str | Path) -> dict[str, str | int]:
    """Validate the portable minimum and return a small machine-readable report."""

    root = Path(skill_dir).expanduser().resolve()
    skill_file = root / _SKILL_FILE
    if not skill_file.is_file():
        raise ValueError(f"missing {_SKILL_FILE}: {skill_file}")
    metadata = _frontmatter(skill_file.read_text(encoding="utf-8"))
    if metadata.get("name") != SKILL_ID:
        raise ValueError(f"SKILL.md name must be {SKILL_ID!r}")
    if not metadata.get("description"):
        raise ValueError("SKILL.md description must be non-empty")
    digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    return {
        "skill_id": SKILL_ID,
        "path": str(root),
        "sha256": digest,
        "bytes": skill_file.stat().st_size,
    }


def install_skill(
    target: str,
    *,
    scope: str = "user",
    source: str | Path | None = None,
    project_root: str | Path | None = None,
    destination: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    """Install the standalone skill bundle without overwriting by default."""

    source_dir = find_skill_source(source)
    source_report = validate_skill_directory(source_dir)
    target_dir = resolve_destination(
        target,
        scope=scope,
        project_root=project_root,
        destination=destination,
        environ=environ,
        home=home,
    )
    if target_dir.exists() and not force:
        raise FileExistsError(
            f"destination already exists: {target_dir}; use --force only when replacement is intended"
        )
    result: dict[str, object] = {
        "target": target,
        "scope": scope,
        "source": source_report,
        "destination": str(target_dir),
        "dry_run": dry_run,
        "replaced": target_dir.exists(),
    }
    if dry_run:
        return result

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir, dirs_exist_ok=force)
    result["installed"] = validate_skill_directory(target_dir)
    return result


def check_install(
    target: str,
    *,
    scope: str = "user",
    source: str | Path | None = None,
    project_root: str | Path | None = None,
    destination: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> dict[str, object]:
    """Compare an installed skill's bytes with the portable source."""

    source_dir = find_skill_source(source)
    destination_dir = resolve_destination(
        target,
        scope=scope,
        project_root=project_root,
        destination=destination,
        environ=environ,
        home=home,
    )
    source_report = validate_skill_directory(source_dir)
    installed_report = validate_skill_directory(destination_dir)
    return {
        "target": target,
        "scope": scope,
        "source": source_report,
        "destination": installed_report,
        "match": source_report["sha256"] == installed_report["sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goalanchor",
        description="Install and verify the portable GoalAnchor Agent Skill bundle.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("workflow", help="signed durable workflow, inspection, recovery and safe local demo")
    for command in ("install", "check"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--target", required=True, choices=sorted(HOSTS))
        sub.add_argument("--scope", choices=("user", "project"), default="user")
        sub.add_argument("--source", type=Path)
        sub.add_argument("--project-root", type=Path)
        sub.add_argument(
            "--destination",
            type=Path,
            help="custom skill root; the goalanchor directory is appended",
        )
        sub.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        if command == "install":
            sub.add_argument("--force", action="store_true")
            sub.add_argument("--dry-run", action="store_true")
    gate = subparsers.add_parser(
        "gate", help="evaluate a serialized GoalAnchor PLAN/VERIFY/EXECUTE/EVIDENCE record"
    )
    gate.add_argument(
        "--phase",
        required=True,
        choices=(
            "plan",
            "verify",
            "execute",
            "plan-change",
            "evidence",
            "conclusion",
            "experiment",
            "design",
            "artifact",
            "artifact-file",
            "provenance",
            "closure",
            "review",
            "review-cycle",
            "graphify",
        ),
    )
    gate.add_argument("--record", required=True, type=Path)
    gate.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    for cycle_name, cycle_help in (
        ("review-cycle", "run a bounded multi-Agent review through a JSON-over-stdio bridge"),
        ("review-run", "alias of review-cycle"),
    ):
        review_cycle = subparsers.add_parser(cycle_name, help=cycle_help)
        review_cycle.add_argument("--request", required=True, type=Path)
        review_cycle.add_argument("--config", required=True, type=Path)
        review_cycle.add_argument(
            "--model",
            default=None,
            help=(
                "pin every sub-agent to this model; pass the main agent's CURRENT "
                "model (re-checked at each run so a mid-run switch stays in sync); "
                "overrides the config's top-level 'model'"
            ),
        )
        review_cycle.add_argument(
            "--dry-run",
            action="store_true",
            help="render the round-1 reviewer prompts without invoking any agent",
        )
        review_cycle.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    platforms_parser = subparsers.add_parser(
        "platforms",
        help="report built-in platform adapters and profile dispatch readiness",
    )
    platforms_parser.add_argument("--config", type=Path, default=None)
    platforms_parser.add_argument(
        "--probe",
        action="store_true",
        help="run one minimal invocation per available built-in platform (costs tokens)",
    )
    platforms_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    review_prompt = subparsers.add_parser(
        "review-prompt",
        help="render the per-role review prompt for host-native (visible) subagents",
    )
    review_prompt.add_argument("--request", required=True, type=Path)
    review_prompt.add_argument(
        "--role", required=True, choices=("reviewer", "arbiter", "meta_reviewer")
    )
    review_prompt.add_argument("--agent-id", default=None, help="defaults to the role name")
    review_prompt.add_argument("--round", type=int, default=1)
    review_prompt.add_argument(
        "--reviewer-results",
        type=Path,
        default=None,
        help="required for --role arbiter; optional for meta_reviewer",
    )
    review_prompt.add_argument(
        "--arbiter-result", type=Path, default=None, help="required for --role meta_reviewer"
    )
    review_prompt.add_argument("--dispute", type=Path, default=None)
    review_prompt.add_argument(
        "--language",
        default="en",
        choices=("en", "zh"),
        help="prompt prose language; headers and schema stay machine-anchored",
    )
    return parser


def _run_gate(phase: str, record_path: Path) -> dict[str, object]:
    """Load a JSON record and run only the requested pure gate."""

    from .closure import (
        ArtifactManifest,
        ClosureRecord,
        ProvenanceChain,
        check_artifact_manifest,
        check_closure,
        check_provenance,
        verify_artifact_file,
    )
    from .core import GoalContract, WorkItem
    from .evidence import (
        DesignDecision,
        EvidenceRecord,
        ExperimentPlan,
        check_conclusion,
        check_design_necessity,
        check_evidence,
        check_experiment,
    )
    from .gates import (
        ExecutePreflight,
        StructureReview,
        VerifyRecord,
        check_execute,
        check_plan_change,
        check_verify,
        evaluate_plan,
    )
    from .integration import ReviewFinding, check_graphify_output, check_review_output
    from .review_service import (
        ArbiterResult,
        MetaReviewResult,
        ReviewContext,
        ReviewerResult,
        evaluate_review_cycle,
    )

    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"gate record is not valid JSON: {record_path}") from exc
    data = payload if isinstance(payload, dict) else None
    if data is None:
        raise ValueError("gate record must be a JSON object")

    try:
        if phase == "plan":
            contract = GoalContract.from_dict(data["contract"])
            work_items = tuple(WorkItem.from_dict(item) for item in data.get("work_items", ()))
            review_payload = data.get("structure_review")
            review = (
                None
                if review_payload is None
                else StructureReview.from_dict(review_payload)
            )
            return evaluate_plan(contract, work_items, review).to_dict()
        if phase == "verify":
            verify_payload = data.get("verify")
            verify = None if verify_payload is None else VerifyRecord.from_dict(verify_payload)
            return check_verify(verify).to_dict()
        if phase == "execute":
            contract = GoalContract.from_dict(data["contract"])
            preflight_payload = data.get("execute")
            preflight = (
                None
                if preflight_payload is None
                else ExecutePreflight.from_dict(preflight_payload)
            )
            return check_execute(contract, preflight).to_dict()
        if phase == "plan-change":
            before = GoalContract.from_dict(data["before_contract"])
            after = GoalContract.from_dict(data["after_contract"])
            return check_plan_change(before, after).to_dict()
        if phase == "evidence":
            return check_evidence(EvidenceRecord.from_dict(data["evidence"])).to_dict()
        if phase == "conclusion":
            records = tuple(EvidenceRecord.from_dict(item) for item in data.get("evidence", ()))
            return check_conclusion(data["conclusion_level"], records).to_dict()
        if phase == "experiment":
            return check_experiment(ExperimentPlan.from_dict(data["experiment"])).to_dict()
        if phase == "design":
            return check_design_necessity(
                DesignDecision.from_dict(data["design"])
            ).to_dict()
        if phase == "artifact":
            manifest_data = data.get("artifact_manifest")
            manifest = None if manifest_data is None else ArtifactManifest.from_dict(manifest_data)
            return check_artifact_manifest(manifest).to_dict()
        if phase == "artifact-file":
            manifest_data = data.get("artifact_manifest")
            manifest = None if manifest_data is None else ArtifactManifest.from_dict(manifest_data)
            return verify_artifact_file(manifest).to_dict()
        if phase == "provenance":
            provenance_data = data.get("provenance")
            chain = None if provenance_data is None else ProvenanceChain.from_dict(provenance_data)
            manifest_data = data.get("artifact_manifest")
            manifest = None if manifest_data is None else ArtifactManifest.from_dict(manifest_data)
            return check_provenance(chain, manifest).to_dict()
        if phase == "closure":
            closure_data = data.get("closure")
            record = None if closure_data is None else ClosureRecord.from_dict(closure_data)
            return check_closure(record).to_dict()
        if phase == "review":
            return check_review_output(data).to_dict()
        if phase == "review-cycle":
            declaration = _require_risk_declaration(data)
            context = ReviewContext.from_dict(data["context"])
            reviewer_results = tuple(
                ReviewerResult(
                    reviewer_id=item["reviewer_id"],
                    round_number=item["round_number"],
                    verdict=item["verdict"],
                    findings=tuple(ReviewFinding.from_dict(finding) for finding in item.get("findings", ())),
                    isolated=item.get("isolated", True),
                    context_fingerprint=item.get("context_fingerprint"),
                )
                for item in data["reviewer_results"]
            )
            arbiter_data = data["arbiter"]
            try:
                arbiter = ArbiterResult(**arbiter_data)
            except TypeError as exc:
                raise ValueError(f"arbiter result does not match the schema: {exc}") from exc
            meta_data = data.get("meta_review")
            if meta_data is None:
                meta = None
            else:
                try:
                    meta = MetaReviewResult(
                        reviewer_id=meta_data["reviewer_id"],
                        round_number=meta_data["round_number"],
                        arbiter_id=meta_data["arbiter_id"],
                        verdict=meta_data["verdict"],
                        summary=meta_data["summary"],
                        findings=tuple(ReviewFinding.from_dict(finding) for finding in meta_data.get("findings", ())),
                        context_fingerprint=meta_data["context_fingerprint"],
                        agrees_with_arbiter=meta_data["agrees_with_arbiter"],
                        isolated=meta_data.get("isolated", True),
                    )
                except TypeError as exc:
                    raise ValueError(f"meta review does not match the schema: {exc}") from exc
            result = evaluate_review_cycle(
                context,
                data["risk"],
                reviewer_results,
                arbiter,
                meta,
                round_number=data["round_number"],
                max_rounds=data.get("max_rounds", 2),
            )
            payload = result.to_dict()
            if declaration is not None:
                payload["risk_declaration"] = declaration
            return payload
        if phase == "graphify":
            return check_graphify_output(data).to_dict()
    except KeyError as exc:
        raise ValueError(f"gate record is missing required field: {exc.args[0]}") from exc
    raise ValueError(f"unsupported gate phase: {phase}")


def _require_risk_declaration(data: Mapping[str, object]) -> dict[str, object] | None:
    """CRITICAL reviews must carry a signed risk declaration in the record."""

    declaration = data.get("risk_declaration")
    if data.get("risk") != "CRITICAL":
        if declaration is None:
            return None
        if not isinstance(declaration, Mapping):
            raise ValueError("risk_declaration must be a JSON object")
        return dict(declaration)
    if not isinstance(declaration, Mapping):
        raise ValueError(
            "CRITICAL risk requires risk_declaration with non-empty declared_by and reason"
        )
    declared_by = declaration.get("declared_by")
    reason = declaration.get("reason")
    if not isinstance(declared_by, str) or not declared_by.strip():
        raise ValueError("risk_declaration.declared_by must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("risk_declaration.reason must be a non-empty string")
    return {"declared_by": declared_by.strip(), "reason": reason.strip()}


def _model_audit_warnings(
    adapter: object, config: Mapping[str, object]
) -> tuple[list[dict[str, str]], str | None]:
    """Collect machine-readable model-consistency warnings for CLI output.

    Platform dispatches without a declared main model get ``MODEL_NOT_PINNED``
    (the stderr text stays for humans); a user-approved model divergence gets
    ``MODEL_DIVERGENCE_APPROVED`` so pipelines can see the exception without
    knowing the config key. Legacy stdio-bridge adapters manage their own
    model and produce no warnings by design.

    Returns the warnings list and the declared main model (``None`` when the
    adapter does not declare one).
    """

    warnings: list[dict[str, str]] = []
    declared = getattr(adapter, "declared_main_model", None)
    model = None
    if callable(declared):
        model = declared()
        if model is None:
            print(
                "goalanchor: warning: the main agent's model is not declared; sub-agent "
                "model consistency cannot be enforced. Pass --model <main-model> or set "
                "the config's top-level 'model'.",
                file=sys.stderr,
            )
            warnings.append(
                {
                    "code": "MODEL_NOT_PINNED",
                    "message": (
                        "the main agent's model is not declared, so sub-agent model "
                        "consistency cannot be enforced; sub-agents may run on "
                        "platform/profile-declared models or on a platform default. "
                        "Pass --model <main-model> or set the config's top-level "
                        "'model'."
                    ),
                }
            )
        divergence = config.get("model_divergence")
        if isinstance(divergence, Mapping) and divergence:
            warnings.append(
                {
                    "code": "MODEL_DIVERGENCE_APPROVED",
                    "message": (
                        "a user-approved model divergence is recorded; sub-agents are "
                        "intentionally allowed to run on models other than the main "
                        "agent's current model."
                    ),
                    "confirmed_by": str(divergence.get("confirmed_by", "")),
                }
            )
    return warnings, model


def _run_external_review_cycle(
    request_path: Path, config_path: Path, model: str | None = None
) -> dict[str, object]:
    from .agent_runtime import run_review_cycle_with_adapter
    from .platforms import adapter_from_config
    from .review_service import ReviewContext, ReviewRisk

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("review-cycle request or config is not valid JSON") from exc
    if not isinstance(request, Mapping) or not isinstance(config, Mapping):
        raise ValueError("review-cycle request and config must be JSON objects")
    if model:
        config = {**config, "model": model}
    try:
        context = ReviewContext.from_dict(request["context"])
        risk = ReviewRisk(request["risk"])
    except KeyError as exc:
        raise ValueError(f"review-cycle request is missing required field: {exc.args[0]}") from exc
    risk_declaration = _require_risk_declaration(request)
    adapter = adapter_from_config(config)
    warnings, declared_model = _model_audit_warnings(adapter, config)
    result = run_review_cycle_with_adapter(
        context,
        risk,
        adapter,
        max_rounds=request.get("max_rounds", 2),
        max_workers=request.get("max_workers"),
    )
    payload = result.to_dict()
    if risk_declaration is not None:
        payload["risk_declaration"] = risk_declaration
    payload["warnings"] = warnings
    model_divergence = config.get("model_divergence")
    if isinstance(model_divergence, Mapping) and model_divergence:
        payload["model_divergence"] = dict(model_divergence)
    usage_report = getattr(adapter, "usage_report", None)
    if callable(usage_report) and not payload.get("agent_bindings"):
        payload["agent_bindings"] = list(usage_report())
    bindings = payload.get("agent_bindings") or []
    payload["usage_summary"] = {
        "calls": len(bindings),
        "tokens_used_total": sum(
            item["tokens_used"]
            for item in bindings
            if isinstance(item.get("tokens_used"), int)
        ),
        "models_used": sorted(
            {
                item["model"]
                for item in bindings
                if isinstance(item.get("model"), str) and item["model"]
            }
        ),
        "main_model": declared_model,
    }
    return payload


def _load_json_file(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc


def _run_review_prompt(args: argparse.Namespace) -> str:
    from .agent_runtime import AgentRequest, AgentRole
    from .platforms import render_agent_prompt
    from .review_service import (
        ArbiterResult,
        DisputePacket,
        ReviewContext,
        ReviewerResult,
        ReviewRisk,
    )

    request = _load_json_file(args.request, "review-prompt request")
    if not isinstance(request, Mapping):
        raise ValueError("review-prompt request must be a JSON object")
    try:
        context = ReviewContext.from_dict(request["context"])
        risk = ReviewRisk(request["risk"])
    except KeyError as exc:
        raise ValueError(f"review-prompt request is missing required field: {exc.args[0]}") from exc
    role = AgentRole(args.role)
    dispute = None
    if args.dispute is not None:
        dispute_data = _load_json_file(args.dispute, "dispute packet")
        if not isinstance(dispute_data, Mapping):
            raise ValueError("dispute packet must be a JSON object")
        dispute = DisputePacket.from_dict(cast(Mapping[str, Any], dispute_data))
    reviewer_results: tuple[ReviewerResult, ...] = ()
    if role is AgentRole.ARBITER and args.reviewer_results is None:
        raise ValueError("arbiter prompts require --reviewer-results")
    if args.reviewer_results is not None:
        data = _load_json_file(args.reviewer_results, "reviewer results")
        if not isinstance(data, list):
            raise ValueError("reviewer results must be a JSON array")
        reviewer_results = tuple(ReviewerResult.from_dict(item) for item in data)
    arbiter_result = None
    if role is AgentRole.META_REVIEWER:
        if args.arbiter_result is None:
            raise ValueError("meta_reviewer prompts require --arbiter-result")
        arbiter_data = _load_json_file(args.arbiter_result, "arbiter result")
        if not isinstance(arbiter_data, Mapping):
            raise ValueError("arbiter result must be a JSON object")
        arbiter_result = ArbiterResult.from_dict(cast(Mapping[str, Any], arbiter_data))
    agent_request = AgentRequest(
        role=role,
        agent_id=args.agent_id or role.value,
        round_number=args.round,
        risk=risk,
        context=context,
        dispute_packet=dispute,
        reviewer_results=reviewer_results,
        arbiter_result=arbiter_result,
    )
    return render_agent_prompt(agent_request, language=args.language)


def _dry_run_review_cycle(
    request_path: Path, config_path: Path, model: str | None = None
) -> dict[str, object]:
    """Render round-1 reviewer prompts without invoking any agent."""

    from .agent_runtime import AgentRequest, AgentRole
    from .platforms import adapter_from_config, render_agent_prompt
    from .review_service import ReviewContext, ReviewRisk, required_reviewers

    request = _load_json_file(request_path, "review-cycle request")
    config = _load_json_file(config_path, "review-cycle config")
    if not isinstance(request, Mapping) or not isinstance(config, Mapping):
        raise ValueError("review-cycle request and config must be JSON objects")
    if model:
        config = {**config, "model": model}
    # Fail fast on dispatch/model-consistency errors before any agent launch.
    adapter = adapter_from_config(config)
    warnings, _ = _model_audit_warnings(adapter, config)
    try:
        context = ReviewContext.from_dict(request["context"])
        risk = ReviewRisk(request["risk"])
    except KeyError as exc:
        raise ValueError(f"review-cycle request is missing required field: {exc.args[0]}") from exc
    required = required_reviewers(risk)
    prompts: dict[str, str] = {}
    for index in range(required):
        agent_id = f"reviewer-{index + 1}"
        agent_request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id=agent_id,
            round_number=1,
            risk=risk,
            context=context,
        )
        prompts[agent_id] = render_agent_prompt(agent_request)
    return {
        "dry_run": True,
        "risk": risk.value,
        "required_reviewers": required,
        "main_model": config.get("model"),
        "warnings": warnings,
        "model_divergence": (
            dict(config["model_divergence"])
            if isinstance(config.get("model_divergence"), Mapping)
            else None
        ),
        "reviewer_prompts": prompts,
        "note": (
            "arbiter and meta_reviewer prompts are generated after the reviewer results "
            "arrive; render them with: python -m goalanchor review-prompt"
        ),
    }


def _run_platforms_report(config_path: Path | None, *, probe: bool = False) -> dict[str, object]:
    """Report built-in platform adapters and, with --config, profile readiness."""

    import shutil as _shutil

    from .agent_runtime import AgentRole
    from .platforms import (
        BUILTIN_PLATFORMS,
        PlatformDispatchConfig,
        probe_platform,
    )
    from .platforms.platform_common import _BUILTIN_ARGV, _BUILTIN_MODEL_FLAGS

    def _resolve(argv: tuple[str, ...]) -> str | None:
        found = _shutil.which(argv[0])
        return None if found is None else str(found)

    # Load the config before probing: an invalid config fails fast instead of
    # after spending probe calls, and each probe can then use the model the
    # dispatch would actually pin for that platform.
    dispatch = None
    if config_path is not None:
        config = _load_json_file(config_path, "platforms config")
        if not isinstance(config, Mapping):
            raise ValueError("platforms config must be a JSON object")
        dispatch = PlatformDispatchConfig.from_dict(config)
    builtins = []
    for name in BUILTIN_PLATFORMS:
        argv = _BUILTIN_ARGV[name]
        resolved_path = _resolve(argv)
        entry: dict[str, object] = {
            "platform": name,
            "argv": list(argv),
            "prompt_mode": "stdin" if "{prompt}" not in argv else "argv",
            "model_flag": _BUILTIN_MODEL_FLAGS.get(name, "-m"),
            "resolved": resolved_path,
            "available": resolved_path is not None,
        }
        if probe:
            if resolved_path is None:
                entry["probe"] = "not-installed"
            else:
                probe_model = None
                if dispatch is not None:
                    # Same precedence as dispatch (profile > platform >
                    # top-level), resolved through the first enabled profile
                    # bound to this platform; platforms without a bound
                    # profile fall back to their own model, then the main one.
                    for profile in dispatch.profiles:
                        if profile.enabled and profile.platform == name:
                            profile_spec = dispatch.resolve_platform(profile)
                            probe_model = (
                                profile_spec.model
                                if profile_spec.model is not None
                                else dispatch.model
                            )
                            break
                    if probe_model is None:
                        spec = dispatch.platforms.get(name)
                        probe_model = (spec.model if spec is not None else None) or dispatch.model
                entry.update(probe_platform(name, model=probe_model))
        builtins.append(entry)
    report: dict[str, object] = {"builtin_platforms": builtins}
    if dispatch is not None:
        report["profiles"] = [
            {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "roles": [role.value for role in profile.roles],
                "platform": profile.platform,
                "bridge_command": list(profile.command) if profile.command else None,
                "model": profile.model,
                "enabled": profile.enabled,
            }
            for profile in dispatch.profiles
        ]
        report["role_bindings"] = dict(dispatch.role_bindings)
        report["main_model"] = dispatch.model
        report["model_divergence"] = dispatch.model_divergence
        reviewer_profiles = [
            profile.profile_id
            for profile in dispatch.profiles
            if profile.enabled and AgentRole.REVIEWER in profile.roles
        ]
        report["reviewer_profile_count"] = len(reviewer_profiles)
        report["max_supported_risk"] = (
            "CRITICAL"
            if len(reviewer_profiles) >= 3
            else "HIGH"
            if len(reviewer_profiles) >= 2
            else "MEDIUM"
            if len(reviewer_profiles) >= 1
            else None
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "workflow":
        from .workflow_cli import main as workflow_main

        return workflow_main(arguments[1:])
    args = _parser().parse_args(arguments)
    try:
        if args.command == "gate":
            result = _run_gate(args.phase, args.record)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            if args.phase == "review-cycle":
                return 1 if result["outcome"] in {"STOPPED", "HUMAN_DECISION_REQUIRED"} else 0
            return 0 if result["passed"] else 1
        if args.command in ("review-cycle", "review-run"):
            model_override = getattr(args, "model", None)
            if getattr(args, "dry_run", False):
                result = _dry_run_review_cycle(args.request, args.config, model=model_override)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
                return 0
            result = _run_external_review_cycle(args.request, args.config, model=model_override)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 1 if result["outcome"] in {"STOPPED", "HUMAN_DECISION_REQUIRED"} else 0
        if args.command == "platforms":
            report = _run_platforms_report(args.config, probe=getattr(args, "probe", False))
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.command == "review-prompt":
            print(_run_review_prompt(args))
            return 0
        kwargs = {
            "target": args.target,
            "scope": args.scope,
            "source": args.source,
            "project_root": args.project_root,
            "destination": args.destination,
        }
        if args.command == "install":
            result = install_skill(
                **kwargs,
                force=args.force,
                dry_run=args.dry_run,
            )
        else:
            result = check_install(**kwargs)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        if args.command == "check" and not result["match"]:
            return 1
        return 0
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"goalanchor: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "HOSTS",
    "SKILL_ID",
    "HostSpec",
    "check_install",
    "find_skill_source",
    "install_skill",
    "main",
    "resolve_destination",
    "validate_skill_directory",
]
