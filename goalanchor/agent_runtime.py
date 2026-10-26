"""Platform-neutral Agent execution through a strict JSON-over-stdio contract.

The runtime never imports or calls a host-specific Agent API.  A host bridge
receives one JSON request on stdin and must emit exactly one JSON object on
stdout.  This keeps the same orchestration usable from Codex, Claude Code,
DSH, Zcode, or any other host that can expose such a bridge command.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from threading import Lock
from typing import Any, Protocol, TypeVar

from .core import ContractValidationError
from .review_service import (
    ArbiterResult,
    DisputePacket,
    MetaReviewResult,
    ReviewContext,
    ReviewerResult,
    ReviewRisk,
    ReviewServiceResult,
    run_review_cycle,
)

_ResultT = TypeVar("_ResultT")
_EnumT = TypeVar("_EnumT", bound=Enum)


class AgentRuntimeError(ContractValidationError):
    """A bridge could not satisfy the portable Agent protocol."""


def _parse_result(
    agent_id: str,
    parse: Callable[[Mapping[str, Any]], _ResultT],
    payload: Any,
    *,
    allowed_fields: frozenset[str] | None = None,
) -> _ResultT:
    """Parse one adapter payload, mapping schema mismatches to a clean error."""

    try:
        if isinstance(payload, Mapping) and allowed_fields is not None:
            payload = {key: value for key, value in payload.items() if key in allowed_fields}
        return parse(payload)
    except (TypeError, ValueError) as exc:
        snippet = json.dumps(payload, ensure_ascii=False)[:200] if isinstance(payload, Mapping) else repr(payload)[:200]
        raise AgentRuntimeError(
            f"agent {agent_id} returned an invalid result object: {exc} (payload: {snippet})"
        ) from exc


class AgentRole(str, Enum):
    REVIEWER = "reviewer"
    ARBITER = "arbiter"
    META_REVIEWER = "meta_reviewer"


@dataclass(frozen=True)
class AgentRuntimeCapabilities:
    supports_parallel: bool = True
    max_concurrency: int = 4
    supports_named_profiles: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.supports_parallel, bool):
            raise AgentRuntimeError("supports_parallel must be a boolean")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise AgentRuntimeError("max_concurrency must be a positive integer")
        if not isinstance(self.supports_named_profiles, bool):
            raise AgentRuntimeError("supports_named_profiles must be a boolean")


@dataclass(frozen=True)
class AgentProfile:
    """A named Agent exposed by a host, such as a UI entry named ``Euclid``."""

    profile_id: str
    display_name: str
    roles: tuple[AgentRole, ...]
    capabilities: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "display_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AgentRuntimeError(f"{field_name} must be a non-empty string")
        roles = tuple(_enum(item, AgentRole, "roles[]") for item in self.roles)
        if not roles or len(roles) != len(set(roles)):
            raise AgentRuntimeError("roles must contain unique AgentRole values")
        object.__setattr__(self, "roles", roles)
        capabilities = tuple(self.capabilities)
        if any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise AgentRuntimeError("capabilities must contain non-empty strings")
        object.__setattr__(self, "capabilities", capabilities)
        if not isinstance(self.enabled, bool):
            raise AgentRuntimeError("enabled must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "roles": [item.value for item in self.roles],
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentProfile:
        return cls(
            profile_id=payload["profile_id"],
            display_name=payload["display_name"],
            roles=tuple(payload["roles"]),
            capabilities=tuple(payload.get("capabilities", ())),
            enabled=payload.get("enabled", True),
        )


def _enum(value: Any, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeError(f"{field_name} is invalid") from exc


@dataclass(frozen=True)
class AgentRequest:
    """One immutable request sent to an external reviewer role."""

    role: AgentRole
    agent_id: str
    round_number: int
    risk: ReviewRisk
    context: ReviewContext
    dispute_packet: DisputePacket | None = None
    reviewer_results: tuple[ReviewerResult, ...] = ()
    arbiter_result: ArbiterResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, AgentRole, "role"))
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise AgentRuntimeError("agent_id must be a non-empty string")
        if isinstance(self.round_number, bool) or not isinstance(self.round_number, int):
            raise AgentRuntimeError("round_number must be an integer")
        if self.round_number < 1:
            raise AgentRuntimeError("round_number must be at least 1")
        object.__setattr__(self, "risk", _enum(self.risk, ReviewRisk, "risk"))
        if not isinstance(self.context, ReviewContext):
            raise AgentRuntimeError("context must be a ReviewContext")
        if self.dispute_packet is not None and not isinstance(self.dispute_packet, DisputePacket):
            raise AgentRuntimeError("dispute_packet must be a DisputePacket")
        reviewer_results = tuple(self.reviewer_results)
        if any(not isinstance(item, ReviewerResult) for item in reviewer_results):
            raise AgentRuntimeError("reviewer_results must contain ReviewerResult values")
        object.__setattr__(self, "reviewer_results", reviewer_results)
        if self.arbiter_result is not None and not isinstance(self.arbiter_result, ArbiterResult):
            raise AgentRuntimeError("arbiter_result must be an ArbiterResult")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "goalanchor.agent.v1",
            "role": self.role.value,
            "agent_id": self.agent_id,
            "round_number": self.round_number,
            "risk": self.risk.value,
            "context": self.context.to_dict(),
            "dispute_packet": (
                None if self.dispute_packet is None else self.dispute_packet.to_dict()
            ),
            "reviewer_results": [item.to_dict() for item in self.reviewer_results],
            "arbiter_result": (
                None if self.arbiter_result is None else self.arbiter_result.to_dict()
            ),
        }


class AgentAdapter(Protocol):
    """Minimal host-neutral contract implemented by any Agent bridge."""

    def invoke(self, request: AgentRequest) -> Mapping[str, Any]:
        ...


class AgentDispatcher(Protocol):
    """Host-neutral catalog and dispatch contract for named subagents."""

    def list_agents(self) -> Sequence[AgentProfile]:
        ...

    def capabilities(self) -> AgentRuntimeCapabilities:
        ...

    def dispatch(self, profile_id: str, request: AgentRequest) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class AgentCatalog:
    """Select enabled named profiles by logical review role."""

    agents: tuple[AgentProfile, ...]

    def __post_init__(self) -> None:
        agents = tuple(self.agents)
        if any(not isinstance(item, AgentProfile) for item in agents):
            raise AgentRuntimeError("agents must contain AgentProfile values")
        if len({item.profile_id for item in agents}) != len(agents):
            raise AgentRuntimeError("Agent profile IDs must be unique")
        object.__setattr__(self, "agents", agents)

    def select(
        self,
        role: AgentRole,
        preferred_profile_id: str | None = None,
        excluded_profile_ids: Sequence[str] = (),
    ) -> AgentProfile:
        excluded = set(excluded_profile_ids)
        if preferred_profile_id is not None:
            for agent in self.agents:
                if agent.profile_id == preferred_profile_id:
                    if not agent.enabled:
                        raise AgentRuntimeError(f"Agent profile is disabled: {preferred_profile_id}")
                    if role not in agent.roles:
                        raise AgentRuntimeError(
                            f"Agent profile {preferred_profile_id} does not support role {role.value}"
                        )
                    if agent.profile_id in excluded:
                        raise AgentRuntimeError(
                            f"Agent profile is already assigned: {preferred_profile_id}"
                        )
                    return agent
            raise AgentRuntimeError(f"Agent profile not found: {preferred_profile_id}")
        for agent in self.agents:
            if agent.enabled and role in agent.roles and agent.profile_id not in excluded:
                return agent
        raise AgentRuntimeError(f"no enabled Agent profile supports role {role.value}")


class CatalogAgentAdapter:
    """Adapt a host's named Agent dispatcher to the review runtime."""

    def __init__(
        self,
        dispatcher: AgentDispatcher,
        role_bindings: Mapping[AgentRole | str, str] | None = None,
        *,
        allow_profile_reuse: bool = False,
    ) -> None:
        if not hasattr(dispatcher, "list_agents") or not hasattr(dispatcher, "dispatch"):
            raise AgentRuntimeError("dispatcher must provide list_agents() and dispatch()")
        bindings: dict[str, str] = {}
        for role, profile_id in (role_bindings or {}).items():
            if isinstance(role, AgentRole):
                binding_key = role.value
            elif isinstance(role, str) and role in {item.value for item in AgentRole}:
                binding_key = role
            elif isinstance(role, str) and role.strip():
                binding_key = role
            else:
                raise AgentRuntimeError("role_bindings keys must be role or agent IDs")
            if not isinstance(profile_id, str) or not profile_id.strip():
                raise AgentRuntimeError("role_bindings values must be non-empty strings")
            bindings[binding_key] = profile_id
        self._dispatcher = dispatcher
        self._catalog = AgentCatalog(tuple(dispatcher.list_agents()))
        self._bindings = bindings
        self._allow_profile_reuse = allow_profile_reuse
        self._assignments: dict[str, str] = {}
        self._lock = Lock()
        self._capabilities = (
            dispatcher.capabilities()
            if hasattr(dispatcher, "capabilities")
            else AgentRuntimeCapabilities()
        )
        if not isinstance(self._capabilities, AgentRuntimeCapabilities):
            raise AgentRuntimeError("dispatcher capabilities() must return AgentRuntimeCapabilities")

    def invoke(self, request: AgentRequest) -> Mapping[str, Any]:
        with self._lock:
            assigned = self._assignments.get(request.agent_id)
            preferred = assigned or self._bindings.get(request.agent_id) or self._bindings.get(request.role.value)
            excluded: tuple[str, ...] = ()
            if not self._allow_profile_reuse:
                excluded = tuple(
                    profile_id
                    for agent_id, profile_id in self._assignments.items()
                    if agent_id != request.agent_id
                )
            profile = self._catalog.select(request.role, preferred, excluded)
            self._assignments[request.agent_id] = profile.profile_id
        return self._dispatcher.dispatch(profile.profile_id, request)

    def capabilities(self) -> AgentRuntimeCapabilities:
        return self._capabilities


