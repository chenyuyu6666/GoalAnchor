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
- [Supported platforms](#supported-platforms)
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
| Release type | MINOR (backward-compatible capability) |
| GitHub Release | [v0.9.1](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.9.1) |
| Release commit | `dfafe47` |
| Core scenarios | 58/58 `PASS` |
| Native hosts | 58/58 `NOT_RUN` |

v0.9.1 adds the incremental `StrictWorkflow` API and the strict/legacy split. Strict review, questions, authorization, execution, reconcile, and closure still return `UNAVAILABLE`. P2b–P7 is incomplete; do not treat this release as a complete strict loop. See [CHANGELOG](CHANGELOG.md).

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

After installation, create or refresh a host session and ask the Agent to review a plan, change, or conclusion. A Skill install target only controls where files are placed; it does not imply built-in Agent dispatch. See [Supported platforms](#supported-platforms).

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

# Strict workflow capabilities and local demo
goalanchor workflow capabilities
goalanchor workflow demo --directory ./workflow-demo-v08
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
├── delivery/                 published artifacts (v0.8.0, v0.9.1)
├── .github/ .workflow/       GitHub Actions / Gitee Go pipelines
├── pyproject.toml            package metadata, dependencies, Ruff/Mypy/pytest config
├── requirements-dev.txt      development and validation dependencies
├── requirements-image.txt    optional ImageGen dependencies
├── CONTRIBUTING.md           contribution and Git rules
├── SECURITY.md               security reporting process
├── CODE_OF_CONDUCT.md        code of conduct
├── CHANGELOG.md              release history
├── LICENSE                   MIT license
├── THIRD_PARTY_NOTICES.md    third-party notices
├── UPSTREAM_SOURCES.md       third-party Skill sources and sync rules
└── README.md / README.en.md  Chinese/English documentation
```

`build/`, `.venv/`, caches, and `test-results/` are local generated files, not source structure.

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

`requirements-dev.txt` contains pytest and YAML validation dependencies. `requirements-image.txt` is optional for the explicit ImageGen CLI fallback and is not required to run GoalAnchor.

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

## Supported platforms

The table separates Skill file placement, built-in Agent dispatch, and the desktop plugin. **A Skill install target only controls file placement; it does not imply native dispatch support.**

| Platform / host | Skill target | Dispatch or plugin path |
|---|---|---|
| Codex CLI | `codex` | Built-in CLI dispatcher; install and sign in to Codex CLI separately |
| Claude Code | `claude` | Built-in CLI dispatcher; install and sign in to Claude CLI separately |
| Gemini CLI | `gemini` | Built-in CLI dispatcher; install and sign in to Gemini CLI separately |
| OpenCode | `opencode` | Built-in CLI dispatcher; install and sign in to OpenCode CLI separately |
| Cursor | `cursor` | Skill install; Agent execution needs a custom command or JSON-over-stdio bridge |
| Pi CLI | `pi` | Skill install; this is Pi CLI, not PI-Desktop |
| DeepSeek Harness (DSH) | `dsh` | Skill install; Agent execution needs a custom command or bridge |
| Agent Skills directory | `agents` | Shared Skill directory; dispatch comes from the host or custom config |
| Zcode | `zcode` plus `--destination` | Skill directory required; custom dispatch integration |
| Other hosts | `custom` plus `--destination` | Custom CLI command or JSON-over-stdio Agent |
| PI-Desktop | separate `.piplug` build | Extension and Python bridge; do not use `--target pi` |

Built-in dispatchers are Codex, Claude, Gemini, and OpenCode. PI-Desktop uses a separate plugin and detects capabilities through public host interfaces. See the [compatibility specification](docs/PI-Desktop适配规格.md) and [verification record](docs/PI-Desktop验证记录.md).

## Versioning and compatibility

- Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
- `0.9.1` is a MINOR release: the incremental `StrictWorkflow` API is added and legacy `Workflow` remains compatible.
- Legacy `ONE_TIME` authorization has known limits and must not be used as strict authorization or closure proof; use `Workflow` / `workflow` for trusted gating.
- Native host claims require per-host testing. Fixtures, controlled callbacks, and shared TextHostAdapter tests do not replace native acceptance.
- See [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) and [CHANGELOG](CHANGELOG.md) for published versions and assets.

## Contributing

Issues and Pull Requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for branch strategy, commit titles, version rules, and local checks.

1. Create a short-lived branch from the latest `master`; do not develop directly on `master`.
2. Keep each commit focused and use Conventional Commit titles.
3. Run pytest, Ruff, Mypy, and the relevant scripts before submitting.
4. In the PR, state the objective, changes, verification results, and remaining risks. Do not commit credentials, personal data, or unauthorized large files.

Participation is subject to [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Security

Do not report vulnerabilities in public Issues, PRs, or discussions. Prefer **Security → Advisories → Report a vulnerability** in the GitHub repository. If unavailable, email [chenyuyu6666@gmail.com](mailto:chenyuyu6666@gmail.com) with the subject `GoalAnchor security report`. Include the affected version/commit, impact, and reproduction steps; never include live credentials or personal data. See [SECURITY.md](SECURITY.md).

## License

GoalAnchor is released under the [MIT License](LICENSE). Third-party snapshots under `skills/graphify/` and `skills/.system/` retain their own licenses; the root MIT license does not relicense them. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md).

## More information

- [Strict runtime guide](docs/workflow-v0.8.md)
- [Offline multi-Agent review example](examples/multi-platform-review/README.md)
- [PI-Desktop plugin guide](goalanchor/_pi_desktop/README.md)
- [Repository documentation index](docs/README.md)
- [Testing guide](tests/README.md)
- [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) · [Gitee mirror](https://gitee.com/chen-yuyu6666/goal-anchor)
