# GoalAnchor v0.10.3

PI-Desktop 工具门控首次真实可用。0.9.4 记录的「运行时拦截未生效」缺陷已定位根因并修复，完整闭环在宿主上取得可复现证据。

## 修复的两层缺陷

1. **武装通路断裂**：composer 键入的斜杠文本被宿主作为普通 prompt 发给模型，扩展命令处理器只从命令面板可达——`GuardState.enable()` 从未执行，模型「Guard 已启用」的叙述与运行时事实脱节。
2. **跨回合状态失忆**：宿主按回合重建扩展运行时，工厂局部 GuardState 每回合被重置——即使武装成功，下一回合的 `tool_call` 也看不到武装状态（欺骗探针实测：未批准 Bash 成功执行）。

## 修复方式（`guard.cjs` 零改动）

- GuardState 提升为 **extension 模块级 `Map<sessionId, GuardState>`**：依赖宿主 runner 的模块级工厂缓存，工厂每回合重调而模块级变量跨回合存活；新会话（新 id）默认未武装。
- `before_agent_start` 钩子：精确 prompt `/goalanchor-guard on` 武装；武装通知每回合重注入并携带进程实例标识；prompt（含模型组合的委托 prompt）只能武装、不能解除。
- composer 批准分支：`/goalanchor-approve <记录>.json` → 工作区 jail 读记录 → 本地 preflight → 真实宿主确认对话框（标注 prompt-initiated）→ 人工点击才授予；28 KiB 显示上限失败封闭；所有失败分支返回诚实通知。
- **纪元配对授予存活**：授予绑定回合纪元，`agent_end` 仅在纪元配对时跳过一次撤销（授予活过批准回合自身的结束，供下一回合消费），此后正常撤销；宿主丢弃 `agent_end` 时漂移有界。palette 授予清除配对，stale 保护不变。

## 宿主验证（PI-Desktop 0.22.0，session `99f98d1e`）

| 步骤 | 证据 |
|---|---|
| 批准前拦截 | Bash 尝试被 `tool_call` 钩子拦截，5 ms 返回，Bash 未执行 |
| 武装 | composer `/goalanchor-guard on` → 宿主确认 Guard 已启用 |
| 批准 | `/goalanchor-approve guard-probe-approval.json` → 真实确认对话框（完整契约：目标/约束/验收/范围/恢复路径/模型/操作）→ 人工点击「是」 |
| 放行 | 已批准 Bash 真实执行：`exitCode 0`，`stdout: GUARD_PROBE_OK`，701 ms |
| 一次性消费 | 第二次同命令调用被拦截（`no exact approval`） |
| closure gate（正向） | 有效 Root Fix 断言 → `{findings: [], passed: true}`（735 ms） |
| closure gate（反向） | 超额 Product Closure 无 artifact manifest → `passed: false`，`ARTIFACT_MANIFEST_REQUIRED`，`recommended_level: Build Success` |

## 验证与质量门禁

- Node 测试 **21/21**（含跨工厂调用持久化、会话隔离、缺身份 fail-closed、禁解除、基座系统提示逐字保留、纪元配对三项）
- pytest **1305 passed / 0 failed**；ruff、mypy（42 文件）、fixtures 12/12 全部通过
- 插件包：`build/goalanchor-0.10.3.piplug`，SHA-256 `d9653edac7b7f189841fa5a8709cec1280a0f878e362b9ed1598b9ff33f42288`

## 未验证边界（不宣称）

- Strict 完整生命周期（UNKNOWN/reconcile/closure 依赖持久 store）：桌面插件只暴露确定性 gate 面，不暴露 store
- 执行 receipt 持久化形态；可信 principal/account 溯源（宿主不提供稳定非 secret 身份绑定）；宿主端 review-cycle gate
- ZCode/Claude/DSH 宿主闭环仍受外部条件阻塞（身份绑定 / 402 余额 / API key）
- 插件重载或宿主重启会静默复位门禁（模块 Map 清空）；每回合武装通知消失即为复位信号