@dataclass(frozen=True)
class AgentCommand:
    """Safe subprocess configuration; commands are argv arrays, never shell text."""

    command: tuple[str, ...]
    timeout_seconds: float = 300.0
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.command, (str, bytes)) or not isinstance(self.command, Sequence):
            raise AgentRuntimeError("command must be an array of arguments")
        command = tuple(self.command)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise AgentRuntimeError("command must contain non-empty string arguments")
        object.__setattr__(self, "command", command)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise AgentRuntimeError("timeout_seconds must be a positive finite number")
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise AgentRuntimeError("env must be an object of string values")
        object.__setattr__(self, "env", dict(self.env))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentCommand:
        if not isinstance(payload, Mapping):
            raise AgentRuntimeError("Agent command configuration must be an object")
        return cls(
            command=tuple(payload.get("command", ())),
            timeout_seconds=payload.get("timeout_seconds", 300.0),
            env=payload.get("env", {}),
        )


class JsonStdioAgentAdapter:
    """Invoke a platform bridge using one JSON request per process."""

    def __init__(
        self,
        commands: Mapping[AgentRole | str, AgentCommand],
        *,
        profiles: Sequence[AgentProfile] = (),
        role_bindings: Mapping[AgentRole | str, str] | None = None,
        capabilities: AgentRuntimeCapabilities | None = None,
        allow_profile_reuse: bool = False,
    ):
        normalized: dict[AgentRole | str, AgentCommand] = {}
        for role, command in commands.items():
            key: AgentRole | str
            if role == "default":
                key = "default"
            else:
                try:
                    key = _enum(role, AgentRole, "commands role")
                except AgentRuntimeError as exc:
                    if not isinstance(role, str) or not role.strip():
                        raise AgentRuntimeError(
                            "profile_id must be a non-empty string"
                        ) from exc
                    key = role
            if not isinstance(command, AgentCommand):
                raise AgentRuntimeError("commands must contain AgentCommand values")
            normalized[key] = command
        if not normalized:
            raise AgentRuntimeError("at least one Agent command is required")
        self._commands = normalized
        self._profiles = tuple(profiles)
        self._catalog = AgentCatalog(self._profiles) if self._profiles else None
        bindings: dict[str, str] = {}
        for role, profile_id in (role_bindings or {}).items():
            if isinstance(role, AgentRole):
                binding_key = role.value
            elif isinstance(role, str) and role in {item.value for item in AgentRole}:
                binding_key = role
            elif isinstance(role, str) and role.strip():
                binding_key = role
            else:
                raise AgentRuntimeError("role_bindings keys must be role or agent IDs")
            bindings[binding_key] = profile_id
        self._role_bindings = bindings
        self._capabilities = capabilities or AgentRuntimeCapabilities()
        self._allow_profile_reuse = allow_profile_reuse
        self._assignments: dict[str, str] = {}
        self._lock = Lock()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> JsonStdioAgentAdapter:
        if not isinstance(payload, Mapping):
            raise AgentRuntimeError("Agent adapter configuration must be an object")
        raw_profiles = payload.get("profiles")
        if raw_profiles is not None:
            if not isinstance(raw_profiles, Mapping):
                raise AgentRuntimeError("profiles must be an object")
            profiles: list[AgentProfile] = []
            commands: dict[str, AgentCommand] = {}
            for profile_id, spec in raw_profiles.items():
                if not isinstance(spec, Mapping):
                    raise AgentRuntimeError("Agent profile configuration must be an object")
                profile_data = dict(spec)
                profile_data.setdefault("profile_id", profile_id)
                profile_data.setdefault("display_name", profile_id)
                profiles.append(AgentProfile.from_dict(profile_data))
                commands[profile_id] = AgentCommand.from_dict(spec)
            return cls(
                commands,
                profiles=profiles,
                role_bindings=payload.get("role_bindings", {}),
                capabilities=AgentRuntimeCapabilities(**payload.get("capabilities", {})),
                allow_profile_reuse=payload.get("allow_profile_reuse", False),
            )
        raw_commands = payload.get("commands")
        if raw_commands is None and "command" in payload:
            raw_commands = {"default": payload}
        if not isinstance(raw_commands, Mapping):
            raise AgentRuntimeError("configuration requires a commands object")
        return cls({role: AgentCommand.from_dict(spec) for role, spec in raw_commands.items()})

    def list_agents(self) -> tuple[AgentProfile, ...]:
        if self._profiles:
            return self._profiles
        profiles: list[AgentProfile] = []
        for key in self._commands:
            if key == "default":
                continue
            role = key if isinstance(key, AgentRole) else AgentRole(key)
            profiles.append(AgentProfile(role.value, role.value.replace("_", " ").title(), (role,)))
        return tuple(profiles)

    def _command_for(self, role: AgentRole) -> AgentCommand:
        if self._catalog is not None:
            profile = self._catalog.select(role, self._role_bindings.get(role.value))
            return self._commands[profile.profile_id]
        return self._commands.get(role) or self._commands.get("default") or self._missing(role)

    def _command_for_request(self, request: AgentRequest) -> AgentCommand:
        if self._catalog is None:
            return self._command_for(request.role)
        with self._lock:
            assigned = self._assignments.get(request.agent_id)
            preferred = (
                assigned
                or self._role_bindings.get(request.agent_id)
                or self._role_bindings.get(request.role.value)
            )
            excluded: tuple[str, ...] = ()
            if not self._allow_profile_reuse:
                excluded = tuple(
                    profile_id
                    for agent_id, profile_id in self._assignments.items()
                    if agent_id != request.agent_id
                )
            profile = self._catalog.select(request.role, preferred, excluded)
            self._assignments[request.agent_id] = profile.profile_id
            return self._commands[profile.profile_id]

    @staticmethod
    def _missing(role: AgentRole) -> AgentCommand:
        raise AgentRuntimeError(f"no Agent command configured for role: {role.value}")

    def dispatch(self, profile_id: str, request: AgentRequest) -> Mapping[str, Any]:
        command = self._commands.get(profile_id)
        if command is None:
            try:
                command = self._commands.get(AgentRole(profile_id))
            except ValueError:
                command = None
        if command is None:
            raise AgentRuntimeError(f"Agent profile not found: {profile_id}")
        return self._invoke_command(command, request)

    def invoke(self, request: AgentRequest) -> Mapping[str, Any]:
        command = self._command_for_request(request)
        return self._invoke_command(command, request)

    def capabilities(self) -> AgentRuntimeCapabilities:
        return self._capabilities

    @staticmethod
    def _invoke_command(command: AgentCommand, request: AgentRequest) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                command.command,
                input=json.dumps(request.to_dict(), ensure_ascii=False) + "\n",
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    **command.env,
                },
                shell=False,
                timeout=command.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentRuntimeError(
                f"Agent bridge failed for {request.role.value}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-2000:]
            raise AgentRuntimeError(
                f"Agent bridge exited with code {completed.returncode}"
                + (f": {detail}" if detail else "")
            )
        output = completed.stdout.strip()
        if not output:
            raise AgentRuntimeError("Agent bridge returned empty stdout")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AgentRuntimeError("Agent bridge stdout is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise AgentRuntimeError("Agent bridge must return one JSON object")
        return payload


def run_review_cycle_with_adapter(
    context: ReviewContext,
    risk: ReviewRisk,
    adapter: AgentAdapter,
    *,
    max_rounds: int = 2,
    max_workers: int | None = None,
) -> ReviewServiceResult:
    """Run a bounded review cycle through any conforming Agent adapter.

    Reviewer calls in one round run concurrently.  Arbiter and Meta Reviewer
    calls remain ordered, and the existing review policy remains authoritative.
    """

    # Duck typing keeps simple test and host bridges usable without requiring
    # runtime Protocol checks.
    if not hasattr(adapter, "invoke"):
        raise AgentRuntimeError("adapter must provide invoke(request)")
    if max_workers is not None and (
        isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1
    ):
        raise AgentRuntimeError("max_workers must be a positive integer")

    def reviewer_call(
        review_context: ReviewContext,
        reviewer_id: str,
        round_number: int,
        dispute: DisputePacket | None,
    ) -> ReviewerResult:
        request = AgentRequest(
            role=AgentRole.REVIEWER,
            agent_id=reviewer_id,
            round_number=round_number,
            risk=risk,
            context=review_context,
            dispute_packet=dispute,
        )
        return _parse_result(
            reviewer_id,
            ReviewerResult.from_dict,
            adapter.invoke(request),
            allowed_fields=frozenset(field.name for field in fields(ReviewerResult)),
        )

    def reviewer_batch_call(
        review_context: ReviewContext,
        reviewer_ids: tuple[str, ...],
        round_number: int,
        dispute: DisputePacket | None,
    ) -> tuple[ReviewerResult, ...]:
        workers = max_workers or max(1, len(reviewer_ids))
        with ThreadPoolExecutor(max_workers=min(workers, len(reviewer_ids))) as executor:
            futures = {
                reviewer_id: executor.submit(
                    reviewer_call, review_context, reviewer_id, round_number, dispute
                )
                for reviewer_id in reviewer_ids
            }
            return tuple(futures[reviewer_id].result() for reviewer_id in reviewer_ids)

    def arbiter_call(
        review_context: ReviewContext,
        results: tuple[ReviewerResult, ...],
        round_number: int,
        dispute: DisputePacket | None,
    ) -> ArbiterResult:
        request = AgentRequest(
            role=AgentRole.ARBITER,
            agent_id="arbiter",
            round_number=round_number,
            risk=risk,
            context=review_context,
            dispute_packet=dispute,
            reviewer_results=results,
        )
        return _parse_result(
            "arbiter",
            ArbiterResult.from_dict,
            adapter.invoke(request),
            allowed_fields=frozenset(field.name for field in fields(ArbiterResult)),
        )

    def meta_review_call(
        review_context: ReviewContext,
        reviewer_results: tuple[ReviewerResult, ...],
        arbiter: ArbiterResult,
        round_number: int,
    ) -> MetaReviewResult:
        request = AgentRequest(
            role=AgentRole.META_REVIEWER,
            agent_id="meta-reviewer",
            round_number=round_number,
            risk=risk,
            context=review_context,
            arbiter_result=arbiter,
            reviewer_results=reviewer_results,
        )
        return _parse_result(
            "meta-reviewer",
            MetaReviewResult.from_dict,
            adapter.invoke(request),
            allowed_fields=frozenset(field.name for field in fields(MetaReviewResult)),
        )

    result = run_review_cycle(
        context,
        risk,
        reviewer_call,
        arbiter_call,
        meta_review_call,
        max_rounds=max_rounds,
        reviewer_batch_call=reviewer_batch_call,
    )
    usage_report = getattr(adapter, "usage_report", None)
    if callable(usage_report):
        result = replace(result, agent_bindings=tuple(dict(item) for item in usage_report()))
    return result


def run_review_cycle_with_dispatcher(
    context: ReviewContext,
    risk: ReviewRisk,
    dispatcher: AgentDispatcher,
    *,
    role_bindings: Mapping[AgentRole | str, str] | None = None,
    allow_profile_reuse: bool = False,
    max_rounds: int = 2,
    max_workers: int | None = None,
) -> ReviewServiceResult:
    """Run review using named host-provided Agent profiles."""

    return run_review_cycle_with_adapter(
        context,
        risk,
        CatalogAgentAdapter(dispatcher, role_bindings, allow_profile_reuse=allow_profile_reuse),
        max_rounds=max_rounds,
        max_workers=(
            max_workers
            if max_workers is not None
            else (
                dispatcher.capabilities().max_concurrency
                if dispatcher.capabilities().supports_parallel
                else 1
            )
        ),
    )
__all__ = [
    "AgentAdapter",
    "AgentCatalog",
    "AgentCommand",
    "AgentDispatcher",
    "AgentProfile",
    "AgentRequest",
    "AgentRole",
    "AgentRuntimeCapabilities",
    "AgentRuntimeError",
    "CatalogAgentAdapter",
    "JsonStdioAgentAdapter",
    "run_review_cycle_with_dispatcher",
    "run_review_cycle_with_adapter",
]
