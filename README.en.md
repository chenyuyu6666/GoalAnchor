# GoalAnchor

[中文](README.md) | **English**

> Move complex engineering work from a plan to verifiable execution and delivery.

GoalAnchor is a goal and evidence checking toolkit for AI Agents. Use it with implementation plans, code changes, experiments, and multi-Agent reviews. It organizes critical work as **PLAN → VERIFY → EXECUTE** and helps catch goal drift, unverified assumptions, weak evidence, and premature claims of completion.

GoalAnchor provides a Skill, a Python library, and a command-line interface. It checks whether plans and records follow defined rules; it does not replace real tests, verification of external facts, or engineering judgment.

## What it does

- **Goal and execution gates**: Check the goal, logic, assumptions, minimal path, and structure before acting; set observable acceptance conditions before execution.
- **Evidence and delivery closure**: Record evidence sources, experiment conditions, conclusion strength, and delivery state so that “tests passed” is not mistaken for “the problem is fixed.”
- **Multi-Agent review**: Reviewers work in parallel, an Arbiter aggregates their results, and a Meta Reviewer checks the arbitration. Results include machine-readable role, model, and round details.
- **Skill and CLI**: Install the GoalAnchor Skill into supported Agent hosts; use the CLI for deterministic checks, platform configuration, and reviews.
- **PI-Desktop plugin**: Provides a desktop plugin, Python rules bridge, model review, and opt-in tool gating. See the [plugin guide](goalanchor/_pi_desktop/README.md) for host details.

## v0.9.0 strict workflow baseline

v0.9.0 splits the strict runtime into a stable public façade plus `StrictWorkflow` and legacy paths. Existing `Workflow` objects, JSON, and historical behavior remain compatible. `StrictWorkflow` adds baseline/evidence/review-context construction and restricted operation registration; SQLite schema v2 stores immutable proof history and a CAS-protected head. Opening a v1 database only adds structure and does not promote legacy records into trusted sources.

Strict review, questions, authorization, execution, reconcile, and closure still return `UNAVAILABLE` in v0.9.0. Legacy reviews, grants, and `CLOSED` results are not strict completion proof. P2b–P7 remains incomplete, and all 58 native-host statuses remain `NOT_RUN`. See the [strict runtime guide](docs/workflow-v0.8.md) for the trust boundary and required providers.

## v0.8.0 first-batch trust foundations

v0.8.0 detects Reviewer/Arbiter verdict disagreements, binds PASS closure to the current acceptance set, and supports optional `ReviewContext.evidence_digests` pairs `(reference, sha256_hex)`.

Nonempty digests must cover all distinct evidence references exactly once. Inputs are frozen, SHA-256 values are normalized to lowercase, and pairs are sorted by reference. Digests participate in the fingerprint and survive `to_dict` / `from_dict`; changed content digests invalidate old review results.

Without digests, the legacy fingerprint and JSON shape remain unchanged and bind references only. Callers must collect trustworthy evidence and recheck it before execution; the core does not read files or establish digest authenticity. The first-batch digest changes ship with the strict runtime in v0.8.0; the runtime version is `0.9.0`.

## v0.8.0 strict workflow

`Workflow` adds trusted controller events, a SQLite one-use authorization ledger, structured questions, execution receipts, and criterion observations. Legacy APIs remain auxiliary checks. Strict enforcement requires a trusted controller that protects keys and the database and supplies a controlled executor; the model cannot sign approvals. Scope checks cwd and declared resource paths; cwd is not a sandbox. `UNKNOWN` means the outcome is uncertain, and default recovery never automatically redispatches.

The strict runtime also supports an explicitly authorized idempotent recovery transport: original authorization/backend/key, tested evidence, and two CAS stages remain bound; the original UNKNOWN still needs separate reconciliation. Criterion reuse requires a current signed object digest and the original observation provenance, without reviving old grants. Presentation-only changes are limited to title. Missing legacy proof yields LEGACY_UNVERIFIED; there is no general migration. Host bindings freeze embedding-supplied version/model/mode/Hook facts and invalidate old context on change, without proving native Hooks. Review delivery receipts require matching session/digest/DELIVERED; synthetic receipts do not establish model reading. Five-host TextHostAdapter coverage uses shared trusted text fixtures, not five native host validations.

```powershell
python -m goalanchor workflow capabilities
python -m goalanchor workflow demo --directory .\workflow-demo-v08
```

The demo remains a local example using synthetic reviews and test approvals. Codex 0.162.1 has real-model command callback, question fixture, rejection, cancellation, and exact fixed-command approval closure evidence. The approval closure uses synthetic review and explicit test approval, without establishing human UI approval or independent multi-model review. Enforcement of every host tool is not covered; Claude / DSH / Zcode remain ADVISORY / UNAVAILABLE. The 58 core scenarios have local executable coverage, while all 58 native-host statuses are NOT_RUN; this does not establish full native-host acceptance. See the [runtime guide](docs/workflow-v0.8.md) for offline inspection, WAL recovery, and evidence boundaries.

## Requirements

