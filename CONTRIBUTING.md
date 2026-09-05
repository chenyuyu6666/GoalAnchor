# 贡献与开发规范

## 分支

- `master` 是默认稳定分支，只接受通过检查的合并。
- 新功能使用 `feature/<短名>`，缺陷修复使用 `fix/<短名>`，文档或工程维护使用 `chore/<短名>`。
- 不直接在 `master` 上开发；合并前必须通过本地测试和 CI。

## 提交标题

所有后续提交的标题统一以版本号开头，并使用中文说明实际变化：

```text
vX.Y.Z <简短变更说明>
```

示例：

```text
v0.2.0 加入 Plan Audit v2 目标与验证约束
v0.3.0 收紧 Plan Audit v3 诊断边界
v0.3.1 更新提交与版本管理规范
```

规则：

- 标题第一个字段必须是完整 SemVer 标签，例如 `v0.3.1`；
- 版本号后直接写可读的中文变更说明，不使用没有版本前缀的标题；
- 一次提交只做一件可说明和可回滚的事情；
- 提交正文记录影响范围、关键决策和实际验证命令；
- 不修改已发布提交的标题或标签，修订使用新的版本号。

## 版本

版本遵循语义化版本（SemVer）：`MAJOR.MINOR.PATCH`。

- `v0.1.0`：初始 skills 与 Plan Audit 基线。
- `v0.2.0`：加入 Plan Audit v2 目标与验证约束。
- `v0.3.0`：收紧 Plan Audit v3 诊断边界。
- 新功能增加 `MINOR`，兼容性修复增加 `PATCH`，破坏性变更增加 `MAJOR`。
- 运行时版本以 `goalanchor/__init__.py` 导出的 `__version__` 为单一来源。
- 发布前更新 `CHANGELOG.md`，让提交标题和标签使用同一个 `vX.Y.Z`，并推送提交和标签。

示例：

```powershell
python -m pytest -q
git commit -m "v0.3.1 更新提交与版本管理规范"
git tag -a v0.3.1 -m "v0.3.1 更新提交与版本管理规范"
git push origin master --follow-tags
```

已发布标签不移动、不复用；修订发布使用新的补丁版本。
