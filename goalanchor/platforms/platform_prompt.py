"""Prompt rendering and argv/model resolution.

Two responsibilities that change together: turning an
:class:`~goalanchor.agent_runtime.AgentRequest` into the shared review prompt,
and resolving a platform argv template against the model that must be pinned.
The ``{model}`` placeholder is recognized only where it is the model flag's
value slot, so the same spelling is used by config validation and dispatch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..agent_runtime import AgentRequest, AgentRole, AgentRuntimeError

_MODEL_PLACEHOLDER = "{model}"


def _placeholder_sites(items: Sequence[str], flag: str) -> list[int]:
    """Indexes where ``{model}`` is the model value slot.

    Only the model flag's value slot is a placeholder site: a standalone
    element (``-m {model}``, or a positional ``{model}``), the attached form
    (``--model={model}``), or the glued form (``-m{model}``). A ``{model}``
    embedded in some other element (``--note "run as {model}"``) is literal
    text and is passed through untouched.
    """

    sites: list[int] = []
    for index, item in enumerate(items):
        if (
            item == _MODEL_PLACEHOLDER
            or item == f"{flag}={_MODEL_PLACEHOLDER}"
            or item == f"{flag}{_MODEL_PLACEHOLDER}"
        ):
            sites.append(index)
    return sites


def _carries_flag(item: str, flag: str) -> bool:
    """Whether one argv element carries ``flag`` with its own literal value.

    Recognizes the bare flag (``--model``) and the attached-value form
    (``--model=opus``). The glued short form (``-mopus``) is deliberately not
    detected: it cannot be told apart from an unrelated option that merely
    begins with the same letters (``-max-tokens``), so rejecting it would
    break legitimate configs while catching at most a spelling the docs tell
    authors to avoid.
    """

    return item == flag or item.startswith(flag + "=")


def _apply_model(
    argv: Sequence[str],
    model: str | None,
    *,
    model_flag: str,
) -> tuple[str, ...]:
    """Pin the sub-agent model onto a platform argv template.

    The ``{model}`` placeholder is replaced in place, but only where it is the
    model flag's value slot (see :func:`_placeholder_sites`); occurrences
    inside other arguments are literal text. When no placeholder site exists,
    the ``<model_flag> <model>`` pair is inserted before the prompt-spec
    section (the first ``{prompt}`` placeholder or flag-like element) so the
    prompt stays last for both argv and stdin modes, and appended when the
    template has no such marker. An argv that carries the same flag with its
    own literal value is rejected — including when a placeholder site is also
    present, because the substituted value would then sit next to a second
    model flag whose value wins in most CLIs. Injecting a duplicate would
    silently unpin the sub-agent model.
    """

    items = tuple(argv)
    sites = _placeholder_sites(items, model_flag)
    if model is None:
        if sites:
            raise AgentRuntimeError(
                "argv contains a {model} placeholder but no model is configured"
            )
        return items
    for index, item in enumerate(items):
        if not _carries_flag(item, model_flag):
            continue
        # A flag whose value slot is the placeholder itself is the placeholder
        # site, not a hardcoded override.
        if (
            item == model_flag
            and index + 1 < len(items)
            and items[index + 1] == _MODEL_PLACEHOLDER
        ) or item == f"{model_flag}={_MODEL_PLACEHOLDER}":
            continue
        raise AgentRuntimeError(
            f"argv already carries {model_flag!r} with its own value; pin the model "
            "with a '{model}' placeholder instead of a hardcoded flag so the "
            "sub-agent model cannot be overridden silently"
        )
    if sites:
        for index in sites:
            item = items[index]
            if item == _MODEL_PLACEHOLDER:
                replacement = model
            elif item == f"{model_flag}={_MODEL_PLACEHOLDER}":
                replacement = f"{model_flag}={model}"
            else:
                replacement = f"{model_flag}{model}"
            items = items[:index] + (replacement,) + items[index + 1 :]
        return items
    model_args = (model_flag, model)
    for index, item in enumerate(items):
        if index > 0 and (item == "{prompt}" or item.startswith("-")):
            return items[:index] + model_args + items[index:]
    return items + model_args


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

_REVIEWER_SHAPE = (
    '{"reviewer_id": "<use the exact agent_id below>", "round_number": <integer>, '
    '"verdict": "PASS|WARN|FAIL", "findings": [], "isolated": true, '
    '"context_fingerprint": "<use the exact fingerprint below>"}'
)
_ARBITER_SHAPE = (
    '{"arbiter_id": "<use the exact agent_id below>", "round_number": <integer>, '
    '"verdict": "PASS|WARN|FAIL", "summary": "<one paragraph>", "rationale": '
    '"<why this verdict>", "reviewer_ids": ["<every reviewer_id>"], '
    '"context_fingerprint": "<use the exact fingerprint below>", '
    '"disagrees_with_reviewers": false}'
)
_META_SHAPE = (
    '{"reviewer_id": "<use the exact agent_id below>", "round_number": <integer>, '
    '"arbiter_id": "<use the exact arbiter_id below>", "verdict": "PASS|WARN|FAIL", '
    '"summary": "<one paragraph>", "findings": [], "context_fingerprint": '
    '"<use the exact fingerprint below>", "agrees_with_arbiter": true, '
    '"isolated": true}'
)


def render_agent_prompt(request: AgentRequest, *, language: str = "en") -> str:
    """Render the shared prompt for one review role.

    The prompt embeds the exact values (agent id, round, context fingerprint)
    the platform must echo, the JSON shape to return, and the immutable
    context. Optional sections carry a dispute packet, prior reviewer results,
    or the arbiter result under audit.
    """

    if language not in _PROMPT_LANGUAGES:
        raise AgentRuntimeError(
            f"language must be one of: {', '.join(_PROMPT_LANGUAGES)}"
        )
    texts = _PROMPT_TEXTS[language]
    role = request.role
    intro = {
        AgentRole.REVIEWER: texts["reviewer_intro"],
        AgentRole.ARBITER: texts["arbiter_intro"],
        AgentRole.META_REVIEWER: texts["meta_intro"],
    }[role]
    shape = {
        AgentRole.REVIEWER: _REVIEWER_SHAPE,
        AgentRole.ARBITER: _ARBITER_SHAPE,
        AgentRole.META_REVIEWER: _META_SHAPE,
    }[role]
    lines: list[str] = [
        # H1 carries the machine role token (agents parse it); the human label
        # lives in the intro, not the title.
        f"# GoalAnchor {role.value} review (round {request.round_number})",
        "",
        intro,
        "",
        texts["exact_values"]
        + f'agent_id="{request.agent_id}", round_number={request.round_number}, '
        + f'context_fingerprint="{request.context.fingerprint}"',
        "",
        texts["shape_line"],
        "",
        shape,
        "",
        texts["field_rules"],
        "",
        texts["context_header"],
        "",
        f'fingerprint: {request.context.fingerprint}',
        "",
        "```json",
        "",
        json.dumps(request.context.to_dict(), ensure_ascii=False, indent=2),
        "",
        "```",
    ]
    if request.dispute_packet is not None:
        lines += [
            "",
            texts["dispute_header"],
            "",
            "```json",
            "",
            json.dumps(request.dispute_packet.to_dict(), ensure_ascii=False, indent=2),
            "",
            "```",
        ]
    if request.reviewer_results:
        lines += [
            "",
            texts["reviewer_results_header"],
            "",
            "```json",
            "",
            json.dumps(
                [item.to_dict() for item in request.reviewer_results],
                ensure_ascii=False,
                indent=2,
            ),
            "",
            "```",
        ]
    if request.arbiter_result is not None:
        lines += [
            "",
            texts["arbiter_header"],
            # Machine-readable anchor (agents parse ``arbiter_id: <id>``); do
            # not reword it to prose.
            f"arbiter_id: {request.arbiter_result.arbiter_id}",
            "",
            "```json",
            "",
            json.dumps(request.arbiter_result.to_dict(), ensure_ascii=False, indent=2),
            "",
            "```",
        ]
    return "\n".join(lines)
