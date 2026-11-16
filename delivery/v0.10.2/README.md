# v0.10.2 交付目录

本目录是 v0.10.2 的权威交付目录。发行包（wheel/sdist）经独立的 `chore(delivery)` 提交在 Release 后入库；tag 内本目录只包含 README 与发布说明。

GitHub Release：[v0.10.2](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.10.2)

## 版本边界

- 版本类型：`MINOR`（首个 0.10 系列发布，承载 0.10.0 引入的新能力）。0.10.0 与 0.10.1 是未发布的中间开发态，从未打 tag 或建 Release。
- 变更范围：`goalanchor/_pi_desktop/`（extension 行为与版本）、`tests/js/test_pi_desktop.cjs`（21 项）、状态与证据文档；`guard.cjs` 拦截语义**零改动**，无 manifest/权限/schema 变化。
- 已验证（PI-Desktop 0.22.0 真实会话，session `99f98d1e`）：
  - **Guard 运行时闭环**：未批准 Bash 被 `tool_call` 钩子拦截（5 ms）→ composer `/goalanchor-approve <记录>.json` 经宿主路由到面板扩展命令路径 → 真实 `ui.confirm` 对话框（完整契约）→ 人工点击授予 → 已批准 Bash 真实执行（exitCode 0，stdout `GUARD_PROBE_OK`，701 ms）→ 第二次同命令再拦（一次性消费）。
  - **closure gate 双向**：有效 Root Fix 断言 `passed=true`；超额 Product Closure 无 artifact manifest → `ARTIFACT_MANIFEST_REQUIRED` + `recommended_level=Build Success`。
- 未验证（如实记录）：Strict 完整生命周期（UNKNOWN/reconcile/closure 依赖持久 store，桌面插件只暴露确定性 gate 面）、执行 receipt 持久化形态、可信 principal/account 溯源（宿主无稳定非 secret 身份绑定）、宿主端 review-cycle gate；ZCode/Claude/DSH 宿主闭环仍受外部条件阻塞。
- Gitee 镜像 refs 同步；Release 页面未创建（需 access token）。

详细变更见 [CHANGELOG](../../CHANGELOG.md) 与 [release-notes](release-notes.md)。
