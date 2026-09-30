# GoalAnchor Agent Runtime

GoalAnchor uses a host-neutral named-Agent contract. The host may display and
run profiles however it likes; the GoalAnchor core only sees profiles, roles,
requests, and structured results.

## Roles

Each review cycle can bind enabled profiles to these logical roles:

- `reviewer`: independent analysis of the same immutable context. Required
  reviewers run in parallel for the current round.
- `arbiter`: summarizes the reviewer results and makes a provisional judgment.
- `meta_reviewer`: independently checks the arbiter's conclusion.

A profile may be named `Euclid`, `Reviewer B`, or anything else. The name is
UI metadata; the stable binding is its `profile_id` and supported roles.

## Bridge contract

The portable CLI accepts a JSON config and invokes one configured bridge per
role/profile. The bridge must:

1. Read exactly one JSON request from stdin.
2. Run the host's native subagent mechanism, if one exists.
3. Write exactly one JSON result object to stdout.
4. Write logs and diagnostics to stderr.
5. Exit non-zero when it cannot produce a valid result.

No shell command string is evaluated. Commands are argv arrays, so a host
bridge can be implemented in Python, Node, a native CLI wrapper, or another
runtime without changing GoalAnchor.

## Visible host-native dispatch

Subagent conversations are visible in a host UI only when the host spawns them
through its own session mechanism (tabs, task lists, agent panels). The
platform adapters below run headless subprocesses: their transcripts are
captured into results and `agent_bindings`, never rendered as chats. To keep
the review visible, render the exact per-role prompts and spawn host-native
subagents with them:

```text
python -m goalanchor review-prompt --request review-request.json --role reviewer --agent-id reviewer-1 --round 1
python -m goalanchor review-prompt --request review-request.json --role arbiter --agent-id arbiter --round 1 --reviewer-results reviewer-results.json
python -m goalanchor review-prompt --request review-request.json --role meta_reviewer --agent-id meta-reviewer --round 1 --arbiter-result arbiter-result.json
```

Then validate and aggregate the collected round deterministically:

```text
python -m goalanchor gate --phase review-cycle --record review-cycle.json
```

The record contains `context`, `risk`, `reviewer_results`, `arbiter`, optional
`meta_review`, and `round_number`. Dispute rounds re-render every prompt with
`--dispute dispute-packet.json` and re-spawn the same roles, still bounded by
`max_rounds`.

## Built-in platform adapters

Profiles can name a platform instead of a bridge command; GoalAnchor then
renders the role prompt itself, invokes the platform CLI, and parses the
output. Built-in platforms all pipe the prompt through stdin (no argv
quoting issues on Windows `.cmd` shims, no argv length limit): `codex`
(`codex exec -`), `claude` (`claude -p`), `gemini`, and `opencode`
(`opencode run`). Other platforms define `platforms.<name>.argv` with one
`{prompt}` placeholder for argv mode, or no placeholder to pipe stdin; when
argv mode is unavoidable keep the prompt single-line. The executable is
resolved through `PATH`, and relative paths resolve against the process
working directory.

Output parsing accepts a direct JSON result object, an envelope such as
`{"type": "result", "result": "<json text>"}`, or a JSON object embedded in
prose or a fenced code block. CLIs like `codex exec` echo the whole prompt
(which contains JSON) into their output before the final answer; the parser
therefore prefers candidates carrying result fields and takes the last one.
Results are then validated against the strict `ReviewerResult` /
`ArbiterResult` / `MetaReviewResult` schemas; a wrong `reviewer_id`,
`context_fingerprint`, or missing findings fails the round loudly instead of
being silently accepted.

Platform dispatch config example (two reviewers on different platforms run in
parallel, one profile per independent reviewer; built-in platforms need no
argv override):

