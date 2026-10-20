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
- [支持的平台](#支持的平台)
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

GoalAnchor 以同一套契约与证据模型提供 legacy `Workflow` 和 `StrictWorkflow` 两条路径：legacy 路径保留既有调用与辅助检查；Strict 路径用于可信控制侧绑定基线、证据、审核上下文和受限操作。Strict 审核、问答、授权、执行、reconcile 与关闭仍返回 `UNAVAILABLE`；P2b–P7 尚未完成。版本历史见 [CHANGELOG](CHANGELOG.md)。

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

安装后新建或刷新宿主会话，再让 Agent 使用 GoalAnchor 检查已形成的计划、改动或结论。Skill 安装目标只表示文件放置方式，不代表该宿主具备内置 Agent 调度。支持目标见[支持的平台](#支持的平台)。

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

# 严格工作流能力与本地 demo
goalanchor workflow capabilities
goalanchor workflow demo --directory ./workflow-demo-v08
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

`StrictWorkflow` 是与旧 `Workflow` 并列的增量 API。它需要 `store`、`user_authority`、`observer_authority`、`policy` 四个位置参数，并通过关键字参数注入 `trusted_event_verifier` / `trusted_runtime_binding_provider`。模型或 Agent 不能提供 principal、session 或 host facts；完整接入见[严格运行时指南](docs/workflow-v0.8.md)。

旧 `Workflow` 的对象、JSON 和历史空证据行为保持兼容；旧审核、旧 grant、legacy `CLOSED` 和 legacy `VerificationResult` 不会变成 Strict 证明。

## 配置

| 场景 | 配置方式 |
|---|---|
| 核心 CLI / Python 库 | 无必需配置文件，无必需环境变量；通过命令行参数或 API 参数调用。 |
| Agent 调度 | 使用 JSON 配置描述平台、profile、命令与模型；参考 [示例配置](examples/multi-platform-review/config.json)，通过 `--config` 传入。模型解析优先级为 profile 级 > 平台级 > 顶层。 |
| 严格工作流 | 需要持久 SQLite 数据库、可信 `EventAuthority` 密钥、受控 executor 和 observer；密钥与账本由可信控制侧保管，不能暴露给模型。详见[严格运行时指南](docs/workflow-v0.8.md)。 |
| PI-Desktop | 在插件设置中填写 Python 可执行文件路径，或在启动桌面前设置 `GOALANCHOR_PYTHON`。插件不读取 API key。 |
| Agent CLI 凭据 | 由各 Agent CLI 自行管理；GoalAnchor 不读取或保存这些凭据。 |
| 测试脚本 | `GOALANCHOR_TEST_TASK_START/END`、`GOALANCHOR_DIALOGUE_TASK_START/END` 等为测试标记，普通用户无需设置。 |

真实平台探测使用 `goalanchor platforms --probe --json`。它会调用已安装的 Agent CLI，可能产生模型用量；不需要探测时不要加 `--probe`。

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
├── requirements-dev.txt      开发与验证依赖
├── requirements-image.txt    可选 ImageGen 依赖
├── CONTRIBUTING.md           贡献与 Git 规范
├── SECURITY.md               安全报告流程
├── CODE_OF_CONDUCT.md        行为准则
├── CHANGELOG.md              版本变更记录
├── LICENSE                   项目许可证（MIT）
├── THIRD_PARTY_NOTICES.md    第三方声明
├── UPSTREAM_SOURCES.md       第三方 Skill 来源与同步规则
└── README.md / README.en.md  中英文说明
```

`build/`、`.venv/`、缓存和 `test-results/` 是本地生成物，不属于源码结构。

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

`requirements-dev.txt` 包含 pytest 与 YAML 校验依赖；`requirements-image.txt` 是显式 ImageGen CLI fallback 的可选依赖，不是运行 GoalAnchor 的必需项。

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

## 支持的平台

下表区分 Skill 文件安装位置、内置 Agent 调度器和独立桌面插件。**Skill 安装目标只表示文件放置方式，不代表该宿主内置了 Agent 调度适配。**

| 平台 / 宿主 | Skill 安装目标 | Agent 调度或插件方式 |
|---|---|---|
| Codex CLI | `codex` | 内置 CLI 调度器；需单独安装并登录 Codex CLI |
| Claude Code | `claude` | 内置 CLI 调度器；需单独安装并登录 Claude CLI |
| Gemini CLI | `gemini` | 内置 CLI 调度器；需单独安装并登录 Gemini CLI |
| OpenCode | `opencode` | 内置 CLI 调度器；需单独安装并登录 OpenCode CLI |
| Cursor | `cursor` | 可安装 Skill；Agent 执行需自定义命令或 JSON-over-stdio 桥接 |
| Pi CLI | `pi` | 可安装 Skill；这是 Pi CLI，不是 PI-Desktop |
| DeepSeek Harness (DSH) | `dsh` | 可安装 Skill；Agent 执行需自定义命令或桥接 |
| Agent Skills 通用目录 | `agents` | 将 Skill 放入共享目录；Agent 调度由宿主或自定义配置提供 |
| Zcode | `zcode`，需搭配 `--destination` | 需指定 Skill 目录；Agent 调度需自定义接入 |
| 其他宿主 | `custom`，需搭配 `--destination` | 可配置自定义 CLI 命令或 JSON-over-stdio Agent |
| PI-Desktop | 单独构建 `.piplug` 插件 | 独立 Extension 和 Python 桥接；不使用 `--target pi` 安装 |

内置调度器目前是 Codex、Claude、Gemini 和 OpenCode。PI-Desktop 使用独立插件，并按宿主公开接口检测能力；具体验证基线和边界见[适配规格](docs/PI-Desktop适配规格.md)与[验证记录](docs/PI-Desktop验证记录.md)。

## 版本与兼容性

- 版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。
- 当前 `0.9.x` 同时提供 legacy `Workflow` 和 `StrictWorkflow`：旧对象、JSON 与历史空证据行为保持兼容。
- 旧 `ONE_TIME` 授权仍存在已知限制，不能作为严格授权或关闭依据；需要可信门控时使用 `Workflow` / `workflow` 命令。
- 原生宿主能力仍以逐宿主实测为准；fixture、受控回调和 TextHostAdapter 共享文本测试不替代原生验收。
- 已发布版本与资产见 [Releases](https://github.com/chenyuyu6666/GoalAnchor/releases)，完整历史见 [CHANGELOG](CHANGELOG.md)。

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

## 许可证

项目使用 [MIT License](LICENSE)。`skills/graphify/` 和 `skills/.system/` 中的第三方快照保留各自许可证；根目录 MIT 许可证不自动覆盖这些内容。第三方声明与来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [UPSTREAM_SOURCES.md](UPSTREAM_SOURCES.md)。

## 更多资料

- [严格运行时指南](docs/workflow-v0.8.md)
- [离线多 Agent 审核示例](examples/multi-platform-review/README.md)
- [PI-Desktop 插件指南](goalanchor/_pi_desktop/README.md)
- [仓库文档索引](docs/README.md)
- [测试说明](tests/README.md)
- [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases) · [Gitee 镜像](https://gitee.com/chen-yuyu6666/goal-anchor)
