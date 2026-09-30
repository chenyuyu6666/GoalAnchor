"""Configuration model, adapter construction, and platform probing.

Owns the validated config (platforms, profiles, role bindings, model
consistency) and the two entry points around it: building an
:class:`~goalanchor.agent_runtime.AgentAdapter` from a config, and probing a
built-in platform. Config resolution caches each profile's argv so dispatch
reuses the exact values validated here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock

from ..agent_runtime import (
    AgentProfile,
    AgentRole,
    AgentRuntimeCapabilities,
    AgentRuntimeError,
    CatalogAgentAdapter,
    JsonStdioAgentAdapter,
)
from .platform_common import (
    _BUILTIN_ARGV,
    _BUILTIN_MODEL_FLAGS,
    BUILTIN_PLATFORMS,
    _model,
    _retries,
    _roles,
    _text,
)
from .platform_prompt import (
    _PROMPT_LANGUAGES,
    _apply_model,
    _argv,
    render_agent_prompt,
)


def _parse():
    """Parse functions via the package facade, resolved at call time.

    Lets tests that monkeypatch ``goalanchor.platforms._run_subprocess``
    take effect instead of binding the original implementation at import.
    """

    import goalanchor.platforms as facade

    return facade


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's command template; ``{prompt}`` marks the prompt slot."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    retries: int = 1
    model: str | None = None
    model_flag: str | None = None

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
            raise AgentRuntimeError(
                f"platforms.{self.name}.timeout_seconds must be a positive finite number"
            )
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise AgentRuntimeError(f"platforms.{self.name}.env must map strings to strings")
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "retries", _retries(self.retries, f"platforms.{self.name}.retries"))
        object.__setattr__(self, "model", _model(self.model, f"platforms.{self.name}.model"))
        object.__setattr__(
            self, "model_flag", _model(self.model_flag, f"platforms.{self.name}.model_flag")
        )


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
    model: str | None = None

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
        object.__setattr__(self, "model", _model(self.model, f"profiles.{self.profile_id}.model"))
        if self.command is not None and self.model is not None:
            raise AgentRuntimeError(
                f"profile {self.profile_id} binds a stdio bridge command; the bridge "
                "protocol carries no model field, so a declared 'model' could never be "
                "honored. Remove it, or bind the profile to a platform instead."
            )
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
    def from_dict(cls, profile_id: str, payload: Mapping[str, object]) -> ProfileBinding:
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
            model=payload.get("model"),
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
        model: str | None = None,
        model_divergence: Mapping[str, object] | None = None,
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
        # Model consistency: sub-agents must run on the main agent's current
        # model. ``model`` is that model; any profile that pins a different
        # one is rejected at config load — before any agent is launched —
        # unless the user explicitly approved the divergence.
        object.__setattr__(self, "model", _model(model, "model"))
        divergence: dict[str, str] | None = None
        if model_divergence is not None:
            if not isinstance(model_divergence, Mapping):
                raise AgentRuntimeError("model_divergence must be an object")
            divergence = {
                "confirmed_by": _text(
                    model_divergence.get("confirmed_by"), "model_divergence.confirmed_by"
                ),
                "reason": _text(
                    model_divergence.get("reason"), "model_divergence.reason"
                ),
            }
        object.__setattr__(self, "model_divergence", divergence)
        if self.model is not None and divergence is None:
            for profile in profile_list:
                if profile.platform is None or not profile.enabled:
                    # Bridge-command profiles run inside a host that manages
                    # its own model; a disabled profile never dispatches
                    # (binding() rejects it, AgentCatalog.select skips it), so
                    # its declared model cannot conflict with anything.
                    continue
                platform_model = normalized[profile.platform].model
                effective = profile.model or platform_model
                if effective is not None and effective != self.model:
                    raise AgentRuntimeError(
                        f"profile {profile.profile_id} pins model {effective!r} but the "
                        f"main agent model is {self.model!r}: sub-agents must run on the "
                        "main agent's model. Align the profile/platform model, or record "
                        'the user-approved exception with "model_divergence": '
                        '{"confirmed_by": "...", "reason": "..."} before dispatch.'
                    )
        # Resolve every enabled platform-bound profile's argv once, at load
        # time — before any agent can be launched — instead of recomputing
        # (and re-validating) it on each dispatch. Every enabled platform
        # profile is a dispatch candidate, so all templates must be
        # launchable; profiles kept around without being used should be
        # marked enabled=false. Resolving per profile uses the profile's
        # effective model (profile > platform > top-level); an argv that
        # carries the model flag injection would lose to, or a {model}
        # placeholder with no model to fill it, fails here. argv-pinned
        # profiles without a declared main model stay legal because no
        # injection happens for them, and disabled profiles are skipped
        # because they never dispatch. Dispatch reads the cached argv rather
        # than calling the resolver a second time, so the two paths cannot
        # drift apart.
        prepared: dict[str, tuple[str, ...]] = {}
        for profile in profile_list:
            if profile.platform is None or not profile.enabled:
                # Bridge profiles manage their own model, and a disabled
                # profile never dispatches (binding rejects it and
                # AgentCatalog.select skips it), so its argv cannot break a
                # run.
                continue
            spec = self.resolve_platform(profile)
            effective = spec.model if spec.model is not None else self.model
            model_flag = _BUILTIN_MODEL_FLAGS.get(spec.name) or spec.model_flag or "-m"
            prepared[profile.profile_id] = _apply_model(
                spec.argv, effective, model_flag=model_flag
            )
        object.__setattr__(self, "_prepared_argv", prepared)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PlatformDispatchConfig:
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
                model=spec.get("model"),
                model_flag=spec.get("model_flag"),
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
            model=payload.get("model"),
            model_divergence=payload.get("model_divergence"),
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
        if (
            profile.argv is not None
            or profile.timeout_seconds is not None
            or bool(profile.env)
            or profile.retries is not None
            or profile.model is not None
        ):
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
                model=profile.model if profile.model is not None else spec.model,
                model_flag=spec.model_flag,
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

    @property
    def main_model(self) -> str | None:
        """The main-agent model declared for this dispatch, if any."""

        return self._config.model

    def dispatch(self, profile_id: str, request) -> Mapping[str, object]:
        binding = self._config.binding(profile_id)
        model = None
        if binding.command is not None:
            import json as _json

            argv = binding.command
            input_text: str | None = _json.dumps(request.to_dict(), ensure_ascii=False) + "\n"
            timeout_seconds = binding.timeout_seconds or 300.0
            env = binding.env
            attempts = binding.retries or 1
            platform_name = "stdio-bridge"
        else:
            spec = self._config.resolve_platform(binding)
            model = spec.model or self._config.model
            argv = self._config._prepared_argv[profile_id]
            prompt = render_agent_prompt(request, language=self._config.language)
            if "{prompt}" in argv:
                argv = _parse()._materialize_argv(argv, prompt)
                input_text = None
            else:
                input_text = prompt
            timeout_seconds = spec.timeout_seconds
            env = spec.env
            attempts = spec.retries
            platform_name = spec.name
        stdout: str | None = None
        stderr = ""
        for attempt in range(1, attempts + 1):
            try:
                stdout, stderr = _parse()._run_subprocess(
                    argv,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                    env=env,
                )
                break
            except AgentRuntimeError:
                if attempt == attempts:
                    raise
        payload = _parse().extract_result_payload(stdout or "")
        usage: dict[str, object] = {
            "agent_id": request.agent_id,
            "role": request.role.value,
            "round_number": request.round_number,
            "profile_id": binding.profile_id,
            "platform": platform_name,
            "model": model,
            "attempts": attempt,
        }
        tokens_used = _parse()._extract_tokens_used(stderr)
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

    def invoke(self, request) -> Mapping[str, object]:
        return self._catalog.invoke(request)

    def capabilities(self) -> AgentRuntimeCapabilities:
        return self._catalog.capabilities()

    def usage_report(self) -> tuple[dict[str, object], ...]:
        return self._router.usage_report()

    def declared_main_model(self) -> str | None:
        """The main-agent model declared for this dispatch, if any.

        ``None`` means the main model was not declared (no top-level ``model``
        and no ``--model``), so sub-agent consistency cannot be enforced —
        platform/profile-level pins may still be in effect, but there is no
        main model to compare them against.
        """

        return self._router.main_model


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
    # A stdio bridge delivers the request JSON as-is: the GoalAnchor protocol
    # carries no model field, so a declared model could never be honored.
    # Rejecting it keeps the pinning guarantee from silently not applying.
    declared_model = payload.get("model") is not None or any(
        isinstance(spec, Mapping) and spec.get("model") is not None
        for spec in (profiles.values() if isinstance(profiles, Mapping) else ())
    )
    if declared_model:
        raise AgentRuntimeError(
            "a 'model' was declared but this is a stdio bridge configuration; "
            "bridge hosts manage their own sub-agent model. Use a platform "
            "dispatch config (profiles.<id>.platform) to pin models."
        )
    return JsonStdioAgentAdapter.from_dict(payload)


def probe_platform(
    name: str, timeout_seconds: float = 120.0, model: str | None = None
) -> dict[str, object]:
    """Run one minimal invocation against a built-in platform CLI.

    Verifies that the executable resolves, is authenticated, and its output
    can be parsed. Costs one tiny model call on the probed platform.
    """

    import json as _json

    if name not in _BUILTIN_ARGV:
        raise AgentRuntimeError(f"unknown platform: {name}")
    spec = PlatformSpec(name=name, argv=_BUILTIN_ARGV[name], timeout_seconds=timeout_seconds)
    prompt = 'Reply with exactly this JSON object and nothing else: {"probe": "ok"}'
    argv = _apply_model(
        spec.argv, model, model_flag=_BUILTIN_MODEL_FLAGS.get(name, "-m")
    )
    if "{prompt}" in argv:
        argv = _parse()._materialize_argv(argv, prompt)
        input_text: str | None = None
    else:
        input_text = prompt
    try:
        stdout, _stderr = _parse()._run_subprocess(
            argv,
            input_text=input_text,
            timeout_seconds=spec.timeout_seconds,
            env=spec.env,
        )
        payload = _parse().extract_result_payload(stdout)
    except AgentRuntimeError as exc:
        return {"platform": name, "probe": "failed", "detail": str(exc)[-300:]}
    if isinstance(payload, Mapping) and payload.get("probe") == "ok":
        return {"platform": name, "probe": "ok"}
    return {
        "platform": name,
        "probe": "unexpected-shape",
        "detail": _json.dumps(payload, ensure_ascii=False)[:200],
    }
