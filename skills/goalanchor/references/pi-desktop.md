# PI-Desktop integration

Use the GoalAnchor Skill/Extension plugin in vastsa/PI-Desktop. Pi CLI's `.pi`
directory installation does not establish desktop compatibility. The package
uses the same Skill source and includes the Python domain core.

Build: `python -m goalanchor.pi_desktop build --destination <new-directory>
--archive <new-package.piplug>`. Install using the desktop Plugins page, review
the declared permissions, and enable the plugin. Do not alter the host's grant
records or private database. Python 3.11+ is required; the package needs no pip
dependencies. If necessary configure Python in plugin settings and use
`/goalanchor-python <executable-path>` for extension commands.

The plugin Skill has the desktop id `local.goalanchor/goalanchor`; invoke
`/skill:local.goalanchor/goalanchor` to distinguish it from a separately imported
Skill with the short id `goalanchor`.

## Review

In Agent mode use the plugin `goalanchor_review` tool with `request.context`
(goal, prompt, constraints, evidence_refs, candidate), risk and max_rounds (1 or
2). CRITICAL needs a user risk declaration. Put the actual review material in
the supplied context: a reference string alone does not prove evidence.

The adapter pins the exact current provider/model identity, independently calls
each Reviewer, then Arbiter and Meta Reviewer, and returns domain-validated
results plus per-role records. It never grants execution permission based on a
model verdict. On the 0.18.0 API these are isolated, tools-free completions, not
visible desktop subagent sessions; report this limitation accurately.

Do not treat missing APIs, unknown model identity, schema errors, timeout,
cancellation or rate limits as a successful review. The plugin has a 100-second
total review budget under the host's 110-second tool limit; two rounds may not
finish within it. A cancelled local bridge discards late responses, but the
host API cannot cancel an already-started remote completion.

## Explicit tool guard

Ordinary sessions remain unguarded. When the user enables
`/goalanchor-guard on`, mutations and unknown tools need exact approval from
`/goalanchor-approve <workspace-json>`. A record contains the original PLAN
contract, work_items, structure_review, verify, execute and operations (tool
plus complete input). The adapter checks PLAN/VERIFY/EXECUTE, then the user
reviews a host confirmation. Calling `goalanchor_gate` or
`/goalanchor-check <workspace-json>` only checks a record; it cannot grant
permission. Never invent a user's confirmation or use an agent-signed grant.

Approval is single-use, scoped to the session/model/input, and invalidated by
revocation, mismatch, tool failure, round end and runtime replacement. Pending
confirmations cannot resurrect a revoked grant. The verified desktop Read/Glob/Grep tools and this
plugin's exact check-tool names and the host's ToolSearch discovery remain available. Host tool names and JSON
inputs must match; do not normalize away consequential fields.

## Later versions

The minimum protocol baseline is 0.18.0. Detect the public capabilities instead
of assuming a particular install path or copying private host APIs. Noncritical
additional fields are tolerated. Missing optional UI can degrade; missing
identity, review or tool-gating contracts cannot silently become PASS. Show
capability diagnostics and distinguish tested versions from unknown future
versions. New host APIs need a verified contract and tests before use.

The extension is a cooperative guard, not an OS sandbox: the host may ignore
hook exceptions/timeouts. Respect desktop mode gating; use the Skill and user
check commands when plugin tools are unavailable in Plan/Goal mode.
