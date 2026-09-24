"""Built-in multi-platform Agent dispatch for the review execution layer.

The review policy lives in :mod:`goalanchor.review_service`; this subpackage is
the execution adapter that makes the policy usable on real platforms. It maps
the host-neutral review roles (``reviewer`` / ``arbiter`` / ``meta_reviewer``)
onto named profiles that run either on a built-in agent CLI (Codex, Claude
Code, Gemini CLI, OpenCode) or behind a JSON-over-stdio bridge command.

The adapter never changes review semantics: it builds commands, renders the
shared prompt, parses the platform's output envelope, and routes profiles with
the same isolation rules as the rest of the runtime.

Modules:

- :mod:`platform_common` — constants and field validators
- :mod:`platform_prompt` — prompt rendering and argv/model resolution
- :mod:`platform_parse` — subprocess execution and output parsing
- :mod:`platform_config` — spec/profile/config classes and entry points
"""

from __future__ import annotations

from .platform_common import (
    _BUILTIN_ARGV,
    _BUILTIN_MODEL_FLAGS,
    BUILTIN_PLATFORMS,
)
from .platform_config import (
    PlatformAgentAdapter,
    PlatformAgentRouter,
    PlatformDispatchConfig,
    PlatformSpec,
    ProfileBinding,
    adapter_from_config,
    build_platform_adapter,
    probe_platform,
)
from .platform_parse import (
    _extract_tokens_used,
    _materialize_argv,
    _run_subprocess,
    extract_result_payload,
)
from .platform_prompt import (
    _apply_model,
    _carries_flag,
    _placeholder_sites,
    render_agent_prompt,
)

__all__ = [
    "BUILTIN_PLATFORMS",
    "PlatformAgentAdapter",
    "PlatformAgentRouter",
    "PlatformDispatchConfig",
    "PlatformSpec",
    "ProfileBinding",
    "adapter_from_config",
    "build_platform_adapter",
    "extract_result_payload",
    "probe_platform",
    "render_agent_prompt",
]
