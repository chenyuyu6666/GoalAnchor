"""Cross-host installation and discovery helpers for the GoalAnchor skill.

The skill content is host-neutral.  This module only maps that content to the
filesystem roots used by common Agent Skills hosts; it does not invoke a host
CLI, shell, model, or subagent API.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Mapping, Sequence


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
        raise ValueError("custom host requires an explicit destination")
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
        package_root = Path(__file__).resolve().parents[1]
        candidates = [
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
            "graphify",
        ),
    )
    gate.add_argument("--record", required=True, type=Path)
    gate.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _run_gate(phase: str, record_path: Path) -> dict[str, object]:
    """Load a JSON record and run only the requested pure gate."""

    from .core import GoalContract, WorkItem
    from .gates import (
        ExecutePreflight,
        StructureReview,
        VerifyRecord,
        check_execute,
        check_plan_change,
        check_verify,
        evaluate_plan,
    )
    from .evidence import (
        DesignDecision,
        EvidenceRecord,
        ExperimentPlan,
        check_conclusion,
        check_design_necessity,
        check_evidence,
        check_experiment,
    )
    from .closure import (
        ArtifactManifest,
        ClosureRecord,
        ProvenanceChain,
        check_artifact_manifest,
        check_closure,
        check_provenance,
        verify_artifact_file,
    )
    from .integration import check_graphify_output, check_review_output

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
        if phase == "graphify":
            return check_graphify_output(data).to_dict()
    except KeyError as exc:
        raise ValueError(f"gate record is missing required field: {exc.args[0]}") from exc
    raise ValueError(f"unsupported gate phase: {phase}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "gate":
            result = _run_gate(args.phase, args.record)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 0 if result["passed"] else 1
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
