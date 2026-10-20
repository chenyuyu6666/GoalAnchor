# Skill 目录说明

本目录包含 GoalAnchor 自己的可安装 Skill，以及随仓库分发的第三方 Skill 快照。根目录 MIT 许可证不自动覆盖第三方内容。

## 内容与归属

| 目录 | 文件数 | 归属 | 说明 |
|---|---|---|---|
| `goalanchor/` | 4 | 项目维护 | 产品 Skill：`SKILL.md`、`agents/openai.yaml`、`references/agent-runtime.md`、`references/pi-desktop.md`。它是 `goalanchor/_skill/` 包内副本的单一来源。 |
| `deeply/` | 1 | 项目维护 | 深度阅读/分析类 Skill。 |
| `graphify/` | 13 | 上游快照 | 基于 Graphify v0.9.45（Apache-2.0），保留 `LICENSE`、`LICENSE-MIT`、`NOTICE`。 |
| `.system/` | 65 | 上游快照 | 六个 Codex 系统 Skill：imagegen、openai-docs、plugin-creator、review-agent、skill-creator、skill-installer。Apache-2.0，逐目录保留许可证。 |

第三方来源、固定提交和本地改动记录见仓库根 [UPSTREAM_SOURCES.md](../UPSTREAM_SOURCES.md)。

## 同步与校验

产品 Skill 会打入 wheel 的 `goalanchor/_skill/`。修改 `skills/goalanchor/` 后必须同步包内副本：

```powershell
python scripts/sync_skill_bundle.py --write
python scripts/sync_skill_bundle.py --check
```

`tests/test_skill_metadata.py` 会校验目录结构与 YAML 元数据；CI 的 `sync --check` 会阻止副本漂移。

## 修改规则

- 优先在项目维护的 `goalanchor/`、`deeply/` 中实现新行为，不直接改上游快照。
- 如必须修改第三方文件，在文件内加显著修改说明，并同步更新 `UPSTREAM_SOURCES.md` 与提交信息。
- 更新上游快照前，先与固定提交做全目录对比，保留许可证与 NOTICE，再运行 pytest、Skill 校验和脚本帮助检查。
- 不要用根目录 MIT 许可证重新许可 Apache-2.0 内容。
- `skills/.system/` 不纳入 Ruff 门禁；不要为了让 lint 通过而改写上游文件。
