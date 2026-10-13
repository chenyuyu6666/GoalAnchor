# GoalAnchor

**中文** | [English](README.en.md)

> **Reduce decision uncertainty, not just accumulate information.**

GoalAnchor 是面向复杂工程任务的**目标锚定与可信收敛框架**（Engineering Convergence Guard）。
它拦截四类典型失败——目标漂移、错误积累、伪证据、伪闭环——而不是产生更多分析。

| | |
|---|---|
| 当前版本 | [`v0.6.0`](CHANGELOG.md) |
| Python 要求 | 3.11+（运行时**零第三方依赖**；CI 验证 3.11/3.12/3.13） |
| 分发形态 | 标准 `SKILL.md` + Python 契约库 + CLI |
| 宿主支持 | Codex、Claude Code、DSH、Zcode、Pi CLI、PI-Desktop、Cursor、Gemini CLI、OpenCode 等 |

---

## 它解决什么问题

复杂工程任务在收尾阶段最常见的失败，不是"分析不够多"，而是：

- **目标漂移** —— 局部 blocker 被当成最终目标，原任务被悄悄替换；
- **错误积累** —— Observation、Hypothesis、Inference、Design Choice 之间无证据升级；
- **伪证据** —— 结论强度超过证据强度（单测通过 ≠ 根因已修复）；
- **伪闭环** —— Containment、Root Fix、Build Success、Product Closure 被互相冒充。

GoalAnchor 用**契约 + 确定性闸门 + 独立审核**把这些环节固定为可核验的状态，而不是依赖执行者的自觉。

## 核心能力

### 1. 契约模型与确定性闸门

- 任务契约（Goal / Prompt / Constraints / Evidence / Candidate）与生命周期状态机；
- `PLAN → VERIFY → EXECUTE` 三段门控：PLAN 阶段 Q1–Q5 静态检查（目标、逻辑、假设、最小路径、结构），VERIFY 阶段要求先固定可复现的验收阈值再行动；
- 证据强度与实验校验、交付闭环与 provenance 记录、决策与轻量授权。

### 2. 有限多 Agent 审核服务

- 多个 **Reviewer 共享同一不可变上下文**并行独立审核，主 Agent（Arbiter）汇总裁决；
- 仲裁被反驳时生成争议包（DisputePacket）做同上下文复审，独立 **Meta Reviewer** 审核仲裁本身；
- `max_rounds=2` 封顶：无法收敛即 `STOPPED`，并产出 DecisionReport 交用户决策。
- 风险 → Reviewer 数量：`LOW=1 / MEDIUM=2 / HIGH=2 / CRITICAL=3`；CRITICAL 需签署风险声明。

### 3. 多平台执行与模型一致性

- 内置 `codex` / `claude` / `gemini` / `opencode` 适配，提示词经 stdin 传入（规避 Windows `.cmd` 转义与 argv 长度限制）；
- **模型一致性强制**：子 Agent 必须与主 Agent 同模型——顶层 `"model"` 或 `--model` 声明主模型，按平台 flag 注入（`codex -m`、`claude --model` 等）；不一致的 profile 在**配置加载期**拒绝，不启动任何 Agent；确需分歧须先经用户确认并记录 `model_divergence`；
- argv 模板预检：带自有取值的模型 flag（`-m preset`、`--model=x`）在配置加载期拒绝；
- 宿主原生子 Agent 会话在宿主 UI 可见；无头平台调度是备用路径。

### 4. 机器可读审计

- 顶层 `warnings` 告警数组：`MODEL_NOT_PINNED`（主模型未声明、一致性不可强制）与 `MODEL_DIVERGENCE_APPROVED`（用户已确认的分歧），供只读 stdout 的 CI/流水线审计；
- `agent_bindings` 记录每次调用的 agent / profile / platform / model / round，`usage_summary` 汇总模型与 token 用量；
- `platforms --probe` 按与真实派发相同的优先级（profile > 平台 > 顶层）做环境探测。

> 边界说明：紧贴短选项（如 `-mopus`）因无法与同前缀无关选项（如 `-max-tokens`）区分，**不在检测范围内**；不要把取值紧贴模型 flag。

## CLI 一览

| 命令 | 用途 |
|---|---|
| `python -m goalanchor install` / `check` | 把 Skill 安装到各宿主目录 / 校验安装一致性 |
| `python -m goalanchor gate --phase …` | 确定性闸门（不调用 LLM） |
| `python -m goalanchor review-cycle` | 完整多 Agent 调度（别名 `review-run`） |
| `python -m goalanchor review-prompt` | 逐角色渲染审核提示词 |
| `python -m goalanchor platforms` | 平台环境自检（`--probe` 探测，`--config` 报告 profile） |

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

把 Skill 安装到当前项目（以 Claude 目录为例，先 `--dry-run` 预览）：

```text
python -m goalanchor install --target claude --scope project --project-root . --source skills/goalanchor --dry-run
```

运行一次多 Agent 审核：

```text
python -m goalanchor review-cycle --request review-request.json --config agent-config.json
```

