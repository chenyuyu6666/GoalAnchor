# v0.9.4 交付目录

本目录是 v0.9.4 的权威交付目录。发行包（wheel/sdist）经独立的 `chore(delivery)` 提交在 Release 后入库；tag 内本目录只包含 README 与发布说明。

GitHub Release：[v0.9.4](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.9.4)

## 版本边界

- 版本类型：PATCH；**无产品代码变更**（`goalanchor/`、`tests/`、`scripts/` 相对 0.9.3 零差异），变更全部为验证证据与规范文档。
- **已知缺陷（未修复，如实记录）**：PI-Desktop 0.22.0 上 Guard 启用成功后运行时拦截未生效——Bash 探针在无批准的情况下正常执行。根因候选与证据见 `audit/pi-desktop-runtime-20261113.json`。该缺陷修复前，PI-Desktop 的 Guard 运行时拦截不能宣称可用。
- 已验证：PI-Desktop 0.22.0 插件 0.9.3（从 0.6.0 升级）；首个成功审核轮次（LOW 单轮 1m 9s，WARN）；`/goalanchor-guard on` 执行成功（宿主确认 Guard 已启用）。
- 原生宿主整体状态不变：Codex 深层闭环已验证；ZCode/Claude/DSH 未闭合。
- Gitee 镜像 refs 同步；Release 页面未创建（需 access token）。

详细变更见 [CHANGELOG](../../CHANGELOG.md) 与 [release-notes](release-notes.md)。
