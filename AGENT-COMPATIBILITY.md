# GoalAnchor Agent 兼容性

GoalAnchor 的规则入口是标准的 `SKILL.md`：YAML frontmatter 只使用 `name` 和 `description`，正文只描述规则、输入、决策和验证，不调用任何宿主专用 API。

## 支持层级

| 层级 | 目标 | 方式 |
|---|---|---|
| 规则兼容 | 规则语义保持一致 | 同一份 `skills/goalanchor/SKILL.md` |
| 发现兼容 | 宿主能自动找到 Skill | 安装到宿主对应的 `skills/<name>/SKILL.md` 根目录 |
| 执行兼容 | 不依赖宿主工具名 | 使用自然语言规则、统一 Agent Dispatcher 和可选 Python CLI |
| 结果兼容 | 结果可比较 | 使用统一的 Goal Contract、Evidence 和 Closure 字段 |

不同 Agent 的模型、权限、上下文和工具集仍可能不同；本项目保证的是文件格式、规则入口、目录映射、Agent 角色协议和确定性校验，不保证模型输出逐字相同。

## 多 Agent 执行层

GoalAnchor 不直接绑定 Codex、Claude Code、DSH 或 Zcode 的私有 API。它提供统一的
`AgentProfile`、`AgentDispatcher` 和 JSON-over-stdio 协议：宿主负责展示/启动命名子智能体，
GoalAnchor 负责角色绑定、Reviewer 并行、Arbiter 汇总、Meta Reviewer 复核和争议轮次。

逻辑角色固定为：

- `reviewer`：相同上下文的独立并行分析；
- `arbiter`：汇总本轮 Reviewer；
- `meta_reviewer`：独立审查 Arbiter。

像 `Euclid` 这样的名称属于宿主 Agent Profile；GoalAnchor 只依赖其 `profile_id`、支持角色
和启用状态。各平台只需提供一个桥接命令或实现同一 Dispatcher 接口，不能改变 GoalAnchor
的判定规则。完整配置格式见 `skills/goalanchor/references/agent-runtime.md`。

### 内置多平台调度适配

执行层提供两条调度路径。**界面可见**是首选：用 `python -m goalanchor review-prompt`
按角色渲染提示词，然后用**宿主自己的子 Agent 机制**并行拉起（子 Agent 会话在宿主 UI
里可见，如独立页签/任务卡片），收集 JSON 回复后用
`python -m goalanchor gate --phase review-cycle --record <cycle.json>` 做确定性校验聚合。
**无头调度**是备用：宿主无法拉起子 Agent 但平台 CLI 可用时，用
`python -m goalanchor review-cycle --request <request.json> --config <config.json>`
直接并发调起平台子进程。

无头路径的要点：

- 内置平台（提示词全部经 stdin 管道传入，规避 Windows `.cmd` 转义与 argv 长度限制）：
  `codex`（`codex exec -`）、`claude`（`claude -p`）、`gemini`、`opencode`（`opencode run`）；
  其他平台在 `platforms.<name>.argv` 里自定义，`{prompt}` 是提示词占位，无占位即 stdin 模式。
- 提示词由 GoalAnchor 按角色渲染并注入共享 `ReviewContext` 指纹；平台输出兼容直接
  JSON、信封包装（如 `{"result": "<json 文本>"}`）和正文夹带 JSON 三种形态；`codex exec`
  的提示词回显/转写不会干扰解析（优先取最后一个带结果字段的 JSON）。
- 已在真实平台验证：claude（Claude Code 2.1.272）与 codex（codex-cli 0.154.0）作为
  Reviewer 全链路通过；平台偶发失败会以退出码 2 + stderr 详情显式报错，不自动重试。
- 同一轮 Reviewer 强制绑定不同 profile 并发执行；`LOW/MEDIUM/HIGH/CRITICAL` 分别需要
  1/2/2/3 个 `reviewer` profile——LOW 不再免审（单审可信度不足），非 LOW 风险一律多审。
- 无头结果附带 `agent_bindings` 审计数组（agent_id → profile → 平台 → 轮次）。
- 可运行示例见 `examples/multi-platform-review/`（含离线 echo Agent 与真实平台参考配置
  `config.real.json`，可在未安装平台 CLI 时先验证调度链路）。

## 目录映射

`python -m goalanchor` 会根据目标和作用域计算目录，不拼接 shell 命令，也不写入未明确指定的目录：

