# 贡献与开发规范

## 分支策略

- `master` 是唯一稳定集成与发布分支，必须开启保护并通过 PR 合并。
- `feature/<短名>`、`fix/<短名>` 和 `chore/<短名>` 是短期工作分支，均从最新
  `master` 创建，完成后通过 PR 合并并 Squash。
- `hotfix/<短名>` 只用于线上紧急修复，从受影响的稳定标签或 `master` 创建，
  修复后通过 PR 合并回 `master`。
- `develop/v0.4.0` 是旧版过渡分支，不再作为日常开发入口；新工作统一从 `master`
  创建短期分支。
- `release/*` 和 `archive/*` 不作为常规分支；只有需要冻结发布或重写历史时才创建。
- `archive/vX.Y.Z-history` 用于保存被聚合的原始提交；聚合前必须先推送归档分支，
  确保每个原始提交仍可追溯。
- 不直接在 `master` 上开发；合并前必须通过本地测试和 CI。

常用操作：

```powershell
git switch master
git pull --ff-only origin master
git switch -c feature/evidence-gate
git push -u origin feature/evidence-gate
```

分支名不绑定版本号，版本边界由发布标签和 `CHANGELOG.md` 统一管理。

## 提交标题

日常开发提交使用 Conventional Commits；只有合并到 `master` 的发布聚合提交带版本号：

```text
<type>(<scope>): <简短变更说明>
```

示例：

```text
feat(core): 增加证据校验
fix(cli): 修复导入错误
docs(process): 更新协作流程
v0.3.2 简化多人协作流程
```

规则：

- 日常提交标题使用 `type(scope): 中文说明`，版本号不写入每个开发提交；
- 发布聚合提交标题使用 `vX.Y.Z 中文说明`，且必须与 Git 标签一致；
- 一次提交只做一件可说明和可回滚的事情；
- 提交正文记录影响范围、关键决策和实际验证命令；
- 已发布版本默认不移动；如果为了清理主线而聚合历史，必须先创建并推送对应的
  `archive/*-history` 分支，再同步重写派生分支和版本标签。

## 版本

版本遵循语义化版本（SemVer）：`MAJOR.MINOR.PATCH`。

- `v0.1.0`：初始 skills 与 Plan Audit 基线。
- `v0.2.0`：加入 Plan Audit v2 目标与验证约束。
- `v0.3.0`：收紧 Plan Audit v3 诊断边界。
- `v0.3.1`：统一提交标题与版本管理规范。
- `v0.3.2`：简化多人协作流程。
- `v0.4.0`：下一版本目标，暂未发布。
- 新功能增加 `MINOR`，兼容性修复增加 `PATCH`，破坏性变更增加 `MAJOR`。
- 运行时版本以 `goalanchor/__init__.py` 导出的 `__version__` 为单一来源。
- 发布前更新 `CHANGELOG.md`，使用一个发布聚合提交和同名 `vX.Y.Z` 标签，并推送提交和标签。

发布示例：

```powershell
python -m pytest -q
git commit -m "v0.3.2 简化多人协作流程"
git tag -a v0.3.2 -m "v0.3.2 简化多人协作流程"
git push origin master --follow-tags
```

已发布标签不移动、不复用；修订发布使用新的补丁版本。
