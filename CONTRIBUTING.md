# 贡献与开发规范

## 分支策略

- `master` 是唯一稳定集成与发布分支，必须开启保护并通过 PR 合并。
- `feature/<短名>`、`fix/<短名>` 和 `chore/<短名>` 是短期工作分支，均从最新
  `master` 创建，完成后通过 PR 合并并保留明确的合并提交，不使用 Squash。
- `hotfix/<短名>` 只用于线上紧急修复，从受影响的稳定标签或 `master` 创建，
  修复后通过 PR 合并回 `master`。
- `develop/v0.4.0` 是旧版过渡分支，已停止使用；其历史由 `history/*` 标签保留。
- `release/*` 不作为常规分支；只有需要长期稳定窗口时才创建。
- 历史标签 `history/*` 只读，用于保存被聚合或删除分支的原始提交；日常开发不在其上
  创建分支，也不移动已发布标签。
- 不直接在 `master` 上开发；合并前必须通过本地测试和 CI。

## 规范仓库与镜像

- GitHub `chenyuyu6666/goal-anchor` 是唯一规范仓库，`master` 上的 PR、Issue 和发布均在 GitHub 管理。
- Gitee `chen-yuyu6666/goal-anchor` 是 GitHub → Gitee 的单向镜像。不要向 Gitee 镜像提交变更；如自动镜像不可用，由维护者在 GitHub 合并后同步 `master` 和 `v*` 版本标签。
- 本地 `origin` 指向 GitHub；如需从 Gitee 下载，可添加只读远端 `gitee`。不要使用 `git push --mirror`，以免推送备份分支或历史标签。

常用操作：

```powershell
git switch master
git pull --ff-only origin master  # origin is the canonical GitHub repository
git switch -c feature/evidence-gate
git push -u origin feature/evidence-gate
```

PR 合并并通过 CI 后删除远程和本地工作分支；PR 页面和提交历史仍可追溯。
分支名不绑定版本号，版本边界由发布标签和 `CHANGELOG.md` 统一管理。

## 本地质量检查

从仓库根目录准备并运行与 CI 对齐的 Python 检查：

```powershell
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m pip install ruff mypy==2.4.0
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest -q
$goalAnchorRevision = (git rev-parse HEAD).Trim()
python scripts/evaluate_fixtures.py --fixtures tests/fixtures/regression --revision $goalAnchorRevision
python scripts/run_test_task.py --task docs/GoalAnchor-自动测试任务.md
python scripts/sync_skill_bundle.py --check
python -m ruff check goalanchor tests scripts
python -m mypy goalanchor
```

回归夹具会在子进程中导入 GoalAnchor，因此测试前需要执行 editable install。CI 将 MyPy 2.4.0 作为阻断检查；本地使用相同版本可避免检查结果因版本差异而变化。

改动涉及 JavaScript 或 PI-Desktop 桥接时，使用 Node.js 20 运行：

```powershell
node --check skills/.system/openai-docs/scripts/fetch-codex-manual.mjs
node --check skills/.system/openai-docs/scripts/resolve-latest-model-info.cjs
node --test tests/js/test_pi_desktop.cjs
```

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

允许的日常提交类型：

- `feat`：新增能力；
- `fix`：修复缺陷；
- `docs`：文档或变更记录；
- `test`：测试变更；
- `refactor`：不改变行为的重构；
- `chore`：依赖、CI 或其他工程维护。

`scope` 使用具体模块，例如 `core`、`gates`、`evidence`、`cli`、`ci`。
提交正文应说明影响范围、关键决策和实际验证命令；不把多个无关改动放在同一提交中。

规则：

- 日常提交标题使用 `type(scope): 中文说明`，版本号不写入每个开发提交；
- 发布聚合提交标题使用 `vX.Y.Z 中文说明`，且必须与 Git 标签一致；
- 一次提交只做一件可说明和可回滚的事情；
- 提交正文记录影响范围、关键决策和实际验证命令；
- 已发布提交和版本标签不移动、不复用；历史整理不能改写发布边界。
  修订发布使用新版本和新标签，保留原有工作提交、合并节点及发布聚合提交。

## 版本

版本遵循语义化版本（SemVer）：`MAJOR.MINOR.PATCH`。

- `v0.1.0`：初始 skills 与 Plan Audit 基线。
- `v0.2.0`：加入 Plan Audit v2 目标与验证约束。
- `v0.3.0`：收紧 Plan Audit v3 诊断边界。
- `v0.3.1`：统一提交标题与版本管理规范。
- `v0.3.2`：简化多人协作流程。
- `v0.3.3`：按 GitHub Flow 重构协作管理。
- `v0.3.4`：明确 Git 提交规则。
- `v0.4.0`：多 Agent 审核协议、多平台 Agent 调度与治理收紧。
- `v0.4.1`：SKILL.md 门控边界澄清（量化验收标准归属 VERIFY 入口）。
- `v0.5.0`：子 Agent 模型一致性强制与 warnings 机器可读告警通道。
- `v0.5.1`：告警措辞修正、flag 拼写防护扩展、配置加载期 argv 预检、probe 钉定（GA-V050-AUDIT 审核修复）。
- `v0.6.0`：platforms 拆子包、wheel 携带 Skill、ruff/mypy 门禁、Gitee Go 流水线、分发与工程优化。
- `v0.7.0`：PI-Desktop Skill/Extension 插件、同模型独立审核、显式工具门控及真实桌面验收。
- 新功能增加 `MINOR`，兼容性修复增加 `PATCH`，破坏性变更增加 `MAJOR`。
- 运行时版本以 `goalanchor/__init__.py` 导出的 `__version__` 为单一来源。
- 发布前更新 `CHANGELOG.md`，在 `master` 上使用一个发布聚合提交和同名 `vX.Y.Z` 标签，
  并推送提交和标签。

发布示例：

```powershell
python -m pytest -q
git commit -m "v0.3.4 明确 Git 提交规则"
git tag -a v0.3.4 -m "v0.3.4 明确 Git 提交规则"
git push origin master --follow-tags
```

已发布标签不移动、不复用；修订发布使用新的补丁版本。