```json
{
  "platforms": {
    "codex": {"timeout_seconds": 600}
  },
  "profiles": {
    "Euclid": {"platform": "claude", "roles": ["reviewer"]},
    "Fermat": {"platform": "codex", "roles": ["reviewer"]},
    "Gauss": {"platform": "codex", "roles": ["arbiter"]},
    "Noether": {"platform": "codex", "roles": ["meta_reviewer"]}
  },
  "role_bindings": {"arbiter": "Gauss", "meta_reviewer": "Noether"}
}
```

Run it with:

```text
python -m goalanchor review-cycle --request review-request.json --config agent-config.json
```

The request contains `context`, `risk`, and optional `max_rounds`. The command
prints the aggregated `ReviewServiceResult` plus an `agent_bindings` audit
array (agent_id → profile → platform → round → attempts). Risk
LOW/MEDIUM/HIGH/CRITICAL requires 1/2/2/3 distinct `reviewer` profiles — LOW
is never review-free, and every non-LOW risk gets at least two independent
reviewers. A runnable offline example lives in
`examples/multi-platform-review/`.

Meta Reviewer requests include the round's reviewer results together with the
arbiter conclusion, so the meta review can verify the arbiter's summary
against the actual reviewer output instead of trusting it.

Prompts are rendered in English by default; set `"language": "zh"` in the
dispatch config (or `review-prompt --language zh`) for Chinese prose. Role
headers, exact-value lines, and the JSON shape stay machine-anchored so
deterministic parsers are unaffected. When a platform reports token usage on
stderr (e.g. `codex exec` prints `tokens used`), the captured count is
recorded as `tokens_used` in the `agent_bindings` audit entry; platforms that
do not report usage simply omit the field.

Platforms may set `retries` (default 1 = no retry) to survive transient CLI
failures; only transport-level errors (non-zero exit, timeout, spawn failure,
empty stdout) are retried, never schema-invalid results. Preview the dispatch
without invoking any agent with `review-cycle --dry-run` (alias: `review-run`),
and inspect or probe platform availability with
`python -m goalanchor platforms [--config <config.json>] [--probe]` — the probe
runs one tiny invocation per available platform to verify authentication.

Two governance hardenings apply at the CLI boundary: a CRITICAL request is
rejected unless it carries `risk_declaration` with non-empty `declared_by` and
`reason` (echoed into the result for audit), and for HIGH/CRITICAL risk a meta
reviewer verdict that differs from the arbiter's verdict counts as a dispute —
convergence requires the meta reviewer to actually confirm the arbiter's
verdict, not just flag agreement.

Both styles can be mixed per profile: `platform` for built-in CLIs, `command`
for the JSON-over-stdio bridge.

Example profile configuration:

```json
{
  "role_bindings": {
    "reviewer": "euclid",
    "arbiter": "arbiter",
    "meta_reviewer": "meta"
  },
  "profiles": {
    "euclid": {
      "display_name": "Euclid",
      "roles": ["reviewer"],
      "capabilities": ["independent-analysis"],
      "command": ["goalanchor-host-bridge", "--profile", "euclid"],
      "timeout_seconds": 300
    },
    "arbiter": {
      "display_name": "Arbiter",
      "roles": ["arbiter"],
      "command": ["goalanchor-host-bridge", "--profile", "arbiter"]
    },
    "meta": {
      "display_name": "Meta Reviewer",
      "roles": ["meta_reviewer"],
      "command": ["goalanchor-host-bridge", "--profile", "meta"]
    }
  }
}
```

Run it with:

```text
python -m goalanchor review-cycle --request review-request.json --config agent-config.json
```

The request contains `context`, `risk`, and optional `max_rounds`. The bridge
receives the exact same `ReviewContext` fingerprint for every reviewer. A
dispute packet is included in the next round without changing the original
context.

## Host integration

Codex, Claude Code, DSH, Zcode, and other hosts implement only the bridge or a
native `AgentDispatcher` adapter. They must not change GoalAnchor's review
semantics. If a host cannot expose an independent Agent, return an explicit
`delegation_unavailable` result; never claim that a single main-agent pass was
an independent review.
