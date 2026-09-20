"""Built-in multi-platform Agent dispatch for the review execution layer.

The review policy lives in :mod:`goalanchor.review_service`; this module is the
execution adapter that makes the policy usable on real platforms.  It maps the
host-neutral review roles (``reviewer`` / ``arbiter`` / ``meta_reviewer``) onto
named profiles that run either on a built-in agent CLI (Codex, Claude Code,
Gemini CLI, OpenCode) or behind a JSON-over-stdio bridge command.

The adapter never changes review semantics: it builds commands, renders the
shared prompt, parses the platform's output envelope, and routes profiles with
the same isolation rules as the rest of the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
import re
from threading import Lock
import shutil
import subprocess
from typing import Mapping, Sequence

from .agent_runtime import (
    AgentProfile,
    AgentRequest,
    AgentRole,
    AgentRuntimeCapabilities,
    AgentRuntimeError,
    CatalogAgentAdapter,
    JsonStdioAgentAdapter,
)
from .review_service import ArbiterResult, DisputePacket, ReviewContext


BUILTIN_PLATFORMS: tuple[str, ...] = ("codex", "claude", "gemini", "opencode")

# All built-in templates pipe the prompt through stdin (no ``{prompt}`` argv
# slot). On Windows these CLIs are .cmd shims, and multi-line prompts passed
# as argv get mangled by cmd.exe quoting; stdin also removes the ~32K argv
# length limit for large review contexts.
_BUILTIN_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec", "-"),
    "claude": ("claude", "-p"),
    "gemini": ("gemini",),
    "opencode": ("opencode", "run"),
}

_ENVELOPE_KEYS: tuple[str, ...] = ("result", "response", "content", "output", "message", "text")
_RESULT_KEYS: frozenset[str] = frozenset({
    "verdict",
    "reviewer_id",
    "arbiter_id",
    "summary",
    "findings",
    "disagrees_with_reviewers",
    "agrees_with_arbiter",
})
_MAX_ENVELOPE_DEPTH = 8

_ROLE_LABELS: dict[AgentRole, str] = {
    AgentRole.REVIEWER: "independent reviewer",
    AgentRole.ARBITER: "arbiter",
    AgentRole.META_REVIEWER: "meta reviewer",
}


def _enum(value: object, enum_type: type, field_name: str) -> object:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeError(f"{field_name} is invalid") from exc


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentRuntimeError(f"{field_name} must be a non-empty string")
    return value.strip()


def _roles(value: object, field_name: str) -> tuple[AgentRole, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AgentRuntimeError(f"{field_name} must be an array of role strings")
    roles = tuple(_enum(item, AgentRole, f"{field_name}[]") for item in value)
    if not roles or len(roles) != len(set(roles)):
        raise AgentRuntimeError(f"{field_name} must contain unique AgentRole values")
    return roles


def _retries(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentRuntimeError(f"{field_name} must be a positive integer (1 = no retry)")
    return value


def _argv(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AgentRuntimeError(f"{field_name} must be an array of arguments")
    argv = tuple(value)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise AgentRuntimeError(f"{field_name} must contain non-empty string arguments")
    return argv


_PROMPT_LANGUAGES = ("en", "zh")

_PROMPT_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "reviewer_intro": (
            "You are one of several independent reviewers running in parallel on "
            "different agents. Judge only from the material below; do not assume "
            "any other reviewer's verdict and do not optimize for agreement."
        ),
        "arbiter_intro": (
            "You are the arbiter. Summarize this round's reviewer results, then "
            "state your own verdict. Set disagrees_with_reviewers=true only when "
            "your judgment differs from the reviewers' conclusions."
        ),
        "meta_intro": (
            "You are the meta reviewer. Independently audit the arbiter's "
            "conclusion against the same context and the reviewer results below. "
            "Set agrees_with_arbiter=false when the arbiter's judgment is not "
            "supported; that requires at least one finding."
        ),
        "exact_values": (
            "Respond with EXACTLY ONE JSON object and nothing else: no markdown "
            "fences, no prose before or after. Use these exact values: "
        ),
        "shape_line": "Required JSON shape (fill the remaining fields):",
        "field_rules": (
            "Field rules: verdict is one of PASS, WARN, FAIL. findings must be a "
            "non-empty array when verdict is WARN or FAIL and [] when PASS. Each "
            'finding is {"finding_id": str, "issue": str, "evidence_refs": [str, '
            'at least one], "decision_impact": str, "remaining_uncertainty": str, '
            '"source_refs": [str], "status": "OPEN"}.'
        ),
        "context_header": "## ReviewContext (immutable, identical for every reviewer)",
        "dispute_header": "## DisputePacket (re-review the same context, no inherited verdict)",
        "reviewer_results_header": "## Reviewer results for this round",
        "arbiter_header": "## Arbiter result under audit",
    },
    "zh": {
        "reviewer_intro": (
            "你是多名独立评审之一,与其他评审并行运行在不同的 Agent 上。只依据下方"
            "材料独立判断;不要假设其他评审的结论,也不要为了与任何人保持一致而调整自己的判断。"
        ),
        "arbiter_intro": (
            "你是仲裁者(主 Agent)。请汇总本轮各评审结果,并给出你自己的裁决。仅当"
            "你的判断与评审结论不同时,才将 disagrees_with_reviewers 设为 true。"
        ),
        "meta_intro": (
            "你是元审核者。请基于同一上下文以及下方的评审结果,独立审查仲裁者的结论。"
            "当仲裁判断缺乏依据时,将 agrees_with_arbiter 设为 false;此时必须附至少"
            "一条 finding。"
        ),
        "exact_values": (
            "请只输出恰好一个 JSON 对象,前后不得有任何文字或 markdown 代码块。"
            "必须原样使用以下精确取值: "
        ),
        "shape_line": "必须输出的 JSON 形状(补全其余字段):",
        "field_rules": (
            "字段规则:verdict 取 PASS、WARN、FAIL 之一;当 verdict 为 WARN 或 FAIL"
            " 时,findings 必须是非空数组,PASS 时为 []。每个 finding 的结构为 "
            '{"finding_id": 字符串, "issue": 字符串, "evidence_refs": [字符串,至少'
            ' 一条], "decision_impact": 字符串, "remaining_uncertainty": 字符串, '
            '"source_refs": [字符串], "status": "OPEN"}。'
        ),
        "context_header": "## ReviewContext(不可变,全体评审完全一致)",
        "dispute_header": "## DisputePacket(同一上下文重审,不得继承任何先前裁决)",
        "reviewer_results_header": "## 本轮评审结果",
        "arbiter_header": "## 待审核的仲裁结论",
    },
}


def render_agent_prompt(request: AgentRequest, *, language: str = "en") -> str:
    """Render one AgentRequest as the platform-neutral review prompt."""

    if language not in _PROMPT_LANGUAGES:
        raise AgentRuntimeError(
            f"language must be one of: {', '.join(_PROMPT_LANGUAGES)}"
        )
    texts = _PROMPT_TEXTS[language]
    role: AgentRole = request.role
    context: ReviewContext = request.context
    fingerprint = context.fingerprint
    blocks: list[str] = []

    if role is AgentRole.REVIEWER:
        intro = texts["reviewer_intro"]
        schema = (
            '{"reviewer_id": "<use the exact agent_id below>", '
            '"round_number": <integer>, "verdict": "PASS|WARN|FAIL", '
            '"findings": [], "isolated": true, '
            '"context_fingerprint": "<use the exact fingerprint below>"}'
        )
    elif role is AgentRole.ARBITER:
        intro = texts["arbiter_intro"]
        schema = (
            '{"arbiter_id": "<use the exact agent_id below>", '
            '"round_number": <integer>, "verdict": "PASS|WARN|FAIL", '
            '"summary": "<one paragraph>", "rationale": "<why this verdict>", '
            '"reviewer_ids": ["<every reviewer_id below, exactly>"], '
            '"context_fingerprint": "<use the exact fingerprint below>", '
            '"disagrees_with_reviewers": false}'
        )
    else:
        intro = texts["meta_intro"]
        schema = (
            '{"reviewer_id": "<use the exact agent_id below>", '
            '"round_number": <integer>, '
            '"arbiter_id": "<use the exact arbiter_id below>", '
            '"verdict": "PASS|WARN|FAIL", "summary": "<one paragraph>", '
            '"findings": [], '
            '"context_fingerprint": "<use the exact fingerprint below>", '
            '"agrees_with_arbiter": true, "isolated": true}'
        )

    blocks.append(f"# GoalAnchor {role.value} review (round {request.round_number})")
    blocks.append(intro)
    blocks.append(texts["exact_values"] + (
        f'agent_id="{request.agent_id}", round_number={request.round_number}, '
        f'context_fingerprint="{fingerprint}".'
    ))
    blocks.append(texts["shape_line"])
    blocks.append(schema)
    blocks.append(texts["field_rules"])
    blocks.append(texts["context_header"])
    blocks.append(f"fingerprint: {fingerprint}")
    blocks.append("```json")
    blocks.append(
        json.dumps(
            {key: value for key, value in context.to_dict().items() if key != "fingerprint"},
            ensure_ascii=False,
            indent=2,
        )
    )
    blocks.append("```")
    if request.dispute_packet is not None:
        packet: DisputePacket = request.dispute_packet
        blocks.append(texts["dispute_header"])
        blocks.append(packet.re_review_prompt)
        blocks.append("```json")
        blocks.append(json.dumps(packet.to_dict(), ensure_ascii=False, indent=2))
        blocks.append("```")
    if request.reviewer_results:
        blocks.append(texts["reviewer_results_header"])
        blocks.append("```json")
        blocks.append(
            json.dumps(
                [item.to_dict() for item in request.reviewer_results],
                ensure_ascii=False,
                indent=2,
            )
        )
        blocks.append("```")
    if request.arbiter_result is not None:
        arbiter: ArbiterResult = request.arbiter_result
        blocks.append(texts["arbiter_header"])
        blocks.append(f'arbiter_id: {arbiter.arbiter_id}')
        blocks.append("```json")
        blocks.append(json.dumps(arbiter.to_dict(), ensure_ascii=False, indent=2))
        blocks.append("```")
    return "\n\n".join(blocks)


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


_TOKENS_USED_RE = re.compile(r"tokens\s+used\s*:?\s*\n?\s*([0-9][0-9,]*)", re.IGNORECASE)


def _extract_tokens_used(stderr: str) -> int | None:
    """Best-effort token usage from platform stderr (e.g. ``codex exec``)."""

    match = _TOKENS_USED_RE.search(stderr or "")
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's command template; ``{prompt}`` marks the prompt slot."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    retries: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "platform name"))
        argv = _argv(self.argv, f"platforms.{self.name}.argv")
        if argv.count("{prompt}") > 1:
            raise AgentRuntimeError(
                f"platforms.{self.name}.argv must contain at most one {{prompt}} placeholder"
            )
        object.__setattr__(self, "argv", argv)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise AgentRuntimeError(f"platforms.{self.name}.timeout_seconds must be a positive finite number")
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise AgentRuntimeError(f"platforms.{self.name}.env must map strings to strings")
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "retries", _retries(self.retries, f"platforms.{self.name}.retries"))


@dataclass(frozen=True)
class ProfileBinding:
    """One named agent profile bound to a platform CLI or a stdio bridge."""

    profile_id: str
    roles: tuple[AgentRole, ...]
    display_name: str
    platform: str | None = None
    command: tuple[str, ...] | None = None
    argv: tuple[str, ...] | None = None
    timeout_seconds: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    enabled: bool = True
    retries: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _text(self.profile_id, "profile_id"))
        object.__setattr__(self, "display_name", _text(self.display_name, "display_name"))
        object.__setattr__(self, "roles", _roles(self.roles, "roles"))
        if self.platform is not None:
            object.__setattr__(self, "platform", _text(self.platform, "platform"))
        if self.command is not None:
            object.__setattr__(self, "command", _argv(self.command, "command"))
        if self.argv is not None:
            argv = _argv(self.argv, "argv")
            if argv.count("{prompt}") > 1:
                raise AgentRuntimeError("argv must contain at most one {prompt} placeholder")
            object.__setattr__(self, "argv", argv)
        if (self.platform is None) == (self.command is None):
            raise AgentRuntimeError(
                f"profile {self.profile_id} needs exactly one of 'platform' or 'command'"
            )
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise AgentRuntimeError("timeout_seconds must be a positive finite number")
        if self.retries is not None:
            object.__setattr__(self, "retries", _retries(self.retries, "retries"))
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise AgentRuntimeError("env must map strings to strings")
        object.__setattr__(self, "env", dict(self.env))
        if any(not isinstance(item, str) or not item.strip() for item in self.capabilities):
            raise AgentRuntimeError("capabilities must contain non-empty strings")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if not isinstance(self.enabled, bool):
            raise AgentRuntimeError("enabled must be a boolean")

    @classmethod
    def from_dict(cls, profile_id: str, payload: Mapping[str, object]) -> "ProfileBinding":
        if not isinstance(payload, Mapping):
            raise AgentRuntimeError(f"profile {profile_id} configuration must be an object")
        return cls(
            profile_id=profile_id,
            roles=tuple(payload.get("roles", ())),
            display_name=payload.get("display_name") or profile_id,
            platform=payload.get("platform"),
            command=payload.get("command"),
            argv=payload.get("argv"),
            timeout_seconds=payload.get("timeout_seconds"),
            env=payload.get("env", {}),
            capabilities=tuple(payload.get("capabilities", ())),
            enabled=payload.get("enabled", True),
            retries=payload.get("retries"),
        )

    def agent_profile(self) -> AgentProfile:
        return AgentProfile(
            profile_id=self.profile_id,
            display_name=self.display_name,
            roles=self.roles,
            capabilities=self.capabilities,
            enabled=self.enabled,
        )


class PlatformDispatchConfig:
    """Validated dispatch configuration: platforms, profiles, role bindings."""

    def __init__(
        self,
        profiles: Sequence[ProfileBinding],
        platforms: Mapping[str, PlatformSpec],
        default_platform: str | None,
        role_bindings: Mapping[str, str],
        capabilities: AgentRuntimeCapabilities | None = None,
        allow_profile_reuse: bool = False,
        language: str = "en",
    ) -> None:
        profile_list = tuple(profiles)
        if not profile_list:
            raise AgentRuntimeError("at least one agent profile is required")
        if len({item.profile_id for item in profile_list}) != len(profile_list):
            raise AgentRuntimeError("agent profile IDs must be unique")
        object.__setattr__(self, "profiles", profile_list)
        normalized: dict[str, PlatformSpec] = {}
        for name, spec in platforms.items():
            if not isinstance(spec, PlatformSpec):
                raise AgentRuntimeError("platforms must contain PlatformSpec values")
            normalized[name] = spec
        object.__setattr__(self, "platforms", normalized)
        if default_platform is not None:
            default_platform = _text(default_platform, "platform")
            if default_platform not in normalized:
                raise AgentRuntimeError(f"unknown platform: {default_platform}")
        object.__setattr__(self, "default_platform", default_platform)
        by_id = {item.profile_id for item in profile_list}
        bindings: dict[str, str] = {}
        for role, profile_id in (role_bindings or {}).items():
            key = role.value if isinstance(role, AgentRole) else _text(role, "role_bindings key")
            target = _text(profile_id, f"role_bindings.{key}")
            if target not in by_id:
                raise AgentRuntimeError(
                    f"role_bindings.{key} points to unknown profile: {target}"
                )
            bindings[key] = target
        object.__setattr__(self, "role_bindings", bindings)
        caps = capabilities or AgentRuntimeCapabilities()
        if not isinstance(caps, AgentRuntimeCapabilities):
            raise AgentRuntimeError("capabilities must be AgentRuntimeCapabilities")
        object.__setattr__(self, "capabilities", caps)
        if not isinstance(allow_profile_reuse, bool):
            raise AgentRuntimeError("allow_profile_reuse must be a boolean")
        object.__setattr__(self, "allow_profile_reuse", allow_profile_reuse)
        if language not in _PROMPT_LANGUAGES:
            raise AgentRuntimeError(
                f"language must be one of: {', '.join(_PROMPT_LANGUAGES)}"
            )
        object.__setattr__(self, "language", language)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PlatformDispatchConfig":
        if not isinstance(payload, Mapping):
            raise AgentRuntimeError("dispatch configuration must be a JSON object")
        raw_platforms = payload.get("platforms") or {}
        if not isinstance(raw_platforms, Mapping):
            raise AgentRuntimeError("platforms must be an object")
        platforms: dict[str, PlatformSpec] = {}
        for name, spec in raw_platforms.items():
            if not isinstance(spec, Mapping):
                raise AgentRuntimeError(f"platforms.{name} must be an object")
            argv = spec.get("argv")
            if argv is None:
                if name in _BUILTIN_ARGV:
                    argv = _BUILTIN_ARGV[name]
                else:
                    raise AgentRuntimeError(
                        f"platforms.{name}.argv is required for non-builtin platforms"
                    )
            platforms[name] = PlatformSpec(
                name=name,
                argv=tuple(argv),
                timeout_seconds=spec.get("timeout_seconds", 600.0),
                env=spec.get("env", {}),
                retries=spec.get("retries", 1),
            )
        for name in BUILTIN_PLATFORMS:
            if name not in platforms:
                platforms[name] = PlatformSpec(name=name, argv=_BUILTIN_ARGV[name])
        default_platform = payload.get("platform")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, Mapping) or not raw_profiles:
            raise AgentRuntimeError("profiles must be a non-empty object")
        profiles = [
            ProfileBinding.from_dict(profile_id, spec)
            for profile_id, spec in raw_profiles.items()
        ]
        for profile in profiles:
            if profile.platform is not None and profile.platform not in platforms:
                raise AgentRuntimeError(
                    f"profile {profile.profile_id} uses unknown platform: {profile.platform}"
                )
        return cls(
            profiles=profiles,
            platforms=platforms,
            default_platform=default_platform,
            role_bindings=payload.get("role_bindings", {}),
            capabilities=(
                AgentRuntimeCapabilities(**payload["capabilities"])
                if isinstance(payload.get("capabilities"), Mapping)
                else None
            ),
            allow_profile_reuse=payload.get("allow_profile_reuse", False),
            language=payload.get("language", "en"),
        )

    def binding(self, profile_id: str) -> ProfileBinding:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                if not profile.enabled:
                    raise AgentRuntimeError(f"agent profile is disabled: {profile_id}")
                return profile
        raise AgentRuntimeError(f"agent profile not found: {profile_id}")

    def resolve_platform(self, profile: ProfileBinding) -> PlatformSpec:
        platform = profile.platform or self.default_platform
        if platform is None:
            raise AgentRuntimeError(
                f"profile {profile.profile_id} has no platform and no default platform is set"
            )
        spec = self.platforms.get(platform)
        if spec is None:
            raise AgentRuntimeError(f"unknown platform: {platform}")
        overrides_timeout = profile.timeout_seconds is not None
        overrides_env = bool(profile.env)
        overrides_retries = profile.retries is not None
        if profile.argv is not None or overrides_timeout or overrides_env or overrides_retries:
            return PlatformSpec(
                name=platform,
                argv=profile.argv if profile.argv is not None else spec.argv,
                timeout_seconds=(
                    profile.timeout_seconds
                    if profile.timeout_seconds is not None
                    else spec.timeout_seconds
                ),
                env=profile.env if profile.env else spec.env,
                retries=profile.retries if profile.retries is not None else spec.retries,
            )
        return spec


class PlatformAgentRouter:
    """Dispatch named profiles to platform CLIs or stdio bridges."""

    def __init__(self, config: PlatformDispatchConfig) -> None:
        self._config = config
        self._usage: list[dict[str, object]] = []
        self._lock = Lock()

    def list_agents(self) -> tuple[AgentProfile, ...]:
        return tuple(profile.agent_profile() for profile in self._config.profiles)

    def capabilities(self) -> AgentRuntimeCapabilities:
        return self._config.capabilities

    def dispatch(self, profile_id: str, request: AgentRequest) -> Mapping[str, object]:
        binding = self._config.binding(profile_id)
        if binding.command is not None:
            argv = binding.command
            input_text: str | None = json.dumps(request.to_dict(), ensure_ascii=False) + "\n"
            timeout_seconds = binding.timeout_seconds or 300.0
            env = binding.env
            attempts = binding.retries or 1
            platform_name = "stdio-bridge"
        else:
            spec = self._config.resolve_platform(binding)
            prompt = render_agent_prompt(request, language=self._config.language)
            if "{prompt}" in spec.argv:
                argv = _materialize_argv(spec.argv, prompt)
                input_text = None
            else:
                argv = spec.argv
                input_text = prompt
            timeout_seconds = spec.timeout_seconds
            env = spec.env
            attempts = spec.retries
            platform_name = spec.name
        stdout: str | None = None
        stderr = ""
        for attempt in range(1, attempts + 1):
            try:
                stdout, stderr = _run_subprocess(
                    argv,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                    env=env,
                )
                break
            except AgentRuntimeError as exc:
                if attempt == attempts:
                    raise
        payload = extract_result_payload(stdout or "")
        usage: dict[str, object] = {
            "agent_id": request.agent_id,
            "role": request.role.value,
            "round_number": request.round_number,
            "profile_id": binding.profile_id,
            "platform": platform_name,
            "attempts": attempt,
        }
        tokens_used = _extract_tokens_used(stderr)
        if tokens_used is not None:
            usage["tokens_used"] = tokens_used
        with self._lock:
            self._usage.append(usage)
        return payload

    def usage_report(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._usage)


class PlatformAgentAdapter:
    """Catalog-routed adapter that also reports per-call platform usage."""

    def __init__(self, config: PlatformDispatchConfig) -> None:
        self._router = PlatformAgentRouter(config)
        self._catalog = CatalogAgentAdapter(
            self._router,
            config.role_bindings,
            allow_profile_reuse=config.allow_profile_reuse,
        )

    def invoke(self, request: AgentRequest) -> Mapping[str, object]:
        return self._catalog.invoke(request)

    def capabilities(self) -> AgentRuntimeCapabilities:
        return self._catalog.capabilities()

    def usage_report(self) -> tuple[dict[str, object], ...]:
        return self._router.usage_report()


def build_platform_adapter(config: PlatformDispatchConfig) -> PlatformAgentAdapter:
    """Route the review runtime through validated platform profiles."""

    return PlatformAgentAdapter(config)


def adapter_from_config(payload: Mapping[str, object]):
    """Load either a platform dispatch config or a legacy stdio bridge config."""

    if not isinstance(payload, Mapping):
        raise AgentRuntimeError("agent configuration must be a JSON object")
    profiles = payload.get("profiles")
    platform_shaped = (
        "platforms" in payload or "platform" in payload or "argv" in payload
    )
    if not platform_shaped and isinstance(profiles, Mapping):
        platform_shaped = any(
            isinstance(spec, Mapping) and "platform" in spec and "command" not in spec
            for spec in profiles.values()
        )
    if platform_shaped:
        return build_platform_adapter(PlatformDispatchConfig.from_dict(payload))
    return JsonStdioAgentAdapter.from_dict(payload)


def probe_platform(name: str, timeout_seconds: float = 120.0) -> dict[str, object]:
    """Run one minimal invocation against a built-in platform CLI.

    Verifies that the executable resolves, is authenticated, and its output
    can be parsed. Costs one tiny model call on the probed platform.
    """

    if name not in _BUILTIN_ARGV:
        raise AgentRuntimeError(f"unknown platform: {name}")
    spec = PlatformSpec(name=name, argv=_BUILTIN_ARGV[name], timeout_seconds=timeout_seconds)
    prompt = 'Reply with exactly this JSON object and nothing else: {"probe": "ok"}'
    if "{prompt}" in spec.argv:
        argv = _materialize_argv(spec.argv, prompt)
        input_text = None
    else:
        argv = spec.argv
        input_text = prompt
    try:
        stdout, _stderr = _run_subprocess(
            argv,
            input_text=input_text,
            timeout_seconds=spec.timeout_seconds,
            env=spec.env,
        )
        payload = extract_result_payload(stdout)
    except AgentRuntimeError as exc:
        return {"platform": name, "probe": "failed", "detail": str(exc)[-300:]}
    if isinstance(payload, Mapping) and payload.get("probe") == "ok":
        return {"platform": name, "probe": "ok"}
    return {
        "platform": name,
        "probe": "unexpected-shape",
        "detail": json.dumps(payload, ensure_ascii=False)[:200],
    }
