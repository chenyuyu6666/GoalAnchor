# GoalAnchor

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/chenyuyu6666/GoalAnchor?label=release)](https://github.com/chenyuyu6666/GoalAnchor/releases)
[![Validate](https://github.com/chenyuyu6666/GoalAnchor/actions/workflows/validate.yml/badge.svg)](https://github.com/chenyuyu6666/GoalAnchor/actions/workflows/validate.yml)

**中文** | [English](README.en.md)

> 让复杂工程任务从“计划”走到可核验的执行与交付。

## 目录

- [项目简介](#项目简介)
- [主要功能](#主要功能)
- [当前版本](#当前版本)
- [运行环境](#运行环境)
- [安装与运行](#安装与运行)
- [使用说明](#使用说明)
- [配置](#配置)
- [项目结构](#项目结构)
- [依赖与开发](#依赖与开发)
- [核心术语](#核心术语)
- [支持的应用](#支持的应用)
- [版本与兼容性](#版本与兼容性)
- [贡献指南](#贡献指南)
- [安全](#安全)
- [许可证](#许可证)
- [更多资料](#更多资料)

## 项目简介

GoalAnchor 是供 AI Agent 和开发者使用的**目标锚定与可信收敛工具**，适用于实施计划、代码改动、实验结论和多 Agent 审核。它把关键步骤组织为 **PLAN → VERIFY → EXECUTE**，帮助发现目标漂移、未经验证的假设、证据不足、审核异议被忽略和过早宣称完成等问题。

它关注的不是“多收集信息”，而是**减少决策不确定性**：先固定目标和可观察的验收条件，再检查证据与假设，最后才允许执行与关闭。

GoalAnchor 提供 Skill、Python 库、命令行工具和 PI-Desktop 插件。它帮助检查计划与记录是否符合规则，但**不能替代真实测试、外部事实核验或工程判断**。

## 主要功能

- **目标与执行门控**：行动前检查目标、逻辑、假设、最小路径和方案结构；执行前固定可观察的验收条件。
- **证据与交付闭环**：记录证据来源、实验条件、结论强度和交付状态，避免把“测试通过”误当成“问题已解决”。
- **多 Agent 审核**：Reviewer 并行审查，Arbiter 汇总，Meta Reviewer 复核；结果包含可读取的角色、模型和轮次记录。
- **Skill 与 CLI**：把 GoalAnchor Skill 安装到支持的 Agent 宿主；用 CLI 执行确定性检查、查看平台配置或运行审核。
- **严格工作流**：可信控制侧签名事件、SQLite 一次性授权账本、结构化问答、执行回执和验收观察。
- **PI-Desktop 插件**：提供桌面插件、Python 规则桥接、模型审核和可主动开启的工具门控。

## 当前版本

| 项目 | 值 |
|---|---|
| 当前版本 | `0.9.1` |
| 发布日期 | 2026-10-20 |
| GitHub Release | [v0.9.1](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.9.1) |
| 发布提交 | `dfafe47` |
| 核心场景 | 58/58 `PASS` |
| 原生宿主 | 58/58 `NOT_RUN` |

- `StrictWorkflow` 的离线核心链已覆盖审核、授权问题、一次性 grant、受限执行、UNKNOWN/reconcile、观察和关闭；原生宿主仍按证据记录 `NOT_RUN`、`LIMITED`、`ADVISORY` 或 `UNAVAILABLE`。宿主验收、效率治理和完整 P2b–P7 交付仍未完成。版本历史见 [CHANGELOG](CHANGELOG.md)。

## 运行环境

- **Python 3.11 或更新版本**，支持 Windows、macOS 和 Linux。
- Python 运行时不依赖第三方包。
- 使用内置 CLI Agent 调度时，需要自行安装并登录对应的 Agent CLI；GoalAnchor 不安装这些 Agent，也不管理其凭据。
- PI-Desktop 插件需要宿主进程能找到 Python；桌面开发测试需要 Node.js 20 或更新版本。
- 构建或开发测试需要 `setuptools>=68`、`build`、`wheel`、`pytest`、`ruff`、`mypy==2.4.0`；详见[依赖与开发](#依赖与开发)。

## 安装与运行

### 从 GitHub 安装 CLI

```sh
git clone https://github.com/chenyuyu6666/GoalAnchor.git
cd GoalAnchor
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```sh
source .venv/bin/activate
```

安装并验证：

```sh
python -m pip install .
goalanchor --help
python -c "import goalanchor; print(goalanchor.__version__)"
```

也可以从 [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) 下载 wheel：

```sh
python -m pip install goalanchor-0.9.1-py3-none-any.whl
```

### 安装 GoalAnchor Skill

以 Codex 用户目录为例：

```sh
goalanchor install --target codex --scope user
```

安装到当前项目：

```sh
goalanchor install --target claude --scope project --project-root .
```

安装后新建或刷新宿主会话，再让 Agent 使用 GoalAnchor 检查已形成的计划、改动或结论。可选目标为 `agents`、`claude`、`codex`、`cursor`、`custom`、`dsh`、`gemini`、`opencode`、`pi`、`zcode`；Skill 安装目标只表示文件放置方式，不代表该宿主具备内置 Agent 调度。各应用的用途、接入方式与角色见[支持的应用](#支持的应用)。

### 安装 PI-Desktop 插件

PI-Desktop 使用独立 `.piplug` 插件，不使用 `--target pi`：

```sh
python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug
```

在 PI-Desktop 的 Plugins 页安装生成的 `.piplug`，按宿主界面审阅并启用权限。详细说明见 [PI-Desktop 插件指南](goalanchor/_pi_desktop/README.md)。

### 快速运行

```sh
# 查看命令与平台环境，不会启动 Agent
goalanchor --help
goalanchor platforms --json

# 离线多 Agent 审核示例（本地 echo Agent，不调用外部模型）
goalanchor review-cycle --request examples/multi-platform-review/request.json --config examples/multi-platform-review/config.json

# 只渲染第一轮 Reviewer 提示词，不调用任何 Agent（用于接入检查）
goalanchor review-cycle --request ... --config ... --dry-run

# 严格工作流能力与本地 demo
goalanchor workflow capabilities
goalanchor workflow demo --directory ./workflow-demo
```

## 使用说明

### 基本流程

1. **PLAN**：明确 `TASK_OBJECTIVE`、`CURRENT_BLOCKING_UNCERTAINTY`、拟执行步骤及其决策关联，依次检查 Q1 Goal、Q2 Logic、Q3 Path、Q4 Minimum、Q5 Structure。
2. **VERIFY**：先固定可观察的验收条件，检查证据来源、实验条件和结论强度。
3. **EXECUTE**：在授权与基线仍然有效时执行；保存回执和验收观察，满足全部条件后才能关闭目标。

在支持 Skill 的宿主中，可以直接这样开始：

> 请使用 GoalAnchor 审核这份实施方案。先明确 TASK_OBJECTIVE、CURRENT_BLOCKING_UNCERTAINTY 和 PROPOSED_STEP_AND_DECISION_LINK，再按 PLAN 阶段顺序检查 Q1–Q5。若通过，进入 VERIFY 并先固定验收条件，然后按 PLAN → VERIFY → EXECUTE 推进；报告未验证的事实。

### Python API

`ReviewContext` 可用 `evidence_digests` 绑定调用方采集的证据：

```python
from hashlib import sha256
from goalanchor import ReviewContext

context = ReviewContext(
    goal="审核候选修复",
    prompt="检查目标与证据",
    constraints=(),
    evidence_refs=("E-report",),
    candidate="候选方案",
    evidence_digests=(("E-report", sha256(b"frozen report").hexdigest()),),
)
```

非空摘要必须完整覆盖证据引用，每个引用只出现一次；摘要为 64 位十六进制 SHA-256。输入会冻结为 tuple、摘要转小写并按引用排序，支持 `to_dict` / `from_dict` 往返；内容摘要变化会使旧审核指纹失效。未提供摘要时，旧指纹和 JSON 格式保持兼容，但仍然只有引用绑定；核心不自动读文件或核实摘要真实性，调用方负责可信采集和执行前重核。

### 严格工作流

`StrictWorkflow` 是与旧 `Workflow` 并列的增量 API。它需要 `store`、`user_authority`、`observer_authority`、`policy` 四个位置参数，并通过关键字参数注入 `trusted_event_verifier` / `trusted_runtime_binding_provider`。模型或 Agent 不能提供 principal、session 或 host facts。可用 `goalanchor workflow demo --directory <新目录>` 在本地离线体验完整门控链路；信任边界要点见下文[安全](#安全)一节。

旧 `Workflow` 的对象、JSON 和历史空证据行为保持兼容；旧审核、旧 grant、legacy `CLOSED` 和 legacy `VerificationResult` 不会变成 Strict 证明。

### 审核请求与输出

多 Agent 审核命令 `goalanchor review-cycle --request request.json --config config.json` 的请求文件包含以下字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `context` | 是 | 审核上下文：`goal`（目标）、`prompt`（审核要求）、`constraints`（约束列表）、`evidence_refs`（证据引用）、`candidate`（被审方案）；可选 `evidence_digests` 绑定证据内容。 |
| `risk` | 是 | 审核风险级别：`LOW` / `MEDIUM` / `HIGH` / `CRITICAL`；`CRITICAL` 必须携带 `risk_declaration`（含非空 `declared_by` 与 `reason`）的签名风险声明。 |
| `max_rounds` | 否 | 审核轮次上限，默认 `2`；每轮为 Reviewer → Arbiter → Meta Reviewer。 |
| `max_workers` | 否 | 同轮 Reviewer 并发上限；不填使用运行时默认值。 |

`--config` 的字段见“配置”一节与[示例配置](examples/multi-platform-review/config.json)；`--model` 可在命令行临时覆盖配置中的模型。

输出：默认打印人读 JSON；加 `--json` 输出机器可读结果。主要字段：`outcome`（`PASS` / `WARN` / `REVIEW_REQUIRED` / `HUMAN_DECISION_REQUIRED` / `STOPPED`）、轮次与每轮 Reviewer 结果、Arbiter 汇总与收据校验信息，以及 `warnings`（模型一致性警告）和 `usage_summary`（调用次数、token 用量、实际使用模型）。退出码 `0` 表示流程完成（不代表审核通过），`1` 表示审核未通过（`STOPPED` 或 `HUMAN_DECISION_REQUIRED`），`2` 表示请求或配置不合法。需要手工校验一份序列化审核记录时，使用 `goalanchor gate --phase review-cycle`，它会按同一 schema 校验 `context`、`reviewer_results`、`arbiter`、`meta_review` 等字段。

## 配置

| 场景 | 配置方式 |
|---|---|
| 核心 CLI / Python 库 | 无必需配置文件，无必需环境变量；通过命令行参数或 API 参数调用。 |
| Agent 调度 | 使用 JSON 配置描述平台、profile、命令与模型；参考 [示例配置](examples/multi-platform-review/config.json)，通过 `--config` 传入。模型解析优先级为 profile 级 > 平台级 > 顶层。 |
| 严格工作流 | 需要持久 SQLite 数据库、可信 `EventAuthority` 密钥、受控 executor 和 observer；密钥与账本由可信控制侧保管，不能暴露给模型。 |
| PI-Desktop | 在插件设置中填写 Python 可执行文件路径，或在启动桌面前设置 `GOALANCHOR_PYTHON`。插件不读取 API key。 |
| Agent CLI 凭据 | 由各 Agent CLI 自行管理；GoalAnchor 不读取或保存这些凭据。 |
| 测试脚本 | `GOALANCHOR_TEST_TASK_START/END`、`GOALANCHOR_DIALOGUE_TASK_START/END` 等为测试标记，普通用户无需设置。 |

真实平台探测使用 `goalanchor platforms --probe --json`。它会调用已安装的 Agent CLI，可能产生模型用量；不需要探测时不要加 `--probe`。

Agent 调度配置的顶层字段：

| 字段 | 说明 |
|---|---|
| `model` | 顶层默认模型标识；`review-cycle --model` 可在命令行覆盖它。解析优先级为 profile 级 > 平台级 > 顶层。 |
| `platforms.<name>` | 平台定义：`argv`（最多一个 `{prompt}` 占位符）、`timeout_seconds`（默认 600）、`model`、`model_flag`。 |
| `profiles.<id>` | 具名 Agent：`platform`（引用平台）或 `command`/`argv`（stdio 桥接，二选一）、`roles`（`reviewer`/`arbiter`/`meta_reviewer`）、`display_name`，可选 `model`、`timeout_seconds`、`env`、`enabled`、`retries`、`capabilities`。 |
| `role_bindings` | 为 `arbiter`、`meta_reviewer` 指定使用的 profile；未绑定的 reviewer 角色使用所有声明了该角色的 profile。 |
| `capabilities` | 调度能力声明：`supports_parallel`（同轮并发）、`max_concurrency`。 |

完整字段校验在 `goalanchor/platforms/platform_config.py`；最小可跑示例见[示例配置](examples/multi-platform-review/config.json)。

### 性能与规模上限

| 项 | 默认值 | 说明 |
|---|---|---|
| 平台调度超时 | 600 秒 | 每个平台可在配置中用 `timeout_seconds` 覆盖（`AgentRequest` 内部默认 300 秒，实际调度以平台配置为准）。 |
| 同轮 Reviewer 并发 | 4 | `AgentRuntime.max_concurrency`，必须是正整数；Arbiter 与 Meta Reviewer 始终顺序执行。 |
| 审核轮次 | 2 | 请求中的 `max_rounds` 可调整；到上限仍未收敛即返回 `REVIEW_REQUIRED`。 |
| SQLite 账本并发 | 单写者 | 写事务用 `BEGIN IMMEDIATE` 串行化；`busy_timeout` 由 store 的 `timeout` 参数控制。Windows 高负载下多进程同时写入可能出现锁竞争等待，这是已知边界。 |
| 数据库大小 | 无内置上限 | 账本只追加不清理；长期运行请自行归档旧数据库文件。 |

## 项目结构

```text
GoalAnchor/
├── goalanchor/               Python 包（契约、门控、证据、审核、工作流、宿主适配）
├── tests/                    单元/集成测试、回归夹具、PI-Desktop Node 测试
├── docs/                     开发规范、工作流/PI-Desktop 帮助与 CI 任务定义
├── skills/                   产品 Skill 与第三方 Skill 快照
├── scripts/                  验证、回归、基准、Skill 同步与宿主检查脚本
├── examples/                 离线多 Agent 审核示例
├── audit/                    验收映射与 JUnit 证据
├── delivery/                 已发布交付物（wheel/sdist/校验和/manifest）
├── .github/ .workflow/       GitHub Actions / Gitee Go 流水线
├── pyproject.toml            包元数据、依赖、Ruff/Mypy/pytest 配置
├── MANIFEST.in               sdist 打包清单
├── requirements-dev.txt      开发与验证依赖
├── requirements-validation.txt  Skill frontmatter 校验依赖（PyYAML）
├── requirements-image.txt    可选 ImageGen 依赖
├── AGENT-COMPATIBILITY.md    Agent 兼容性层级与支持边界
├── CONTRIBUTING.md           贡献与 Git 规范
├── SECURITY.md               安全报告流程
├── CODE_OF_CONDUCT.md        行为准则
├── CHANGELOG.md              版本变更记录
├── LICENSE                   项目许可证（MIT）
├── THIRD_PARTY_NOTICES.md    第三方声明
├── UPSTREAM_SOURCES.md       第三方 Skill 来源与同步规则
└── README.md / README.en.md  中英文说明
```

`build/`、`.venv/`、缓存、`test-results/` 和 `graphify-out/` 是本地生成物，不属于源码结构。

## 依赖与开发

### 运行时依赖

- Python `>=3.11`。
- 无第三方 Python 运行时依赖。
- 内置 Agent 调度需要外部 Agent CLI（Codex、Claude、Gemini、OpenCode）。
- PI-Desktop 需要 Python 在宿主 PATH 中；桌面开发测试需要 Node.js 20+。

### 开发依赖

```sh
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m pip install ruff mypy==2.4.0
```

`requirements-dev.txt` 包含 pytest 与 YAML 校验依赖；`requirements-validation.txt` 是 Skill frontmatter 与 Agent 元数据校验的独立依赖（PyYAML）；`requirements-image.txt` 是显式 ImageGen CLI fallback 的可选依赖，不是运行 GoalAnchor 的必需项。

### 本地质量检查

```sh
python -m pytest -q
python -m ruff check goalanchor tests scripts
python -m mypy goalanchor
python scripts/evaluate_fixtures.py --fixtures tests/fixtures/regression --revision "$(git rev-parse HEAD)"
python scripts/run_test_task.py --task docs/GoalAnchor-自动测试任务.md
python scripts/sync_skill_bundle.py --check
```

- 回归夹具会在子进程中导入 GoalAnchor，测试前需要先 `pip install -e .`。
- CI 使用三操作系统 × Python 3.11–3.13，并固定 MyPy 2.4.0。
- `ruff` 只检查自有 Python 代码；`skills/.system/` 是上游快照，不纳入该门禁。
- 涉及 JavaScript 或 PI-Desktop 桥接时，使用 Node.js 20 运行 `node --test tests/js/test_pi_desktop.cjs`。

## 核心术语

| 术语 | 含义 |
|---|---|
| 三段门控 | `PLAN → VERIFY → EXECUTE`：先固定目标与计划，再固定证据与验收条件，最后才允许执行与关闭。 |
| 五问 Q1–Q5 | 计划阶段的五项检查：Q1 Goal（目标明确）、Q2 Logic（推理成立）、Q3 Path（路径可行）、Q4 Minimum（最小充分集）、Q5 Structure（结构可执行）。 |
| 门控 Gate | 确定性硬检查，不是提示词软约束；`GateId` 还包括 VERIFY、EXECUTE、CLOSURE、ARTIFACT、PROVENANCE 等阶段与专项检查。 |
| 证据分级 | `Observation`（观察）→ `Fact`（事实）→ `ReproducibleExperiment`（可复现实验）→ `ExclusionaryEvidence`（排除性证据）→ `RootCauseEvidence`（根因证据）。 |
| 结论上限 | 每条证据只支持到一定强度的结论：`Observation` / `Fact` / `Association` / `Causal` / `RootCause` / `DesignNecessity`；超出上限的结论必须降级或补充证据。 |
| 证据状态 | `VALID` / `LIMITED` / `INSUFFICIENT` / `CONTRADICTED`，由门控按证据与结论的匹配关系判定。 |
| 九级闭合 | 关闭强度从低到高：`Symptom Suppressed` → `Containment` → `Local Fix` → `Root Cause Identified` → `Root Fix` → `Build Success` → `Artifact Verified` → `System Validation` → `Product Closure`；宣称哪一级必须有对应证据。 |
| 溯源链 | 嵌入式交付物链：`Source → Object → ELF → BIN/FW → Flashed Artifact → Device Under Test`，每级对应产物类型与验证动作。 |
| ReviewContext | 一次审核的固定上下文：目标、要求、约束、证据引用与被审方案；可选携带 `evidence_digests` 绑定证据内容。 |
| 审核三角色 | Reviewer 独立审 → Arbiter 汇总 → Meta Reviewer 复核仲裁；一轮未收敛可进入下一轮，受 `max_rounds` 约束。 |
| Strict / Legacy | 同一契约与证据模型下的两条路径：`Workflow`（legacy，保持既有行为）与 `StrictWorkflow`（可信控制侧绑定基线、证据、审核与授权）。 |
| 审核包 packet | 派发给某个角色的完整输入：角色提示词、上下文、既往轮次与收据；收据校验防止结果被替换。 |

以上术语在测试与源码中都有对应实现（`gates.py`、`evidence.py`、`closure.py`、`review_service.py`），以代码为准。

## 支持的应用

GoalAnchor 本身不是 Agent，也不提供模型。它以三种形态接入外部应用，同一应用可以同时承担一种或多种：

- **Skill（规则注入）**：把 `PLAN → VERIFY → EXECUTE` 规则和 Q1–Q5 检查清单放进宿主的 Skill 目录，由宿主 Agent 在会话中遵循。
- **执行器（子 Agent 调度）**：`review-cycle` 按配置调用外部 CLI，把 reviewer / arbiter / meta-reviewer 分派到独立进程并汇总结果。
- **插件（桌面深度集成）**：以 `.piplug` 在桌面宿主中注册 Skill、工具和 Hook，获得比纯 Skill 更强的接入点。

> **Skill 安装成功只代表文件已放置，不代表该应用具备内置调度器或原生强制执行能力。** 原生宿主能力仍以逐宿主实测为准。

### 各应用的角色

| 角色 | 作用 | 边界 |
|---|---|---|
| 宿主应用 | 提供会话界面、模型和工具调用，加载 Skill 或插件 | 规则在宿主内执行；GoalAnchor 不接管宿主的 Agent 循环 |
| 审核执行器 | 被 `review-cycle` 调起，独立产出审核结论 | 子 Agent 可能是同一应用，也可能是另一个 CLI / 模型 |
| 可信控制面 | 保管密钥、SQLite 账本、一次性授权与执行回执 | 必须由调用方应用持有，不能由模型或 Agent 自报 |
| 构建与发布端 | 运行测试、构建 wheel、产出交付物 | CI 或本地脚本，不属于 Agent 能力 |

### 一、内置调度支持的宿主

以下四个应用同时支持 Skill 安装和内置子 Agent 调度，可直接用于多 Agent 审核。

| 应用 | 用途 | 接入方式 | 在方案中的角色 | 支持级别 |
|---|---|---|---|---|
| **Codex CLI** | OpenAI 的终端编码 Agent | `goalanchor install --target codex --scope user`（`CODEX_HOME` 或 `~/.codex`）；项目内装到 `.agents/skills`。调度命令 `codex exec -`，模型参数 `-m` | Skill 宿主 + 审核执行器；另有 `codex app-server` 受控事件回调通道 | Skill + 内置调度 + 受控回调（原生强制 `LIMITED`） |
| **Claude Code** | Anthropic 的终端编码 Agent | `--target claude`（用户 `~/.claude`，项目 `.claude/skills`）。调度命令 `claude -p`，模型参数 `--model` | Skill 宿主 + 审核执行器 | Skill + 内置调度 |
| **Gemini CLI** | Google 的终端 Agent | `--target gemini`（用户 `~/.gemini`，项目 `.gemini/skills`）。调度命令 `gemini`，模型参数 `-m` | Skill 宿主 + 审核执行器；常用于跨模型交叉审核 | Skill + 内置调度 |
| **OpenCode** | 开源终端编码 Agent | `--target opencode`（用户 `~/.config/opencode`，项目 `.opencode/skills`）。调度命令 `opencode run`，模型参数 `-m` | Skill 宿主 + 审核执行器 | Skill + 内置调度 |

四个内置调度器都通过 **stdin** 传入提示词，避免 Windows `cmd.exe` 破坏多行参数，并绕开命令行长度上限。使用前需自行安装并登录对应 CLI，GoalAnchor 不安装它们，也不管理其凭据。

### 二、仅 Skill 接入的宿主

这些应用可以安装并使用 GoalAnchor Skill，但没有内置调度器。需要独立审核时，可让宿主用自己的子 Agent 机制拉起，或配置自定义命令桥接。

| 应用 | 用途 | 接入方式 | 在方案中的角色 | 支持级别 |
|---|---|---|---|---|
| **Cursor** | AI 代码编辑器 | `--target cursor`（用户 `~/.cursor`，项目 `.cursor/skills`） | Skill 宿主；审核执行需自定义命令或 JSON-over-stdio 桥接 | 仅 Skill |
| **Pi CLI** | Pi 的终端 Agent | `--target pi`（用户 `~/.pi/agent`，项目 `.pi/skills`） | Skill 宿主；无内置调度 | 仅 Skill |
| **DeepSeek Harness (DSH)** | DeepSeek 的 Agent harness | `--target dsh`（`DSH_HOME` 或 `~/.dsh`；项目 `.dsh/skills`） | Skill 宿主；审核执行需自定义桥接 | 仅 Skill |
| **Zcode** | Zcode Agent 环境 | `--target zcode --destination <Skill 根目录>`；原生发现路径由宿主配置，必须显式指定 | Skill 宿主；调度需自定义接入 | 仅 Skill（需显式目录） |
| **Agent Skills 通用目录** | 兼容 Agent Skills 规范的共享落盘位置 | `--target agents`（用户 `~/.agents`，项目 `.agents/skills`） | 通用文件位置；Agent 调度由使用它的宿主提供 | 仅文件放置 |

**Pi CLI 与 PI-Desktop 是两个不同产品**：前者是终端 Agent，用 `--target pi` 安装 Skill；后者是桌面应用，必须构建独立插件。

### 三、桌面插件：PI-Desktop

| 项目 | 说明 |
|---|---|
| 用途 | 桌面 AI Agent 应用，支持插件、Skill、工具注册与会话扩展 |
| 接入方式 | `python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug`，然后在 Plugins 页安装 `.piplug`。**不要**使用 `--target pi` |
| 申请的宿主权限 | `agent.prompt.inject`（Skill）、`agent.extension`（Hook）、`agent.tool.register`（工具）、`agent.complete`（模型额度）、`session.read`（核对模型身份） |
| 在方案中的角色 | 唯一提供**工具级**接入的形态：`goalanchor_review` 审核工具、`goalanchor_gate` 门控工具、工具批准 Hook 与会话状态命令 |
| 支持级别 | 最低接口基线 0.18.0；已实际验证 0.18.0 与 0.19.1 的安装、审核、门控与卸载 |

插件要求宿主进程能找到 Python；Python 内核随插件提供，不需要另外 `pip install`。插件不读取 API key，也不申请网络或文件写入权限。宿主权限模型不是 OS 沙箱。详见[插件指南](goalanchor/_pi_desktop/README.md)；适配规格与验证记录由项目文档中心维护，不随代码分发。

### 四、自定义应用与通用桥接

内置调度器之外，任何“能从 stdin 读一行 JSON、向 stdout 写一行 JSON”的命令都可以作为审核执行器。

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

| 方式 | 适用场景 | 接入要点 |
|---|---|---|
| 自定义平台（`platforms.<name>.argv`） | 已有 CLI，可用 stdin 或参数接收提示词 | `argv` 最多一个 `{prompt}` 占位符；不写占位符时从 stdin 读取；`timeout_seconds` 默认 600 秒 |
| JSON-over-stdio 桥接 | 宿主没有 CLI，但能执行命令 | stdin 一行 JSON 请求；stdout 必须输出且仅输出一行 JSON 结果 |
| 宿主原生子 Agent | 希望在宿主界面看到每个角色的会话 | 用 `goalanchor review-prompt --role ...` 渲染提示词，交给宿主拉起子 Agent，再用 `goalanchor gate --phase review-cycle` 校验 |
| 自定义 Skill 目录（`--target custom`） | 应用只支持 Skill，不支持调度 | 用 `--destination` 指定根目录，GoalAnchor 会追加 `goalanchor/` |

### 五、如何选择

| 你的目标 | 建议接入 |
|---|---|
| 只在自己的 Agent 会话中获得 PLAN / VERIFY / EXECUTE 检查 | 安装 Skill：`goalanchor install --target <host>` |
| 让 reviewer / arbiter / meta-reviewer 由不同模型独立执行 | 用内置调度器或自定义 `platforms` 配置运行 `review-cycle` |
| 在宿主界面中看到每个角色的对话 | 用 `review-prompt` 渲染提示词，交给宿主原生子 Agent |
| 使用 PI-Desktop | 安装 `.piplug`，获得工具注册与批准 Hook |
| 需要可信授权、持久账本、执行回执 | 使用 `Workflow` / `StrictWorkflow` 并自建可信控制面 |

### 六、接入排查

| 现象 | 常见原因 | 处理方式 |
|---|---|---|
| `goalanchor platforms --json` 中 `available` 为 `false` | 对应 CLI 未安装，或不在当前 `PATH` | 安装并登录该 CLI，重开终端后复查；Windows 上需能找到 `.cmd` 入口 |
| Skill 已安装但 Agent 不触发 | 未重启会话；或任务不满足触发条件 | 新建或刷新会话；确认是“审核已形成的方案/改动/结论”或用户显式要求审核，普通问答不触发 |
| `review-cycle` 退出码 `2` | 配置或运行错误 | 检查 `platforms.<name>.argv`、`{prompt}` 占位符数量和 `timeout_seconds` |
| `review-cycle` 退出码 `1` | 需要人工决策，或审核已 `STOPPED` | 查看输出的裁决与异议，补充证据或授权后重跑，不要直接当作通过 |
| 子 Agent 结果解析失败 | stdout 混入日志，或输出了多个 JSON 对象 | 桥接脚本必须只向 stdout 写一行 JSON，日志改走 stderr |
| PI-Desktop 报找不到 Python | 宿主进程 `PATH` 中没有 Python | 在插件设置里填写 Python 绝对路径，或在启动前设置 `GOALANCHOR_PYTHON` |
| PI-Desktop 修改插件后行为未变 | 宿主仍在用旧模块缓存 | 完整退出并重启桌面应用，重新初始化会话；批准状态不会继承 |

### 支持级别的含义

| 级别 | 含义 |
|---|---|
| Skill + 内置调度 + 受控回调 | 可安装 Skill、可被 `review-cycle` 调用，并额外支持宿主 App Server 的受控事件回调；原生强制仍为 `LIMITED` |
| Skill + 内置调度 | 可安装 Skill，也可被 `review-cycle` 直接调用 |
| 仅 Skill | 只能放置 Skill 文件；审核执行需宿主原生子 Agent 或自定义桥接 |
| 仅文件放置 | 只提供目录位置，不代表该宿主存在可用的 Agent 运行时 |

## 版本与兼容性

- 版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。
- 当前 `0.9.x` 同时提供 legacy `Workflow` 和 `StrictWorkflow`：旧对象、JSON 与历史空证据行为保持兼容。
- 旧 `ONE_TIME` 授权仍存在已知限制，不能作为严格授权或关闭依据；需要可信门控时使用 `Workflow` / `workflow` 命令。
- 原生宿主能力仍以逐宿主实测为准；fixture、受控回调和 TextHostAdapter 共享文本测试不替代原生验收。
- 已发布版本与资产见 [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases)，完整历史见 [CHANGELOG](CHANGELOG.md)。

### 升级与回滚

- **升级运行时**：从 [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) 下载新版本 wheel 后 `python -m pip install --upgrade <wheel 文件>`，或重新 `pip install .`；项目未发布到 PyPI，`pip install goalanchor` 不可用。零第三方依赖，升级不影响其他包。
- **升级 Skill**：`goalanchor install --target <host> --force` 覆盖旧版本文件。
- **升级 SQLite 账本**：schema 版本记录在 `PRAGMA user_version`。v1 账本首次被新版本打开时自动补齐 v2 结构（只增不迁移，legacy 记录保持原样）；空库直接建为 v2；高于当前支持的版本会拒绝打开。Strict 功能要求账本已是 v2。
- **回滚**：先停止使用账本的进程并备份数据库文件，再安装旧版 wheel。旧版本对 v2 账本的读取行为以该版本代码为准；如需完全回退，恢复备份的 v1 备份文件。
- **回滚 PI-Desktop 插件**：在桌面中卸载旧 `.piplug` 后安装目标版本构建产物；数据库与桌面插件版本无绑定关系。

### 支持周期

- 仅维护**最新一个 MINOR 分支**（当前 `0.9.x`）；缺陷修复与安全修复优先发布在最新版本。
- 旧 MINOR 分支（如 `0.8.x`）不再接受常规修复；如仍在使用，请升级到最新 Release。
- 发布节奏与资产校验以 [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) 页面为准；每个版本的交付物都附 `SHA256SUMS.txt` 与 `release-manifest.json`。

## 贡献指南

欢迎提交 Issue 和 Pull Request。开始前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，其中包含分支策略、提交标题、版本规则和本地质量检查要求。

简要流程：

1. 从最新 `master` 创建短期分支，不直接在 `master` 开发。
2. 每个提交只做一件事，使用 Conventional Commits 标题。
3. 提交前运行 pytest、Ruff、Mypy 和相关脚本检查。
4. PR 描述写清目标、改动、验证结果和剩余风险；不要提交凭据、个人数据或未授权的大文件。

参与项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 安全

请不要在公开 Issue、PR 或讨论中报告漏洞。优先使用 GitHub 仓库的 **Security → Advisories → Report a vulnerability**；不可用时发送邮件到 [chenyuyu6666@gmail.com](mailto:chenyuyu6666@gmail.com)，主题为 `GoalAnchor security report`。请包含受影响版本/提交、影响和复现步骤，不要附带真实凭据或个人数据。详见 [SECURITY.md](SECURITY.md)。

### 信任边界

GoalAnchor 的门控逻辑在**本地 Python 进程**中执行，模型或 Agent 只通过结构化输入参与：

| 主体 | 能做什么 | 不能做什么 |
|---|---|---|
| 本地核心（可信） | 执行 Gate 判定、证据校验、账本读写、签名事件验证、一次性授权记账 | 不会读取或上报你的模型凭据 |
| 模型 / Agent（不可信） | 产出结论文本、审核意见、问答内容、计划修订 | 不能直接修改门控判定、解锁 EXECUTE 阻断或写入账本；这些输入都要经过 Reviewer → Arbiter → Meta 结构化确认 |
| 宿主应用 | 加载 Skill、调度子 Agent、落实执行阻断 | 宿主自报字段（如 `isolated`、`agrees_with_arbiter`）不可被核心核验，属于已知边界 |
| 调用方（可信控制面） | 保管 `EventAuthority` 密钥、SQLite 账本与执行回执 | 密钥与账本不能暴露给模型或 Agent CLI 进程 |

实践要求：审核证据由调用方采集并（可选）以 `evidence_digests` 绑定；执行阻断最终依赖宿主落实，GoalAnchor 不在宿主进程外强制进程级隔离。

## 许可证

项目使用 [MIT License](LICENSE)。`skills/graphify/` 和 `skills/.system/` 中的第三方快照保留各自许可证；根目录 MIT 许可证不自动覆盖这些内容。第三方声明与来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md)。

## 更多资料

- [Agent 兼容性说明](AGENT-COMPATIBILITY.md)
- [离线多 Agent 审核示例](examples/multi-platform-review/README.md)
- [PI-Desktop 插件指南](goalanchor/_pi_desktop/README.md)
- [仓库文档索引](docs/README.md)
- [测试说明](tests/README.md)
- [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) · [Gitee 镜像](https://gitee.com/chen-yuyu6666/goal-anchor)
