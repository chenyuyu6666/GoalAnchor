# Upstream sources and synchronization

## Scope and snapshot history

`skills/.system/` contains six Codex system-skill snapshots. They were imported from the installed Codex system-skill distribution in GoalAnchor commit [`e72e657`](https://github.com/chenyuyu6666/goal-anchor/commit/e72e65789d46ee4cda7d2f5bd4ada5a0f3ccad38) on 2026-08-29. The exact upstream Git commits used by that installed distribution were not recorded. The pinned commits below are the latest public upstream commits affecting each path on or before the import date; they are reproducible reference baselines, not a claim that the local files are byte-for-byte identical.

The imported system snapshots differ from the public reference commits below; that difference alone does not establish a GoalAnchor-authored modification. The GoalAnchor-specific edits made after import are listed in the table and marked in each affected file. Review the full local diff before any future upstream sync; do not overwrite a complete skill directory based only on its name. `deeply` and `goalanchor` are project-maintained skills; `graphify` is an upstream snapshot recorded below.

## Graphify skill snapshot

`skills/graphify/` is based on [Graphify v0.9.45](https://github.com/Graphify-Labs/graphify/tree/v0.9.45), tag commit [`0738af3`](https://github.com/Graphify-Labs/graphify/commit/0738af373af9cf5c95f862cc5f3327fd96b4ea23), also identified as package version `graphifyy 0.9.45` in the local `.graphify_version` file. It is licensed under Apache-2.0. The upstream `LICENSE`, `LICENSE-MIT`, and `NOTICE` are retained in `skills/graphify/` because the NOTICE identifies earlier MIT-licensed contributions.

The eight local files in `skills/graphify/references/` match the upstream Codex reference files at this tag. The local `SKILL.md` contains a Codex-specific adaptation in the semantic-extraction dispatch instructions: it uses Codex's `spawn_agent`/`wait_agent` flow instead of the generic Agent tool. The root MIT license does not apply to this snapshot.

## Snapshot inventory

| Local skill | Public upstream reference | License evidence | Known local state |
|---|---|---|---|
| `imagegen` | [openai/skills `e6afb0d`](https://github.com/openai/skills/tree/e6afb0d74cc75d220df2faf3dd6c635c2dc6a108/skills/.system/imagegen) | Apache-2.0; retain the per-skill `LICENSE.txt` | The imported Codex system snapshot differs from this public reference; its exact system-distribution SHA is not retained. No post-import GoalAnchor patch is recorded. |
| `openai-docs` | [openai/skills `972cb86`](https://github.com/openai/skills/tree/972cb867affac58fda9afa76bb1a19b399a278cf/skills/.system/openai-docs) | Apache-2.0; retain the per-skill `LICENSE.txt` | The imported Codex system snapshot includes Codex-specific references and scripts and differs from this public reference; its exact system-distribution SHA is not retained. No post-import GoalAnchor patch is recorded. |
| `plugin-creator` | [openai/skills `e940b8a`](https://github.com/openai/skills/tree/e940b8a86138adf03972802b990a1dfc57fcbf09/skills/.system/plugin-creator) | Apache-2.0; the upstream `LICENSE.txt` is included locally | The imported Codex system snapshot contains additional plugin lifecycle helpers and differs from this public reference; its exact system-distribution SHA is not retained. No post-import GoalAnchor patch is recorded. |
| `review-agent` | [openai/codex `83a4187`](https://github.com/openai/codex/tree/83a418783707f4446aa832b2799d6cacfef75011/codex-rs/skills/src/assets/samples/review-agent) | Apache-2.0 under the upstream repository license; its full text is included locally | The `SKILL.md` matches this reference; `agents/openai.yaml` is local package metadata. |
| `skill-creator` | [openai/skills `4ab6e0f`](https://github.com/openai/skills/tree/4ab6e0fd99c6667163bc34173e3ed3a3fed75ebc/skills/.system/skill-creator) | Apache-2.0; retain the per-skill license (`license.txt` locally) | GoalAnchor adds Windows-safe UTF-8 I/O in `scripts/generate_openai_yaml.py`, `scripts/init_skill.py`, and `scripts/quick_validate.py`; each file carries a modification notice. |
| `skill-installer` | [openai/skills `49f948f`](https://github.com/openai/skills/tree/49f948faa9258a0c61caceaf225e179651397431/skills/.system/skill-installer) | Apache-2.0; retain the per-skill `LICENSE.txt` | GoalAnchor adds bounded GitHub/Git operations and readable error handling in `scripts/github_utils.py`, `scripts/install-skill-from-github.py`, and `scripts/list-skills.py`; each file carries a modification notice. |

The OpenAI skills catalog states that each skill's license is in that skill directory. The OpenAI Codex repository identifies its repository license as Apache-2.0. Copies of the required license texts are retained in the local skill directories and indexed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Maintenance rules

- Keep GoalAnchor-authored core code, docs, and the `deeply`/`goalanchor` skills under the root MIT license; Graphify and Codex system snapshots remain under their upstream licenses.
- Prefer new custom behavior in project-maintained skills instead of modifying upstream snapshots.
- When a compatibility or security change must modify a third-party file, add a prominent change notice in that file, record its path and behavior here, and repeat the scope in the commit message.
- Before updating a snapshot, compare the complete directory with its pinned upstream reference, preserve its license and attribution files, and run `python -m pytest -q` plus the relevant skill validators and script help checks.
- Never interpret the root MIT license as replacing or relicensing the bundled Apache-2.0 material.