- **Python 3.11 or later**, on Windows, macOS, or Linux.
- The GoalAnchor Python runtime has no third-party Python dependencies.
- For built-in CLI Agent dispatch, install and sign in to the corresponding Agent CLI separately. GoalAnchor does not install those Agents or manage their credentials.
- The PI-Desktop plugin requires Python to be available to the host process. Desktop development tests also require Node.js 20 or later.

## Installation

### Install the CLI from GitHub

Create a virtual environment from the repository root:

~~~sh
git clone https://github.com/chenyuyu6666/GoalAnchor.git
cd GoalAnchor
python -m venv .venv
~~~

Windows PowerShell:

~~~powershell
.\.venv\Scripts\Activate.ps1
~~~

macOS / Linux:

~~~sh
source .venv/bin/activate
~~~

After activating the virtual environment, install the project:

~~~sh
python -m pip install .
~~~

### Install the GoalAnchor Skill

This example installs the Skill for the current Codex user:

~~~sh
goalanchor install --target codex --scope user
~~~

To install it into a project:

~~~sh
goalanchor install --target claude --scope project --project-root .
~~~

After installation, start or refresh a session in the host and ask the Agent to use GoalAnchor to check a formed plan, change, or conclusion. See the platform table below for installation targets.

## Quick start

List commands and check which built-in Agent CLIs are available. This reports the environment without launching an Agent:

~~~sh
goalanchor --help
goalanchor platforms --json
~~~

Run the included offline review example. It uses a local echo Agent and makes no external model calls:

~~~sh
goalanchor review-cycle --request examples/multi-platform-review/request.json --config examples/multi-platform-review/config.json
~~~

After installing the Skill in a supported Agent host, start a task with a prompt such as:

> Use GoalAnchor to review this implementation plan. First state TASK_OBJECTIVE, CURRENT_BLOCKING_UNCERTAINTY, and PROPOSED_STEP_AND_DECISION_LINK, then evaluate Q1 Goal, Q2 Logic, Q3 Path, Q4 Minimum, and Q5 Structure in PLAN. If it passes, enter VERIFY and set the acceptance criterion before proceeding through PLAN → VERIFY → EXECUTE. Report unverified facts as uncertain.

To probe real platforms, run <code>goalanchor platforms --probe --json</code>. This invokes installed Agent CLIs and may consume model usage.

## Supported platforms

The table distinguishes Skill file locations, built-in Agent dispatchers, and the standalone desktop plugin. **A Skill installation target only describes where files are placed; it does not imply a built-in Agent dispatcher for that host.**

| Platform / host | Skill target | Agent dispatch or plugin |
|---|---|---|
| Codex CLI | <code>codex</code> | Built-in CLI dispatcher; install and sign in to the Codex CLI separately |
| Claude Code | <code>claude</code> | Built-in CLI dispatcher; install and sign in to the Claude CLI separately |
| Gemini CLI | <code>gemini</code> | Built-in CLI dispatcher; install and sign in to the Gemini CLI separately |
| OpenCode | <code>opencode</code> | Built-in CLI dispatcher; install and sign in to the OpenCode CLI separately |
| Cursor | <code>cursor</code> | Skill installation is available; Agent execution needs a custom command or JSON-over-stdio bridge |
| Pi CLI | <code>pi</code> | Skill installation is available; this is Pi CLI, not PI-Desktop |
| DeepSeek Harness (DSH) | <code>dsh</code> | Skill installation is available; Agent execution needs a custom command or bridge |
| Agent Skills-compatible directory | <code>agents</code> | Installs the Skill into a shared directory; Agent dispatch is provided by the host or custom configuration |
| Zcode | <code>zcode</code> with <code>--destination</code> | Requires an explicit Skill directory; Agent dispatch requires custom integration |
| Other hosts | <code>custom</code> with <code>--destination</code> | Configure a custom CLI command or JSON-over-stdio Agent |
| PI-Desktop | Build a <code>.piplug</code> plugin | Standalone Extension and Python bridge; do not install it with <code>--target pi</code> |

The built-in dispatchers are Codex, Claude, Gemini, and OpenCode. Other Agents can use a configured command or the JSON-over-stdio bridge. PI-Desktop is a separate plugin that detects capabilities through the host’s public interfaces. See the [compatibility specification](docs/PI-Desktop适配规格.md) and [verification record](docs/PI-Desktop验证记录.md) for the tested baseline and limits.

Build the PI-Desktop plugin:

~~~sh
python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug
~~~

For installation and permissions, see the [PI-Desktop plugin guide](goalanchor/_pi_desktop/README.md).

## More information

- [Offline multi-Agent review example](examples/multi-platform-review/README.md)
- [Changelog](CHANGELOG.md) · [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases)
- [GitHub canonical repository](https://github.com/chenyuyu6666/GoalAnchor) · [Gitee mirror](https://gitee.com/chen-yuyu6666/goal-anchor)
- [Contributing](CONTRIBUTING.md)
- [License](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md) · [Upstream source records](UPSTREAM_SOURCES.md)

Third-party Skills and snapshots retain their own licenses; the repository license does not automatically cover third-party materials.
