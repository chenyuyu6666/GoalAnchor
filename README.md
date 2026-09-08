# GoalAnchor

GoalAnchor 是面向复杂工程任务的目标锚定与可信收敛框架（Engineering Convergence Guard）。
它解决的是防漂移、防错误积累、防伪证据和防伪闭环，而不是如何产生更多分析。

核心原则：

> **Reduce decision uncertainty, not just accumulate information.**

## 当前版本

当前稳定版本为 `v0.3.2`，下一开发目标为 `v0.4.0`；日常开发从 `master` 创建
短期工作分支，运行时版本由 `goalanchor.__version__` 统一维护。

- `v0.1.0`：初始 skills 与 Plan Audit 基线；
- `v0.2.0`：加入 Plan Audit v2 目标与验证约束；
- `v0.3.0`：收紧 Plan Audit v3 诊断边界；
- `v0.3.1`：统一提交标题与版本管理规范；
- `v0.3.2`：简化多人协作流程。

版本说明见 [CHANGELOG.md](CHANGELOG.md)，分支、提交和发布流程见
[CONTRIBUTING.md](CONTRIBUTING.md)。

完整工程问题定义位于 `D:/Project/GoalAnchor/Doc/GoalAnchor-核心工程问题定义.md`。

## 当前仓库实现

本仓库当前包含 GoalAnchor 直接维护的 Codex Skills、v1 规划文档及配套脚本，不是独立的 Web 服务或业务应用。
Skill 的行为由 `SKILL.md` 定义；阶段 1 已提供无第三方运行时依赖的 `goalanchor/` Python 契约与状态模型，阶段 2 已增加 Q1–Q5、VERIFY、EXECUTE 和 PLAN_CHANGED 闸门，阶段 3 已增加证据强度与实验可信度校验，阶段 4 已增加交付闭环/provenance，阶段 5 已增加 Review/Graphify 结果适配。

## 多 Agent 兼容

GoalAnchor 的规则入口使用标准 `SKILL.md`，可按目标安装到 Codex、Claude Code、Pi、DSH 和通用 Agent Skills 根目录，并为 Cursor、Gemini CLI、OpenCode 提供目录投影。安装与检查命令见 [AGENT-COMPATIBILITY.md](AGENT-COMPATIBILITY.md)，例如：

```text
python -m goalanchor install --target claude --scope project --project-root . --source skills/goalanchor --dry-run --json
```

规则正文不依赖宿主专用工具或 API；宿主差异只由目录映射和可选界面元数据处理。

## v1 交付边界

- 防漂移：局部 blocker 不得替代最终交付目标；
- 防错误积累：Observation、Fact、Hypothesis、Constraint、Inference 和 Design Choice 不得无证据升级；
- 防伪证据：结论强度不得超过证据强度；
- 防伪闭环：Containment、Root Fix、Build Success、Artifact Verified 和 Product Closure 不得相互替代；
- 不建设 Web 服务、数据库、常驻后台或通用项目管理平台。

阶段计划、接口决策和验收规则位于：

- `D:/Project/GoalAnchor/Doc/GoalAnchor-任务计划.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-核心工程问题定义.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-项目定位.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-术语表.md`
- `D:/Project/GoalAnchor/Doc/ADR-0001-GoalAnchor-v1接口.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-v1用户场景.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段1-契约与状态模型.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段2-闸门.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段3-证据与实验.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段4-闭环与Provenance.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段5-工具集成.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-阶段6-测试与回归.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-效果验证协议.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-P0多平台兼容矩阵.md`
- `D:/Project/GoalAnchor/Doc/GoalAnchor-设计-基于性能对话案例.md`

## Skill 目录

- `skills/goalanchor`：GoalAnchor 的规则入口，对外部计划、补丁和代理结论执行 `PLAN -> VERIFY -> EXECUTE` 审核。
- `skills/graphify`：将代码和资料整理为可查询的知识图谱。
- `skills/deeply`：检索财经、科技、商业和思想领域的一手观点。
- `skills/.system/imagegen`：生成和编辑位图、抠图及背景处理。
- `skills/.system/openai-docs`：查询和引用官方 OpenAI/Codex 文档。
- `skills/.system/plugin-creator`：创建和校验 Codex 插件。
- `skills/.system/skill-creator`：创建和校验 Codex Skill。
- `skills/.system/skill-installer`：从官方或 GitHub 仓库安装 Skill。
- `skills/.system/review-agent`：只读审查代码修改并返回分级问题。

`.system` 下的目录是上游系统 Skill 的快照；本项目的定制 Skill 放在同级的非 `.system` 目录中。
除非同步上游版本，否则不要直接修改 `.system` 中的 `SKILL.md` 正文；必要的脚本补丁须记录在 [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md) 中。

## 本地验证

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

文档驱动的完整验收任务：

```powershell
.\.venv\Scripts\python.exe scripts/run_test_task.py --task docs/GoalAnchor-自动测试任务.md
```

测试任务定义、支持的检查类型和 JSON 报告格式见
[GoalAnchor-自动测试任务.md](docs/GoalAnchor-自动测试任务.md)。

对话体验测试不是 Python 单元测试。请在实际 GoalAnchor 对话上下文中读取并执行
[GoalAnchor-对话体验测试任务.md](docs/GoalAnchor-对话体验测试任务.md)，它会按固定案例检查目标保持、证据边界、决策链接、状态切换和不过度拦截，并输出 `DIALOGUE_TEST_REPORT`。

ImageGen 的显式 CLI 回退模式另需：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-image.txt
```

图片生成还需要 `OPENAI_API_KEY`；`--dry-run` 不需要 API 调用。OpenAI Docs 脚本需要 Node.js 18 或更高版本。
Graphify 的运行时版本按 `skills/graphify/.graphify_version` 管理，Deeply 需要配置 `DEEPLY_TOKEN`。

## 知识图谱

当前项目图谱保存在仓库外的
`D:/Project/GoalAnchor/Doc/Project-Code-Knowledge-Graph/graphify-out/`，包括交互式 HTML、JSON 数据和分析报告。
该目录不属于本仓库 Git 根目录；如需版本化，应为文档目录建立独立仓库，或在本仓库保存指向它的版本清单。

## 开发与提交

使用 `feature/*`、`fix/*` 或 `chore/*` 分支开发。所有后续提交标题统一使用
`vX.Y.Z 中文变更说明` 格式，例如
`v0.3.0 收紧 Plan Audit v3 诊断边界`；版本号必须与对应标签一致。
日常开发提交使用 `type(scope): 中文说明`，只有发布聚合提交带版本号。
完整规范和发布示例见 [CONTRIBUTING.md](CONTRIBUTING.md)。