| 目标 | 用户级默认目录 | 项目级默认目录 |
|---|---|---|
| Codex | `$CODEX_HOME/skills` 或 `~/.codex/skills` | `.agents/skills` |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| DSH | `$DSH_HOME/skills` 或 `~/.dsh/skills` | `.dsh/skills` |
| Cursor | `~/.cursor/skills` | `.cursor/skills` |
| Gemini CLI | `~/.gemini/skills` | `.gemini/skills` |
| OpenCode | `~/.config/opencode/skills` | `.opencode/skills` |
| Pi | `~/.pi/agent/skills` | `.pi/skills` |
| Agent Skills 通用根 | `~/.agents/skills` | `.agents/skills` |
| 其他工具 | `--destination` 指定 | `--destination` 指定 |

DSH 要求直接的一层目录 bundle：最终必须是 `<root>/goalanchor/SKILL.md`，不要再套一层仓库目录；其他宿主对 `.agents` 是否递归发现由各自实现决定，本工具始终生成这个标准第一层结构。

## 安装

在仓库根目录执行。首次使用建议先 dry-run：

```text
python -m goalanchor install --target claude --scope user --source skills/goalanchor --dry-run --json
```

安装到当前项目：

```text
python -m goalanchor install --target codex --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target claude --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target dsh --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target cursor --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target gemini --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target opencode --scope project --project-root . --source skills/goalanchor
python -m goalanchor install --target pi --scope project --project-root . --source skills/goalanchor
```

安装后检查内容哈希：

```text
python -m goalanchor check --target claude --scope project --project-root . --source skills/goalanchor --json
```

默认不覆盖已有 Skill。确认要替换时才加 `--force`；路径不在目标表中时使用 `--target custom --destination <skill-root>`。

## 宿主说明

### Codex

`skills/goalanchor/SKILL.md` 可作为 Codex Skill 包安装。`agents/openai.yaml` 是可选的 Codex 界面元数据，其他宿主可以忽略它；规则正文不依赖这个文件。
在 Codex 中按 `$goalanchor` 调用；不再使用旧的 `$plan-audit`。

### Claude Code

项目级入口是 `.claude/skills/goalanchor/SKILL.md`，用户级入口是 `~/.claude/skills/goalanchor/SKILL.md`。通过 `/goalanchor` 直接调用，或由 `description` 触发自动加载。

### DSH

项目级入口是 `.dsh/skills/goalanchor/SKILL.md`，用户级入口是 `$DSH_HOME/skills/goalanchor/SKILL.md`。Skill 名必须保持 kebab-case；不使用 DSH 专用插件 API，因此可作为普通文件 Skill 加载。
DSH 的 Skill 名为 `goalanchor`；通过宿主的 Skill 加载入口选择该名称。

### Zcode

Zcode 使用 `--target zcode --destination <skill-root>` 安装 Skill。其 Agent UI/调度方式由
Zcode 自己提供；只需把命名 Agent 映射到 `reviewer`、`arbiter` 和 `meta_reviewer` 三个逻辑
角色，或提供符合 JSON-over-stdio 协议的桥接命令。

Windows 桌面安装可能不注册 `zcode` PATH 命令。检测时应同时检查安装注册表、开始菜单和
安装目录中的 `resources/glm/zcode.cjs`，不能把 `Get-Command zcode` 失败解释为未安装。
本机 3.16.3 桌面版的 CLI 报告版本为 0.17.0；两者分别记录。

`scripts/zcode_protocol_probe.py` 是仓库内的有界原生协议验收工具（不随 wheel 安装）。
执行 `python scripts/zcode_protocol_probe.py --help` 查看参数：显式指定 CLI、隔离工作目录、
Provider、模型和该模型支持的 reasoning level；Provider 配置只按路径交给宿主，不读写凭据。
默认仅预检，`--send` 才发送一次固定短句；请求 ID、session/input/turn 和终态必须关联，
`session/event.params.type` 是事件类型，`accepted: true` 只表示请求已接纳。
只有 `turn.completed` 且 `payload.resultType == success`、结果匹配、无工具调用才记成功。
超时保持 UNKNOWN，仅停止本次会话，不自动重发。若需 `--allow-workspace-mode-write`，
只对显式指定的隔离测试目录使用；宿主可能持久化该测试目录的模式偏好。
0.17.0 的计划状态与权限模式是两个字段：`mode=build` 可以同时 `planEnabled=true`。
探测器读取并核验本会话的 v4 conversation snapshot；不凭 `setMode` ACK 猜测计划状态。
创建时推理档位同时传入 `model.options.reasoningLevel` 和顶层 `thoughtLevel`，随后读回验证。
较大或无法解析的分片 snapshot 目前明确拒绝，不能降级为通过。

