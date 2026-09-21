# GoalAnchor

GoalAnchor 是面向复杂工程任务的目标锚定与可信收敛框架（Engineering Convergence Guard）。
它解决的是防漂移、防错误积累、防伪证据和防伪闭环，而不是如何产生更多分析。

核心原则：

> **Reduce decision uncertainty, not just accumulate information.**

## 当前版本

当前稳定版本为 `v0.4.1`；日常开发从 `master` 创建短期工作分支，运行时版本由
`goalanchor.__version__` 统一维护。

- `v0.1.0`：初始 skills 与 Plan Audit 基线；
- `v0.2.0`：加入 Plan Audit v2 目标与验证约束；
- `v0.3.0`：收紧 Plan Audit v3 诊断边界；
- `v0.3.1`：统一提交标题与版本管理规范；
- `v0.3.2`：简化多人协作流程。
- `v0.3.3`：按 GitHub Flow 重构协作管理。
- `v0.3.4`：明确 Git 提交规则。
- `v0.4.0`：多 Agent 审核协议、多平台 Agent 调度与治理收紧。
- `v0.4.1`：SKILL.md 门控边界澄清（量化验收标准归属 VERIFY 入口）。

版本说明见 [CHANGELOG.md](CHANGELOG.md)，分支、提交和发布流程见
[CONTRIBUTING.md](CONTRIBUTING.md)。

完整工程问题定义位于 `D:/Project/GoalAnchor/Doc/GoalAnchor-核心工程问题定义.md`。

## 当前仓库实现

本仓库包含 GoalAnchor 直接维护的 Agent Skills、契约库与配套脚本，不是独立的 Web 服务或业务应用。

`goalanchor/` 是零第三方运行时依赖的 Python 契约库，按阶段提供：契约与生命周期状态模型、Q1–Q5/VERIFY/EXECUTE/PLAN_CHANGED 闸门、证据强度与实验校验、交付闭环与 provenance、Review/Graphify 结果适配、五 Gate 治理映射、决策与轻量授权、Verification 闭环，以及**有限多 Agent Review Service**——多 Reviewer 同上下文并行、主 Agent（Arbiter）汇总、争议包复审、独立 Meta Reviewer 审核、`max_rounds=2` 封顶后 `STOPPED` + DecisionReport 交用户决策。

执行层（`agent_runtime.py` + `platforms.py`）提供多平台 Agent 调度：内置 `codex`/`claude`/`gemini`/`opencode` 适配（提示词全部经 stdin 传入，规避 Windows `.cmd` 转义与 argv 长度限制），同一轮 Reviewer 强制不同 profile 并发，平台输出解析兼容直接 JSON、信封包装与正文夹带 JSON；支持瞬时失败重试（`retries`）、dry-run 预览、`platforms --probe` 环境与认证自检、token 用量审计（`tokens_used`/`usage_summary`）；提示词支持 `"language": "zh"` 中文模板。宿主原生子 Agent 拉起的会话在宿主 UI 可见（`review-prompt` 渲染 + `gate --phase review-cycle` 校验），无头平台调度为备用路径。

CLI 入口：`install` / `check`（Skill 安装）、`gate --phase …`（确定性闸门）、`review-cycle`（别名 `review-run`，完整调度）、`review-prompt`（逐角色提示词渲染）、`platforms`（环境自检）。

`v0.4.0` 尚未发布；持续开发任务与每次改动记录见 `D:/Project/GoalAnchor/Doc/GoalAnchor-开发任务与变更记录.md`。

## 多 Agent 兼容

GoalAnchor 的规则入口使用标准 `SKILL.md`，可按目标安装到 Codex、Claude Code、Pi、DSH 和通用 Agent Skills 根目录，并为 Cursor、Gemini CLI、OpenCode 提供目录投影。安装与检查命令见 [AGENT-COMPATIBILITY.md](AGENT-COMPATIBILITY.md)，例如：

```text
python -m goalanchor install --target claude --scope project --project-root . --source skills/goalanchor --dry-run --json
```

规则正文不依赖宿主专用工具或 API；宿主差异只由目录映射、可选界面元数据和执行层适配器处理。多 Agent 审核的调度协议、平台适配与配置格式见
[skills/goalanchor/references/agent-runtime.md](skills/goalanchor/references/agent-runtime.md)，
可运行示例见 [examples/multi-platform-review/](examples/multi-platform-review/)。

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

Phase 6 固定基准：

```powershell
.\.venv\Scripts\python.exe scripts/run_phase6_benchmark.py
```

多 Agent Review 使用统一 JSON-over-stdio 桥接协议：Reviewer 并行，Arbiter 与 Meta Reviewer
按轮次收敛。配置格式见 `skills/goalanchor/references/agent-runtime.md`，入口为：

```powershell
python -m goalanchor review-cycle --request review-request.json --config agent-config.json
```

基准说明和开发者最小示例见 `D:/Project/GoalAnchor/Doc/GoalAnchor-Phase6-Benchmark与开发者示例.md`。

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
`D:/Project/GoalAnchor/Doc/GoalAnchor-Knowledge-Graph/graphify-out/`，包括交互式 HTML、JSON 数据和分析报告，图谱根指向本仓库目录。
该目录不属于本仓库 Git 根目录；如需版本化，应为文档目录建立独立仓库，或在本仓库保存指向它的版本清单。代码更新后在该目录运行 `graphify update .` 刷新。

## 开发与提交

使用 `feature/*`、`fix/*` 或 `chore/*` 短期分支开发。日常提交使用
`type(scope): 中文说明`；只有发布聚合提交使用 `vX.Y.Z 中文变更说明`，并创建同名标签。
完整规范和发布示例见 [CONTRIBUTING.md](CONTRIBUTING.md)。
