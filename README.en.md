# GoalAnchor

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/chenyuyu6666/GoalAnchor?label=release)](https://github.com/chenyuyu6666/GoalAnchor/releases)
[![Validate](https://github.com/chenyuyu6666/GoalAnchor/actions/workflows/validate.yml/badge.svg)](https://github.com/chenyuyu6666/GoalAnchor/actions/workflows/validate.yml)

[中文](README.md) | **English**

> Move complex engineering tasks from a plan to verifiable execution and delivery.

## Contents

- [Overview](#overview)
- [Features](#features)
- [Current release](#current-release)
- [Requirements](#requirements)
- [Install and run](#install-and-run)
- [Usage](#usage)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Dependencies and development](#dependencies-and-development)
- [Core terminology](#core-terminology)
- [Supported applications](#supported-applications)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)
- [More information](#more-information)

## Overview

GoalAnchor is a **goal-anchoring and trustworthy-convergence tool** for AI Agents and developers working on implementation plans, code changes, experiment conclusions, and multi-Agent reviews. It organizes work as **PLAN → VERIFY → EXECUTE** to expose goal drift, unverified assumptions, insufficient evidence, ignored reviewer objections, and premature completion claims.

The goal is not to collect more information, but to **reduce decision uncertainty**: fix the objective and observable acceptance criteria first, inspect evidence and assumptions next, and only then allow execution and closure.

GoalAnchor provides a Skill, a Python library, a CLI, and a PI-Desktop plugin. It checks whether plans and records follow the rules; it does **not** replace real tests, external fact-checking, or engineering judgment.

## Features

- **Goal and execution gates**: check objective, logic, assumptions, minimum path, and plan structure before acting; fix observable acceptance criteria before execution.
- **Evidence and delivery closure**: record evidence sources, experiment conditions, conclusion strength, and delivery state so "tests passed" is not mistaken for "the problem is fixed."
- **Multi-Agent review**: parallel Reviewers, an Arbiter, and a Meta Reviewer, with machine-readable roles, models, and rounds.
- **Skill and CLI**: install the GoalAnchor Skill into supported Agent hosts; use the CLI for deterministic checks, platform configuration, and reviews.
- **Strict workflow**: trusted controller events, a SQLite one-use authorization ledger, structured questions, execution receipts, and acceptance observations.
- **PI-Desktop plugin**: desktop plugin, Python rules bridge, model review, and opt-in tool gating.

## Current release

| Item | Value |
|---|---|
| Version | `0.9.1` |
| Release date | 2026-10-20 |
| GitHub Release | [v0.9.1](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.9.1) |
| Release commit | `dfafe47` |
| Core scenarios | 58/58 `PASS` |
| Native hosts | 58/58 `NOT_RUN` |

- `StrictWorkflow` now covers the offline core chain for review, authorization questions, one-time grants, restricted execution, UNKNOWN/reconcile, observations, and closure. Native hosts remain classified by evidence as `NOT_RUN`, `LIMITED`, `ADVISORY`, or `UNAVAILABLE`; host acceptance, efficiency governance, and the remaining P2b–P7 delivery work are incomplete. See [CHANGELOG](CHANGELOG.md) for release history.

## Requirements

- **Python 3.11 or later** on Windows, macOS, or Linux.
- No third-party Python runtime dependencies.
- Built-in CLI Agent dispatch requires the corresponding Agent CLI to be installed and signed in separately. GoalAnchor does not install those Agents or manage their credentials.
- The PI-Desktop plugin requires Python to be available to the host. Desktop development tests require Node.js 20 or later.
- Building and development checks require `setuptools>=68`, `build`, `wheel`, `pytest`, `ruff`, and `mypy==2.4.0`; see [Dependencies and development](#dependencies-and-development).

## Install and run

### Install the CLI from GitHub

```sh
git clone https://github.com/chenyuyu6666/GoalAnchor.git
cd GoalAnchor
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```sh
source .venv/bin/activate
```

Install and verify:

```sh
python -m pip install .
goalanchor --help
python -c "import goalanchor; print(goalanchor.__version__)"
```

You can also install a wheel from [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases):

```sh
python -m pip install goalanchor-0.9.1-py3-none-any.whl
```

### Install the GoalAnchor Skill

Codex user directory:

```sh
goalanchor install --target codex --scope user
```

Install into the current project:

```sh
goalanchor install --target claude --scope project --project-root .
```

After installation, create or refresh a host session and ask the Agent to review a formed plan, change, or conclusion. Valid targets are `agents`, `claude`, `codex`, `cursor`, `custom`, `dsh`, `gemini`, `opencode`, `pi`, and `zcode`; a Skill install target only controls where files are placed and does not imply built-in dispatch. See [Supported applications](#supported-applications) for each application's purpose, integration, and role.

### Install the PI-Desktop plugin

PI-Desktop uses a separate `.piplug` plugin, not `--target pi`:

```sh
python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug
```

Install the generated `.piplug` from the PI-Desktop Plugins page and review the requested permissions. See the [PI-Desktop plugin guide](goalanchor/_pi_desktop/README.md).

### Quick run

```sh
# Show commands and local platform environment; does not start an Agent
goalanchor --help
goalanchor platforms --json

# Offline multi-Agent review example (local echo Agent; no external model)
goalanchor review-cycle --request examples/multi-platform-review/request.json --config examples/multi-platform-review/config.json

# Render round-1 reviewer prompts only, without invoking any Agent (integration check)
goalanchor review-cycle --request ... --config ... --dry-run

# Strict workflow capabilities and local demo
goalanchor workflow capabilities
goalanchor workflow demo --directory ./workflow-demo
```

## Usage

### Basic flow

1. **PLAN**: state `TASK_OBJECTIVE`, `CURRENT_BLOCKING_UNCERTAINTY`, the proposed step, and its decision link; check Q1 Goal, Q2 Logic, Q3 Path, Q4 Minimum, and Q5 Structure.
2. **VERIFY**: fix observable acceptance criteria first, then inspect evidence sources, experiment conditions, and conclusion strength.
3. **EXECUTE**: act only while authorization and baseline remain valid; save receipts and acceptance observations, and close only when every condition is met.

With the Skill installed, start a task with:

> Review this implementation plan with GoalAnchor. State TASK_OBJECTIVE, CURRENT_BLOCKING_UNCERTAINTY, and PROPOSED_STEP_AND_DECISION_LINK, then check Q1–Q5 in PLAN order. If it passes, enter VERIFY and fix acceptance criteria before moving through PLAN → VERIFY → EXECUTE. Report unverified facts.

### Python API

`ReviewContext` can bind caller-collected evidence with `evidence_digests`:

```python
from hashlib import sha256
from goalanchor import ReviewContext

context = ReviewContext(
    goal="review candidate fix",
    prompt="check objective and evidence",
    constraints=(),
    evidence_refs=("E-report",),
    candidate="candidate",
    evidence_digests=(("E-report", sha256(b"frozen report").hexdigest()),),
)
```

Nonempty digests must cover every evidence reference exactly once and be 64-character lowercase SHA-256 values. Inputs are frozen, digests are normalized and sorted, and `to_dict` / `from_dict` round-trips are supported. Without digests, the legacy fingerprint and JSON shape remain unchanged and bind references only. The core does not read files or establish digest authenticity; callers collect trustworthy evidence and recheck it before execution.

### Strict workflow

`StrictWorkflow` is an incremental API alongside legacy `Workflow`. It takes `store`, `user_authority`, `observer_authority`, and `policy` positionally, plus keyword providers such as `trusted_event_verifier` and `trusted_runtime_binding_provider`. Models or Agents must not supply principal, session, or host facts. See the [strict runtime guide](docs/workflow-v0.8.md).

Legacy `Workflow` objects, JSON, and historical empty-evidence behavior remain compatible. Legacy reviews, grants, `CLOSED`, and `VerificationResult` do not become Strict proof.

### Review request and output

The multi-Agent review command `goalanchor review-cycle --request request.json --config config.json` takes a request file with these fields:

| Field | Required | Meaning |
|---|---|---|
| `context` | Yes | Review context: `goal`, `prompt`, `constraints`, `evidence_refs`, `candidate`; optional `evidence_digests` binds evidence content. |
| `risk` | Yes | Review risk level: `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`; `CRITICAL` requires a `risk_declaration` with non-empty `declared_by` and `reason`. |
| `max_rounds` | No | Round limit, default `2`; each round is Reviewer → Arbiter → Meta Reviewer. |
| `max_workers` | No | Reviewer concurrency per round; runtime default applies when omitted. |

`--config` fields are described under “Configuration” and in the [sample config](examples/multi-platform-review/config.json); `--model` overrides the configured model for one run.

Output: a human-readable JSON summary by default; pass `--json` for machine-readable output. Key fields: `outcome` (`PASS` / `WARN` / `REVIEW_REQUIRED` / `HUMAN_DECISION_REQUIRED` / `STOPPED`), the round number with per-round reviewer results, the arbiter summary with receipt validation info, plus `warnings` (model-consistency warnings) and `usage_summary` (call count, token usage, models actually used). Exit code `0` means the flow completed (not that the review passed), `1` means the review did not pass (`STOPPED` or `HUMAN_DECISION_REQUIRED`), and `2` means the request or config is invalid. To validate a serialized review record, use `goalanchor gate --phase review-cycle`, which enforces the same schema over `context`, `reviewer_results`, `arbiter`, `meta_review`, and related fields.

## Configuration

| Area | Configuration |
|---|---|
| Core CLI / Python API | No required config file or environment variable; use CLI/API arguments. |
| Agent dispatch | JSON config describing platforms, profiles, commands, and models. See the [example config](examples/multi-platform-review/config.json) and pass it with `--config`. Model resolution priority is profile > platform > top level. |
| Strict workflow | Requires a persistent SQLite database, trusted `EventAuthority` keys, a controlled executor, and an observer. Keys and the ledger must stay on the trusted control side and must not be exposed to models. See the [strict runtime guide](docs/workflow-v0.8.md). |
| PI-Desktop | Set the Python executable in the plugin settings, or set `GOALANCHOR_PYTHON` before launching the desktop. The plugin does not read API keys. |
| Agent CLI credentials | Managed by each Agent CLI. GoalAnchor does not read or store them. |
| Test harness | `GOALANCHOR_TEST_TASK_START/END` and `GOALANCHOR_DIALOGUE_TASK_START/END` are test markers; users do not need to set them. |

Real platform probing uses `goalanchor platforms --probe --json`. It invokes installed Agent CLIs and may consume model usage; omit `--probe` when not needed.

Top-level fields of the dispatch config:

| Field | Meaning |
|---|---|
| `model` | Top-level default model ID; `review-cycle --model` overrides it from the command line. Resolution order: profile > platform > top level. |
| `platforms.<name>` | Platform definition: `argv` (at most one `{prompt}` placeholder), `timeout_seconds` (default 600), `model`, `model_flag`. |
| `profiles.<id>` | A named Agent: `platform` (references a platform) or `command`/`argv` (stdio bridge, mutually exclusive), `roles` (`reviewer`/`arbiter`/`meta_reviewer`), `display_name`; optional `model`, `timeout_seconds`, `env`, `enabled`, `retries`, `capabilities`. |
| `role_bindings` | Assigns profiles to `arbiter` and `meta_reviewer`; reviewer roles use every profile declaring that role. |
| `capabilities` | Dispatch capability flags: `supports_parallel` (concurrent reviewers per round), `max_concurrency`. |

Field validation lives in `goalanchor/platforms/platform_config.py`; see the [sample config](examples/multi-platform-review/config.json) for a minimal working example.

### Performance and scale limits

| Item | Default | Notes |
|---|---|---|
| Platform dispatch timeout | 600 s | Override per platform with `timeout_seconds` in the config (`AgentRequest` defaults to 300 s internally; dispatch follows the platform config). |
| Reviewer concurrency per round | 4 | `AgentRuntime.max_concurrency`, must be a positive integer; Arbiter and Meta Reviewer always run sequentially. |
| Review rounds | 2 | Adjust via `max_rounds` in the request; hitting the limit returns `REVIEW_REQUIRED`. |
| SQLite ledger concurrency | single writer | Write transactions serialize with `BEGIN IMMEDIATE`; `busy_timeout` follows the store's `timeout`. Under heavy Windows load, multi-process writes can contend on locks — a known boundary. |
| Database size | no built-in cap | The ledger is append-only; archive old database files for long-running use. |

## Project layout

```text
GoalAnchor/
├── goalanchor/               Python package (contracts, gates, evidence, review, workflow, hosts)
├── tests/                    unit/integration tests, regression fixtures, PI-Desktop Node tests
├── docs/                     development standards, workflow/PI-Desktop help, CI task definitions
├── skills/                   product Skill and third-party Skill snapshots
├── scripts/                  validation, regression, benchmark, bundle sync, host checks
├── examples/                 offline multi-Agent review example
├── audit/                    acceptance map and JUnit evidence
├── delivery/                 published artifacts (wheel/sdist/checksums/manifest)
├── .github/ .workflow/       GitHub Actions / Gitee Go pipelines
├── pyproject.toml            package metadata, dependencies, Ruff/Mypy/pytest config
├── MANIFEST.in               sdist packaging manifest
├── requirements-dev.txt      development and validation dependencies
├── requirements-validation.txt  Skill frontmatter validation dependencies (PyYAML)
├── requirements-image.txt    optional ImageGen dependencies
├── AGENT-COMPATIBILITY.md    Agent compatibility tiers and support boundaries
├── CONTRIBUTING.md           contribution and Git rules
├── SECURITY.md               security reporting process
├── CODE_OF_CONDUCT.md        code of conduct
├── CHANGELOG.md              release history
├── LICENSE                   MIT license
├── THIRD_PARTY_NOTICES.md    third-party notices
├── UPSTREAM_SOURCES.md       third-party Skill sources and sync rules
└── README.md / README.en.md  Chinese/English documentation
```

`build/`, `.venv/`, caches, `test-results/`, and `graphify-out/` are local generated files, not source structure.

## Dependencies and development

### Runtime dependencies

- Python `>=3.11`.
- No third-party Python runtime dependencies.
- Built-in Agent dispatch requires external Agent CLIs (Codex, Claude, Gemini, OpenCode).
- PI-Desktop requires Python on the host PATH; desktop development tests require Node.js 20+.

### Development dependencies

```sh
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m pip install ruff mypy==2.4.0
```

`requirements-dev.txt` contains pytest and YAML validation dependencies; `requirements-validation.txt` holds the standalone Skill-frontmatter and Agent-metadata validation dependencies (PyYAML); `requirements-image.txt` is optional for the explicit ImageGen CLI fallback and is not required to run GoalAnchor.

### Local quality checks

```sh
python -m pytest -q
python -m ruff check goalanchor tests scripts
python -m mypy goalanchor
python scripts/evaluate_fixtures.py --fixtures tests/fixtures/regression --revision "$(git rev-parse HEAD)"
python scripts/run_test_task.py --task docs/GoalAnchor-自动测试任务.md
python scripts/sync_skill_bundle.py --check
```

- Regression fixtures import GoalAnchor in a subprocess; run `pip install -e .` first.
- CI covers three operating systems and Python 3.11–3.13 with a pinned MyPy 2.4.0.
- `ruff` checks project Python only; `skills/.system/` is an upstream snapshot and is excluded.
- For JavaScript or PI-Desktop bridge changes, use Node.js 20 and run `node --test tests/js/test_pi_desktop.cjs`.

## Core terminology

| Term | Meaning |
|---|---|
| Three-stage gating | `PLAN → VERIFY → EXECUTE`: fix the goal and plan first, then the evidence and acceptance conditions, and only then allow execution and closure. |
| Five questions Q1–Q5 | Plan-stage checks: Q1 Goal, Q2 Logic, Q3 Path, Q4 Minimum (smallest sufficient set), Q5 Structure (executable structure). |
| Gate | Deterministic hard checks, not prompt-level soft constraints; `GateId` also covers VERIFY, EXECUTE, CLOSURE, ARTIFACT, PROVENANCE, and more. |
| Evidence levels | `Observation` → `Fact` → `ReproducibleExperiment` → `ExclusionaryEvidence` → `RootCauseEvidence`. |
| Conclusion ceilings | Each evidence item only supports conclusions up to a strength: `Observation` / `Fact` / `Association` / `Causal` / `RootCause` / `DesignNecessity`; stronger claims must be downgraded or backed by more evidence. |
| Evidence status | `VALID` / `LIMITED` / `INSUFFICIENT` / `CONTRADICTED`, decided by how evidence matches the claimed conclusion. |
| Nine closure levels | Ascending closure strength: `Symptom Suppressed` → `Containment` → `Local Fix` → `Root Cause Identified` → `Root Fix` → `Build Success` → `Artifact Verified` → `System Validation` → `Product Closure`; claiming a level requires matching evidence. |
| Provenance chain | Embedded-delivery chain: `Source → Object → ELF → BIN/FW → Flashed Artifact → Device Under Test`. |
| ReviewContext | The frozen context of one review: goal, prompt, constraints, evidence refs, and the candidate; optionally binds evidence content via `evidence_digests`. |
| Review roles | Reviewer → Arbiter → Meta Reviewer per round; unresolved rounds continue until `max_rounds`. |
| Strict / Legacy | Two paths over the same contract and evidence model: `Workflow` (legacy, unchanged behavior) and `StrictWorkflow` (trusted-controller binding of baselines, evidence, review, and authorization). |
| Review packet | The full input handed to one role: role prompt, context, prior rounds, and receipts; receipt validation prevents result substitution. |

All of these terms have concrete implementations in `gates.py`, `evidence.py`, `closure.py`, and `review_service.py`; the code is authoritative.

## Supported applications

GoalAnchor is not an Agent and does not provide a model. It integrates with external applications in three forms, and one application can carry more than one of them:

- **Skill (rule injection)**: places the `PLAN → VERIFY → EXECUTE` rules and the Q1–Q5 checklist in the host's Skill directory for the host Agent to follow.
- **Executor (sub-agent dispatch)**: `review-cycle` invokes an external CLI from configuration and dispatches reviewer / arbiter / meta-reviewer into separate processes.
- **Plugin (deep desktop integration)**: ships as a `.piplug` that registers a Skill, tools, and hooks inside a desktop host.

> **A successful Skill install only means files were placed. It does not imply a built-in dispatcher or native enforcement.** Native host capability is always established by per-host testing.

### Roles

| Role | Purpose | Boundary |
|---|---|---|
| Host application | Provides the session UI, model, and tool calls; loads the Skill or plugin | Rules run inside the host; GoalAnchor does not take over the host Agent loop |
| Review executor | Invoked by `review-cycle` to produce independent review results | The sub-agent may be the same application or a different CLI / model |
| Trusted control plane | Holds keys, the SQLite ledger, one-time authorizations, and execution receipts | Must be owned by the calling application, never self-reported by a model |
| Build and release side | Runs tests, builds wheels, produces deliverables | CI or local scripts, not an Agent capability |

### 1. Hosts with built-in dispatch

These four applications support both Skill installation and built-in sub-agent dispatch, so they can drive multi-Agent review directly.

| Application | Purpose | Integration | Role | Support level |
|---|---|---|---|---|
| **Codex CLI** | OpenAI terminal coding Agent | `goalanchor install --target codex --scope user` (`CODEX_HOME` or `~/.codex`); project installs go to `.agents/skills`. Dispatch: `codex exec -`, model flag `-m` | Skill host + review executor; also offers a `codex app-server` controlled event channel | Skill + dispatch + controlled callbacks (native enforcement `LIMITED`) |
| **Claude Code** | Anthropic terminal coding Agent | `--target claude` (user `~/.claude`, project `.claude/skills`). Dispatch: `claude -p`, model flag `--model` | Skill host + review executor | Skill + dispatch |
| **Gemini CLI** | Google terminal Agent | `--target gemini` (user `~/.gemini`, project `.gemini/skills`). Dispatch: `gemini`, model flag `-m` | Skill host + review executor; commonly used for cross-model review | Skill + dispatch |
| **OpenCode** | Open-source terminal coding Agent | `--target opencode` (user `~/.config/opencode`, project `.opencode/skills`). Dispatch: `opencode run`, model flag `-m` | Skill host + review executor | Skill + dispatch |

All four built-in dispatchers pass the prompt over **stdin**, which avoids Windows `cmd.exe` mangling multi-line arguments and bypasses the command-line length limit. Install and sign in to each CLI separately; GoalAnchor does not install them or manage their credentials.

### 2. Skill-only hosts

These applications can install and use the Skill but have no built-in dispatcher. For independent review, let the host start its own sub-agents or configure a custom bridge.

| Application | Purpose | Integration | Role | Support level |
|---|---|---|---|---|
| **Cursor** | AI code editor | `--target cursor` (user `~/.cursor`, project `.cursor/skills`) | Skill host; review execution needs a custom command or JSON-over-stdio bridge | Skill only |
| **Pi CLI** | Pi terminal Agent | `--target pi` (user `~/.pi/agent`, project `.pi/skills`) | Skill host; no built-in dispatch | Skill only |
| **DeepSeek Harness (DSH)** | DeepSeek Agent harness | `--target dsh` (`DSH_HOME` or `~/.dsh`; project `.dsh/skills`) | Skill host; review execution needs a custom bridge | Skill only |
| **Zcode** | Zcode Agent environment | `--target zcode --destination <skill root>`; native discovery is host-configured, so the root is mandatory | Skill host; custom dispatch integration | Skill only (explicit directory) |
| **Agent Skills directory** | Shared root compatible with the Agent Skills convention | `--target agents` (user `~/.agents`, project `.agents/skills`) | Shared file location; dispatch comes from whichever host reads it | File placement only |

**Pi CLI and PI-Desktop are different products**: the former is a terminal Agent installed with `--target pi`; the latter is a desktop application that requires a separate plugin build.

### 3. Desktop plugin: PI-Desktop

| Item | Description |
|---|---|
| Purpose | Desktop AI Agent application with plugin, Skill, tool-registration, and session-extension support |
| Integration | `python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug`, then install the `.piplug` from the Plugins page. Do **not** use `--target pi` |
| Requested host permissions | `agent.prompt.inject` (Skill), `agent.extension` (hooks), `agent.tool.register` (tools), `agent.complete` (model quota), `session.read` (identity check) |
| Role | The only **tool-level** integration: the `goalanchor_review` tool, the `goalanchor_gate` tool, tool-approval hooks, and a session status command |
| Support level | Minimum public baseline 0.18.0; install, review, gating, and uninstall verified on 0.18.0 and 0.19.1 |

The plugin requires the host process to find Python; the Python core ships inside the plugin, so no separate `pip install` is needed. It does not read API keys and requests no network or file-write permissions. Host permissions are not an OS sandbox. See the [plugin guide](goalanchor/_pi_desktop/README.md), [compatibility specification](docs/PI-Desktop适配规格.md), and [verification record](docs/PI-Desktop验证记录.md).

### 4. Custom applications and generic bridges

Beyond the built-in dispatchers, any command that reads one JSON line from stdin and writes one JSON line to stdout can act as a review executor.

```json
{
  "platforms": {
    "my-agent": {
      "argv": ["python", "bridge.py", "{prompt}"],
      "timeout_seconds": 300
    }
  }
}
```

| Approach | When to use it | Key points |
|---|---|---|
| Custom platform (`platforms.<name>.argv`) | You already have a CLI that accepts a prompt via stdin or argv | At most one `{prompt}` placeholder; without it the prompt is read from stdin; `timeout_seconds` defaults to 600 |
| JSON-over-stdio bridge | The host has no CLI but can execute commands | One JSON request line in; exactly one JSON result line out |
| Host-native sub-agents | You want each role visible in the host UI | Render prompts with `goalanchor review-prompt --role ...`, let the host start sub-agents, then validate with `goalanchor gate --phase review-cycle` |
| Custom Skill root (`--target custom`) | The application supports Skills but not dispatch | Point `--destination` at the root; GoalAnchor appends `goalanchor/` |

### 5. Choosing an integration

| Your goal | Recommended integration |
|---|---|
| Just get PLAN / VERIFY / EXECUTE checks in your own Agent session | Install the Skill: `goalanchor install --target <host>` |
| Have reviewer / arbiter / meta-reviewer run independently on different models | Run `review-cycle` with the built-in dispatchers or custom `platforms` |
| See every role's conversation in the host UI | Render prompts with `review-prompt` for host-native sub-agents |
| Use PI-Desktop | Install the `.piplug` for tool registration and approval hooks |
| Need trusted authorization, a durable ledger, and execution receipts | Use `Workflow` / `StrictWorkflow` with your own trusted control plane |

### 6. Integration troubleshooting

| Symptom | Usual cause | What to do |
|---|---|---|
| `goalanchor platforms --json` reports `available: false` | The CLI is not installed or not on `PATH` | Install and sign in to that CLI, reopen the terminal, and recheck; on Windows the `.cmd` entry point must be resolvable |
| Skill is installed but the Agent never triggers it | The session was not refreshed, or the task does not meet the trigger condition | Start or refresh a session; confirm you are reviewing a formed plan/change/conclusion or explicitly asked for a review — ordinary Q&A does not trigger it |
| `review-cycle` exits with `2` | Configuration or runtime error | Check `platforms.<name>.argv`, the number of `{prompt}` placeholders, and `timeout_seconds` |
| `review-cycle` exits with `1` | Human decision required, or review already `STOPPED` | Read the verdict and objections, add evidence or authorization, and rerun; do not treat it as a pass |
| Sub-agent result cannot be parsed | Logs on stdout, or more than one JSON object | The bridge must write exactly one JSON line to stdout; send logs to stderr |
| PI-Desktop cannot find Python | No Python on the host process `PATH` | Set the plugin's Python field to an absolute path, or set `GOALANCHOR_PYTHON` before launch |
| PI-Desktop behaves unchanged after an edit | The host is still using cached modules | Fully quit and restart the desktop app, then reinitialize the session; approvals are not inherited |

### Support level definitions

| Level | Meaning |
|---|---|
| Skill + dispatch + controlled callbacks | Skill install, `review-cycle` dispatch, and host App Server event callbacks; native enforcement is still `LIMITED` |
| Skill + dispatch | Skill install and direct `review-cycle` dispatch |
| Skill only | Skill files only; review execution needs host-native sub-agents or a custom bridge |
| File placement only | Provides a directory location only; it does not imply a usable Agent runtime |

## Versioning and compatibility

- Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
- The current `0.9.x` line provides both legacy `Workflow` and `StrictWorkflow`; legacy objects, JSON, and historical empty-evidence behavior remain compatible.
- Legacy `ONE_TIME` authorization has known limits and must not be used as strict authorization or closure proof; use `Workflow` / `workflow` for trusted gating.
- Native host claims require per-host testing. Fixtures, controlled callbacks, and shared TextHostAdapter tests do not replace native acceptance.
- See [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) and [CHANGELOG](CHANGELOG.md) for published versions and assets.

### Upgrade and rollback

- **Upgrade the runtime**: download the new wheel from [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) and run `python -m pip install --upgrade <wheel file>`, or reinstall with `pip install .`; the project is not published to PyPI, so `pip install goalanchor` will not work. No third-party runtime dependencies are affected.
- **Upgrade the Skill**: `goalanchor install --target <host> --force` overwrites the previous files.
- **Upgrade the SQLite ledger**: the schema version lives in `PRAGMA user_version`. A v1 ledger is automatically extended to v2 (additive only; legacy records stay untouched) the first time a newer release opens it; an empty database is created directly at v2; unsupported newer versions are rejected. Strict features require a v2 ledger.
- **Rollback**: stop the processes using the ledger and back up the database file, then install the older wheel. How an older release reads a v2 ledger is defined by that release's code; for a full rollback, restore the backed-up v1 file.
- **Roll back the PI-Desktop plugin**: uninstall the old `.piplug` in the desktop app and install the target build; plugin and ledger versions are not coupled.

### Support window

- Only the **latest MINOR branch** (currently `0.9.x`) is maintained; fixes and security patches land in the newest release first.
- Older MINOR branches (such as `0.8.x`) receive no routine fixes; upgrade to the latest Release.
- Release cadence and asset verification are tracked on the [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) page; every release ships `SHA256SUMS.txt` and a `release-manifest.json`.

## Contributing

Issues and Pull Requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for branch strategy, commit titles, version rules, and local checks.

1. Create a short-lived branch from the latest `master`; do not develop directly on `master`.
2. Keep each commit focused and use Conventional Commit titles.
3. Run pytest, Ruff, Mypy, and the relevant scripts before submitting.
4. In the PR, state the objective, changes, verification results, and remaining risks. Do not commit credentials, personal data, or unauthorized large files.

Participation is subject to [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

Do not report vulnerabilities in public Issues, PRs, or discussions. Prefer **Security → Advisories → Report a vulnerability** in the GitHub repository. If unavailable, email [chenyuyu6666@gmail.com](mailto:chenyuyu6666@gmail.com) with the subject `GoalAnchor security report`. Include the affected version/commit, impact, and reproduction steps; never include live credentials or personal data. See [SECURITY.md](SECURITY.md).

### Trust boundary

GoalAnchor's gating runs inside a **local Python process**; models and Agents participate only through structured input:

| Party | May do | Must not do |
|---|---|---|
| Local core (trusted) | Runs Gate checks, evidence validation, ledger writes, signed-event verification, one-time authorization accounting | Never reads or exfiltrates your model credentials |
| Model / Agent (untrusted) | Produces conclusion text, review opinions, question answers, plan revisions | Cannot alter Gate verdicts, unlock EXECUTE blocks, or write the ledger; such inputs must pass Reviewer → Arbiter → Meta structured confirmation |
| Host application | Loads Skills, dispatches sub-Agents, enforces execution blocks | Self-reported fields (e.g. `isolated`, `agrees_with_arbiter`) cannot be verified by the core — a known boundary |
| Caller (trusted control plane) | Holds `EventAuthority` keys, the SQLite ledger, and execution receipts | Keys and the ledger must never be exposed to model or Agent CLI processes |

Practical requirements: the caller collects review evidence and (optionally) binds it via `evidence_digests`; execution blocking ultimately relies on the host, since GoalAnchor does not enforce process-level isolation outside the host process.

## License

GoalAnchor is released under the [MIT License](LICENSE). Third-party snapshots under `skills/graphify/` and `skills/.system/` retain their own licenses; the root MIT license does not relicense them. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md).

## More information

- [Strict runtime guide](docs/workflow-v0.8.md)
- [Agent compatibility](AGENT-COMPATIBILITY.md)
- [Offline multi-Agent review example](examples/multi-platform-review/README.md)
- [PI-Desktop plugin guide](goalanchor/_pi_desktop/README.md)
- [Repository documentation index](docs/README.md)
- [Testing guide](tests/README.md)
- [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) · [Gitee mirror](https://gitee.com/chen-yuyu6666/goal-anchor)
