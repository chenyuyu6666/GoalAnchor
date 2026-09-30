# GoalAnchor

[中文](README.md) | **English**

> **Reduce decision uncertainty, not just accumulate information.**

GoalAnchor is a **goal-anchoring and trustworthy-convergence framework** for complex engineering tasks (an Engineering Convergence Guard).
It blocks four recurring failure modes — goal drift, error accumulation, fake evidence, and fake closure — rather than producing yet more analysis.

| | |
|---|---|
| Current version | [`v0.5.1`](CHANGELOG.md) |
| Requires | Python 3.11+ (**zero third-party runtime dependencies**; CI runs 3.11/3.12/3.13) |
| Distribution | A standard `SKILL.md`, a Python contract library, and a CLI |
| Supported hosts | Codex, Claude Code, DSH, Zcode, Pi, Cursor, Gemini CLI, OpenCode, and more |

---

## What problem it solves

The most common failures when complex engineering tasks wrap up are not "not enough analysis" but:

- **Goal drift** — a local blocker is treated as the end goal and the original task is silently replaced;
- **Error accumulation** — undocumented upgrades between Observations, Hypotheses, Inferences, and Design Choices;
- **Fake evidence** — conclusion strength exceeds evidence strength (tests pass ≠ the root cause is fixed);
- **Fake closure** — Containment, Root Fix, Build Success, and Product Closure are passed off as one another.

GoalAnchor turns these checkpoints into verifiable state using **contracts, deterministic gates, and independent review**, instead of relying on the executor's diligence.

## Core capabilities

### 1. Contract model and deterministic gates

- Task contracts (Goal / Prompt / Constraints / Evidence / Candidate) and a lifecycle state machine;
- `PLAN → VERIFY → EXECUTE` gates: PLAN runs static checks Q1–Q5 (goal, logic, assumptions, minimal path, structure); VERIFY requires a reproducible acceptance threshold to be fixed before acting;
- Evidence strength and experiment validation, delivery closure with provenance, decisions and lightweight authorization.

### 2. Bounded multi-Agent review service

- Multiple **Reviewers share one immutable context** and review independently in parallel; the main Agent (the Arbiter) aggregates the verdict;
- If the arbiter is challenged, a DisputePacket drives a same-context re-review, and an independent **Meta Reviewer** audits the arbitration itself;
- Capped at `max_rounds=2`: if convergence fails, the result is `STOPPED` with a DecisionReport for the user.
- Risk → reviewer count: `LOW=1 / MEDIUM=2 / HIGH=2 / CRITICAL=3`; CRITICAL requires a signed risk declaration.

### 3. Multi-platform execution with model consistency

- Built-in adapters for `codex` / `claude` / `gemini` / `opencode`; prompts go through stdin (avoiding Windows `.cmd` escaping and argv-length limits);
- **Enforced model consistency**: sub-agents must run on the main agent's model — declare it via the top-level `"model"` key or `--model`, and it is injected with each platform's flag (`codex -m`, `claude --model`, etc.); profiles that differ are rejected at **config-load time**, before any agent launches; a genuine divergence needs prior user approval recorded as `model_divergence`;
- argv template preflight: a model flag carrying its own literal value (`-m preset`, `--model=x`) is rejected at config load;
- Host-native sub-agent sessions stay visible in the host UI; headless platform dispatch is the fallback path.

### 4. Machine-readable auditing

- A top-level `warnings` array: `MODEL_NOT_PINNED` (main model undeclared; consistency cannot be enforced) and `MODEL_DIVERGENCE_APPROVED` (a user-approved divergence), so stdout-only CI/pipelines can audit the run;
- `agent_bindings` records the agent / profile / platform / model / round of every call; `usage_summary` aggregates models and token usage;
- `platforms --probe` checks the environment using the same precedence as a real dispatch (profile > platform > top-level).

> Boundary: a glued short option (e.g. `-mopus`) cannot be distinguished from an unrelated option with the same prefix (e.g. `-max-tokens`), so it is **not detected**; never glue the value to the model flag.

## CLI reference

| Command | Purpose |
|---|---|
| `python -m goalanchor install` / `check` | Install the Skill into host directories / verify install integrity |
| `python -m goalanchor gate --phase …` | Deterministic gates (no LLM calls) |
| `python -m goalanchor review-cycle` | Full multi-Agent dispatch (alias `review-run`) |
| `python -m goalanchor review-prompt` | Render per-role review prompts |
| `python -m goalanchor platforms` | Environment self-check (`--probe` to probe, `--config` to report profiles) |

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Install the Skill into the current project (Claude layout shown; preview with `--dry-run` first):

```text
python -m goalanchor install --target claude --scope project --project-root . --source skills/goalanchor --dry-run
```

Run a multi-Agent review:

```text
python -m goalanchor review-cycle --request review-request.json --config agent-config.json
```

A runnable offline example (an echo platform — no credits spent): [`examples/multi-platform-review/`](examples/multi-platform-review/).

## Repository layout

```text
goalanchor/   Python contract library and CLI with zero third-party dependencies
skills/       Custom Skills: goalanchor / graphify / deeply; .system/ holds upstream snapshots
scripts/      Stress, benchmark, and test-task scripts
tests/        Full unit-test suite
examples/     Runnable offline examples
docs/         In-repo documentation assets, versioned with the package
```

The two documentation locations have distinct roles:

- **`docs/` (in-repo, lowercase)**: version-controlled assets shipped with the code and referenced by scripts and CI;
- **`Doc/` (sibling directory, capitalized)**: a separate center for design/specs/records (task plan, ADRs, per-change logs, knowledge graph); not shipped with the package; enter through its `README.md`.

Everything under `.system/` is an upstream Skill snapshot: do not edit it directly except when syncing upstream; patches are logged in [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md).

## Documentation center

Design decisions, development specs, task records, and the knowledge graph live in the sibling `Doc/` directory, entered through its `README.md`. Key locations:

- `Doc/00_总览与入口/`: task plan, core engineering-problem definition, project positioning, glossary;
- `Doc/01_设计与决策/`: the ADR-0001 interface decision, the P0 compatibility matrix, the effect-validation protocol;
- `Doc/02_开发规格/`: the six phase specs and the v0.5.0 spec;
- `Doc/03_验证与测试/`, `Doc/04_任务记录/`: verification evidence and per-change records.

## Verification toolchain

```powershell
# Full regression
.\.venv\Scripts\python.exe -m pytest                                 # 206 passed
# Model-consistency stress (fully offline)
.\.venv\Scripts\python.exe scripts/stress_model_sync.py             # 739 checks
# Documentation consistency
pwsh Doc\03_验证与测试\validate-stage-0.ps1
pwsh Doc\03_验证与测试\validate-multi-round.ps1 -Rounds 2
```

The dialogue experience test is not a unit test: run [docs/GoalAnchor-对话体验测试任务.md](docs/GoalAnchor-对话体验测试任务.md) inside a real GoalAnchor conversation.

## Knowledge graph

The project graph lives at `Doc/GoalAnchor-Knowledge-Graph/graphify-out/` (interactive HTML / JSON / analysis report);
rebuild it from the repo root with `graphify update .` after code changes.

## Contributing

Use short-lived `feature/*`, `fix/*`, or `chore/*` branches; daily commits use `type(scope): description`, and only the release aggregate commit uses `vX.Y.Z` with a matching tag.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guidelines and [AGENT-COMPATIBILITY.md](AGENT-COMPATIBILITY.md) for host adaptation.
