# GoalAnchor

**中文** | [English](README.en.md)

> 让复杂工程任务从“计划”走到可核验的执行与交付。

GoalAnchor 是供 AI Agent 使用的目标与证据校验工具，适用于实施计划、代码改动、实验结论和多 Agent 审核。它把关键步骤组织为 **PLAN → VERIFY → EXECUTE**，帮助发现目标漂移、未经验证的假设、证据不足和过早宣称完成等问题。

GoalAnchor 提供 Skill、Python 库和命令行工具。它帮助检查计划与记录是否符合规则，但不能替代真实测试、外部事实核验或工程判断。

## 主要功能

- **目标与执行门控**：行动前检查目标、逻辑、假设、最小路径和方案结构；执行前固定可观察的验收条件。
- **证据与交付闭环**：记录证据来源、实验条件、结论强度和交付状态，避免把“测试通过”误当成“问题已解决”。
- **多 Agent 审核**：Reviewer 并行审查，Arbiter 汇总，Meta Reviewer 复核；结果包含可读取的角色、模型和轮次记录。
- **Skill 与 CLI**：把 GoalAnchor Skill 安装到支持的 Agent 宿主；用 CLI 执行确定性检查、查看平台配置或运行审核。
- **PI-Desktop 插件**：提供桌面插件、Python 规则桥接、模型审核和可主动开启的工具门控。宿主适配细节见[插件指南](goalanchor/_pi_desktop/README.md)。

## 运行环境

- **Python 3.11 或更新版本**，支持 Windows、macOS 和 Linux。
- GoalAnchor 的 Python 运行时不依赖第三方 Python 包。
- 使用内置 CLI Agent 调度时，需要自行安装并完成对应 Agent CLI 的登录。GoalAnchor 不会替你安装这些 Agent 或管理它们的凭据。
- PI-Desktop 插件需要宿主进程能找到 Python。桌面开发测试另外需要 Node.js 20 或更新版本。

## 安装

### 从 GitHub 安装 CLI

在仓库根目录创建虚拟环境：

~~~sh
git clone https://github.com/chenyuyu6666/GoalAnchor.git
cd GoalAnchor
python -m venv .venv
~~~

Windows PowerShell：

~~~powershell
.\.venv\Scripts\Activate.ps1
~~~

macOS / Linux：

~~~sh
source .venv/bin/activate
~~~

激活虚拟环境后安装：

~~~sh
python -m pip install .
~~~

### 安装 GoalAnchor Skill

下面以 Codex 用户目录为例：

~~~sh
goalanchor install --target codex --scope user
~~~

安装到项目目录：

~~~sh
goalanchor install --target claude --scope project --project-root .
~~~

安装完成后，在宿主中新建或刷新会话，然后让 Agent 使用 GoalAnchor 检查已形成的计划、改动或结论。安装目标见下方平台表。

## 快速使用

查看命令和本机可用的内置 Agent CLI。此命令只报告环境，不会启动 Agent：

~~~sh
goalanchor --help
goalanchor platforms --json
~~~

运行仓库自带的离线审核示例。它使用本地 echo Agent，不调用外部模型：

~~~sh
goalanchor review-cycle --request examples/multi-platform-review/request.json --config examples/multi-platform-review/config.json
~~~

在支持的 Agent 宿主中安装 Skill 后，可以这样开始一次任务：

> 请使用 GoalAnchor 审核这份实施方案。先明确 TASK_OBJECTIVE、CURRENT_BLOCKING_UNCERTAINTY 和 PROPOSED_STEP_AND_DECISION_LINK，再按 PLAN 阶段顺序检查 Q1 Goal、Q2 Logic、Q3 Path、Q4 Minimum、Q5 Structure。若通过，进入 VERIFY 并先固定验收条件，然后按 PLAN → VERIFY → EXECUTE 推进；报告未验证的事实。

真实平台探测命令是 <code>goalanchor platforms --probe --json</code>。它会调用已安装的 Agent CLI，可能产生模型用量。

## 支持的平台

下表区分 Skill 文件安装位置、内置 Agent 调度器和独立桌面插件。**Skill 安装目标只表示文件放置方式，不代表该宿主内置了 Agent 调度适配。**

| 平台 / 宿主 | Skill 安装目标 | Agent 调度或插件方式 |
|---|---|---|
| Codex CLI | <code>codex</code> | 内置 CLI 调度器；需单独安装并登录 Codex CLI |
| Claude Code | <code>claude</code> | 内置 CLI 调度器；需单独安装并登录 Claude CLI |
| Gemini CLI | <code>gemini</code> | 内置 CLI 调度器；需单独安装并登录 Gemini CLI |
| OpenCode | <code>opencode</code> | 内置 CLI 调度器；需单独安装并登录 OpenCode CLI |
| Cursor | <code>cursor</code> | 可安装 Skill；Agent 执行需自定义命令或 JSON-over-stdio 桥接 |
| Pi CLI | <code>pi</code> | 可安装 Skill；这是 Pi CLI，不是 PI-Desktop |
| DeepSeek Harness (DSH) | <code>dsh</code> | 可安装 Skill；Agent 执行需自定义命令或桥接 |
| Agent Skills 通用目录 | <code>agents</code> | 将 Skill 放入共享目录；Agent 调度由宿主或自定义配置提供 |
| Zcode | <code>zcode</code>，需搭配 <code>--destination</code> | 需指定 Skill 目录；Agent 调度需自定义接入 |
| 其他宿主 | <code>custom</code>，需搭配 <code>--destination</code> | 可配置自定义 CLI 命令或 JSON-over-stdio Agent |
| PI-Desktop | 单独构建 <code>.piplug</code> 插件 | 独立 Extension 和 Python 桥接；不使用 <code>--target pi</code> 安装 |

内置调度器目前是 Codex、Claude、Gemini 和 OpenCode。自定义命令和 JSON-over-stdio 桥接可用于其他 Agent。PI-Desktop 使用独立插件，并按宿主公开接口检测能力；具体验证基线和边界见[适配规格](docs/PI-Desktop适配规格.md)与[验证记录](docs/PI-Desktop验证记录.md)。

构建 PI-Desktop 插件：

~~~sh
python -m goalanchor.pi_desktop build --destination build/pi-desktop --archive build/goalanchor.piplug
~~~

详细安装和权限说明见[PI-Desktop 插件指南](goalanchor/_pi_desktop/README.md)。

## 更多资料

- [离线多 Agent 审核示例](examples/multi-platform-review/README.md)
- [变更记录](CHANGELOG.md) · [GitHub Releases](https://github.com/chenyuyu6666/GoalAnchor/releases)
- [GitHub 主仓库](https://github.com/chenyuyu6666/GoalAnchor) · [Gitee 镜像](https://gitee.com/chen-yuyu6666/goal-anchor)
- [贡献指南](CONTRIBUTING.md)
- [许可证](LICENSE) · [第三方声明](THIRD_PARTY_NOTICES.md) · [上游来源记录](UPSTREAM_SOURCES.md)

第三方 Skill 和快照保留各自许可；项目根目录的许可证不自动覆盖第三方内容。