2026-10-25 本机记录：桌面 GLM-5.3-Flash 完成短句测试和四角色缺证据拒绝测试；
App Server 的 Coding Plan `ark-code-latest/low` 完成一次关联终态、零工具调用的真实测试。
详见 `audit/zcode-desktop-review-20261025.json` 与 `audit/zcode-protocol-plan-state-20261025.json`。

`scripts/zcode_goalanchor_bridge.py` 目前是实验性的 legacy/advisory 适配器。
它拒绝 Strict 输入、错误 envelope、多段 JSON 和不明确的终态；0.17.0 的 one-shot
`type: result` 输出缺少本适配器所需的明确成功标记，不能据此出具可信审核证明。
原生 UI 的真实模型答复另作证据，不能用离线 fake CLI 测试替代。
Strict question、approval、reconcile 和 closure 强制能力仍需独立验收。

### 其他 Agent

优先使用 `.agents/skills/goalanchor/SKILL.md` 或工具文档指定的等价目录。若宿主只支持自定义目录，使用 `--target custom --destination`，并保留 `SKILL.md` 文件名和 `name: goalanchor`。

### Cursor、Gemini CLI、OpenCode

三者都支持标准 `SKILL.md` 目录形式，项目级默认入口分别是 `.cursor/skills`、`.gemini/skills` 和 `.opencode/skills`；本仓库的 `cursor`、`gemini`、`opencode` target 会生成对应目录。它们的其他配置（权限、信任、自动调用开关）仍由各宿主自己的配置控制，不由 GoalAnchor CLI 越权修改。表中的用户目录是宿主默认值，不额外假设宿主支持自定义环境变量。

### Pi

Pi 支持 `.pi/skills`、`.agents/skills` 和 `~/.pi/agent/skills`。本仓库的 `pi` target 使用 Pi 原生目录；也可以使用 `agents` target 复用中立目录。当前机器未安装 Pi，因此本地记录只覆盖目录投影和文件哈希，未覆盖 Pi 运行时自动发现。
Pi 的显式调用名为 `/skill:goalanchor`。

### PI-Desktop（vastsa）

PI-Desktop 与 Pi CLI 分开适配；桌面不会自动导入 `~/.pi`。使用
`python -m goalanchor.pi_desktop build --destination <新目录> --archive <新包.piplug>`
生成 Skill/Extension 插件，再通过宿主 Plugins 导入并审核权限。以 0.18.0 为最低基线，
后续版本按公开 API 能力检测，不依赖固定安装路径、私有 IPC 或数据库。

已验证 0.18.0 发行包的 manifest/Skill 描述投影及实际 ExtensionRunner 加载、命令、工具 Hook、
Python 预检和会话重建清零。后续真实桌面验收覆盖 0.18.0 的安装/LOW 审核，以及
0.19.1 的原生安装、LOW 审核、确认窗口、单次写入、拒绝变化参数、Stop 和卸载清理。
仅在所列范围宣称实测；未知后续版本接受非关键新增字段，必需接口不兼容时明确拒绝。

审核复用原有 Reviewer/Arbiter/Meta 及两轮策略，通过当前模型独立 completion 执行；
0.18.0 不提供公开的可见子会话派发。门控由用户 `/goalanchor-guard on` 主动启用，
经预检和宿主确认绑定单次精确工具调用。宿主 Hook 的 fail-open、模型额度与取消限制见
[`goalanchor/_pi_desktop/README.md`](goalanchor/_pi_desktop/README.md)。

### ChatGPT / OpenAI Skills API

ChatGPT 的工作区 Skill 和 OpenAI Skills API 属于上传/版本 API，不是本地目录自动发现。可以把安装器复制出的 Skill 目录压缩后上传，或通过对应 API 创建版本；本仓库不在本地 CLI 中伪造账号、上传和权限操作。

## 兼容性检查

```text
python -m goalanchor check --target agents --scope project --project-root . --source skills/goalanchor --json
```

检查失败时先确认：目标目录是否正确、Skill 是否被旧版本覆盖、`SKILL.md` 是否位于目录第一层，以及源文件和目标文件 SHA-256 是否一致。