可直接运行的离线示例（echo 平台，不消耗额度）：[`examples/multi-platform-review/`](examples/multi-platform-review/)。

### PI-Desktop

桌面版使用独立的 Skill/Extension 插件包，支持同模型独立审核、明确启用的工具门控和能力诊断。以 0.18.0 为最低接口基线，后续版本按公开 API 能力检测。构建可导入目录与包：

```text
python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug
```

在 PI-Desktop 的 Plugins 页安装该包并审核权限；不使用 `install --target pi` 代替桌面安装。首次生成后再构建应换一个新目录/文件，构建器不覆盖已有产物。完整说明见 [桌面插件指南](goalanchor/_pi_desktop/README.md) 与 [适配规格](docs/PI-Desktop适配规格.md)。

独立审核通过宿主 completion 执行；没有可见子会话。已实测 0.18.0 的安装/LOW 审核与 0.19.1 的审核、门控、Stop 和卸载；范围和限制见 [验证记录](docs/PI-Desktop验证记录.md)。同版本重装后应完整退出并重启宿主，避免旧模块缓存。开发桌面测试需要 Node.js 20+。

## 仓库结构

```text
goalanchor/   零第三方依赖的 Python 契约库与 CLI
skills/       goalanchor / graphify / deeply 定制 Skill,.system/ 为上游快照
scripts/      压测、基准、测试任务脚本
tests/        全量单元测试
examples/     可运行的离线示例
docs/         仓库内文档资产(随包版本化:自动测试任务、对话体验测试)
```

注意两个文档位置的分工，避免混淆：

- **`docs/`（仓库内，小写）**：参与 Git 版本控制、随代码一起发布的文档资产，供脚本与 CI 引用；
- **`Doc/`（仓库旁，大写）**：独立于本仓库的设计/规格/记录中心（任务计划、ADR、逐次变更记录、知识图谱），不随包发布，入口见其 `README.md`。

`.system/` 下为上游 Skill 快照，除非同步上游否则不直接修改，补丁记录在 [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md)。

## 文档中心

完整的设计决策、开发规格、任务记录与知识图谱位于仓库旁的 `Doc/` 目录，入口为其 `README.md`。关键文档：

- `Doc/00_总览与入口/`：任务计划、核心工程问题定义、项目定位、术语表；
- `Doc/01_设计与决策/`：ADR-0001 接口决策、P0 兼容矩阵、效果验证协议；
- `Doc/02_开发规格/`：六阶段规格与 v0.5.0 规格；
- `Doc/03_验证与测试/`、`Doc/04_任务记录/`：验证证据与逐次变更记录。

## 验证工具链

```powershell
# 全量回归
.\.venv\Scripts\python.exe -m pytest                                 # 206 passed
# 模型一致性压测(全离线)
.\.venv\Scripts\python.exe scripts\stress_model_sync.py             # 739 项校验
# 文档一致性校验
pwsh Doc\03_验证与测试\validate-stage-0.ps1
pwsh Doc\03_验证与测试\validate-multi-round.ps1 -Rounds 2
```

对话体验测试（非单元测试）：在真实 GoalAnchor 对话中执行 [docs/GoalAnchor-对话体验测试任务.md](docs/GoalAnchor-对话体验测试任务.md)。

## 知识图谱

项目图谱位于 `Doc/GoalAnchor-Knowledge-Graph/graphify-out/`（交互 HTML / JSON / 分析报告）；
代码更新后在仓库根运行 `graphify update .` 重建。

## 参与贡献

开发、Git 提交与维护更新**必须遵循** [docs/开发Git提交与维护更新规范.md](docs/开发Git提交与维护更新规范.md)，要点：

- **分支**：从最新 `master` 创建短期分支（`feature/`、`fix/`、`refactor/`、`docs/`、`test/`、`chore/`、`hotfix/`），不直接在稳定分支开发；
- **提交**：日常提交 `type(scope): 说明`（feat / fix / docs / test / refactor / chore）；涉及逻辑的提交正文包含影响范围、关键决策、验证结果与剩余风险；
- **提交拓扑**：历史保持「工作提交 → 合并提交 → `vX.Y.Z` 发布聚合提交 → 同名标签」的固定结构；已发布的提交、合并节点和标签不移动、不复用、不删除；
- **版本**：版本号只在发布聚合提交更新；发布后同步 CHANGELOG、README、CONTRIBUTING 的版本引用；
- **验证**：提交前至少通过 `pytest -q`、`ruff check goalanchor tests scripts`、`git diff --check`，按变更范围追加专项验证并在正文记录实际命令与结果；
- **回滚**：未发布问题用反向/修复提交恢复；已发布版本发新补丁版本，不移动旧标签；
- **AI 协作**：AI 的开发、提交与推送操作边界同样在该规范中约定（远端写操作需明确授权），交付须按固定格式报告。

仓库级细则以 [CONTRIBUTING.md](CONTRIBUTING.md)、[CHANGELOG.md](CHANGELOG.md) 与 `pyproject.toml` 为准，与上述规范冲突时先修正规范来源；宿主适配见 [AGENT-COMPATIBILITY.md](AGENT-COMPATIBILITY.md)。
