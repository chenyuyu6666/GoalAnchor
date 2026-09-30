# GoalAnchor Agent 兼容性

GoalAnchor 的规则入口是标准的 `SKILL.md`：YAML frontmatter 只使用 `name` 和 `description`，正文只描述规则、输入、决策和验证，不调用任何宿主专用 API。

## 支持层级

| 层级 | 目标 | 方式 |
|---|---|---|
| 规则兼容 | 规则语义保持一致 | 同一份 `skills/goalanchor/SKILL.md` |
| 发现兼容 | 宿主能自动找到 Skill | 安装到宿主对应的 `skills/<name>/SKILL.md` 根目录 |
| 执行兼容 | 不依赖宿主工具名 | 使用自然语言规则和可选 Python CLI |
| 结果兼容 | 结果可比较 | 使用统一的 Goal Contract、Evidence 和 Closure 字段 |

不同 Agent 的模型、权限、上下文和工具集仍可能不同；本项目保证的是文件格式、规则入口、目录映射和确定性校验，不保证模型输出逐字相同。

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

### 其他 Agent

优先使用 `.agents/skills/goalanchor/SKILL.md` 或工具文档指定的等价目录。若宿主只支持自定义目录，使用 `--target custom --destination`，并保留 `SKILL.md` 文件名和 `name: goalanchor`。

### Cursor、Gemini CLI、OpenCode

三者都支持标准 `SKILL.md` 目录形式，项目级默认入口分别是 `.cursor/skills`、`.gemini/skills` 和 `.opencode/skills`；本仓库的 `cursor`、`gemini`、`opencode` target 会生成对应目录。它们的其他配置（权限、信任、自动调用开关）仍由各宿主自己的配置控制，不由 GoalAnchor CLI 越权修改。表中的用户目录是宿主默认值，不额外假设宿主支持自定义环境变量。

### Pi

Pi 支持 `.pi/skills`、`.agents/skills` 和 `~/.pi/agent/skills`。本仓库的 `pi` target 使用 Pi 原生目录；也可以使用 `agents` target 复用中立目录。当前机器未安装 Pi，因此本地记录只覆盖目录投影和文件哈希，未覆盖 Pi 运行时自动发现。
Pi 的显式调用名为 `/skill:goalanchor`。

### ChatGPT / OpenAI Skills API

ChatGPT 的工作区 Skill 和 OpenAI Skills API 属于上传/版本 API，不是本地目录自动发现。可以把安装器复制出的 Skill 目录压缩后上传，或通过对应 API 创建版本；本仓库不在本地 CLI 中伪造账号、上传和权限操作。

## 兼容性检查

```text
python -m goalanchor check --target agents --scope project --project-root . --source skills/goalanchor --json
```

检查失败时先确认：目标目录是否正确、Skill 是否被旧版本覆盖、`SKILL.md` 是否位于目录第一层，以及源文件和目标文件 SHA-256 是否一致。
