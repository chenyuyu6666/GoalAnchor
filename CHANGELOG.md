# 变更记录

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [未发布]

- 阶段 2 已实现：增加 Q1–Q5、VERIFY、EXECUTE 和 PLAN_CHANGED 的纯 Python 闸门及 CLI。
- 阶段 3 已实现：增加 EvidenceRecord、ExperimentPlan、结论强度、观察窗口、混杂变量和设计必要性校验。
- 阶段 4 已实现：增加 ArtifactManifest、六节点 ProvenanceChain、闭环等级和安全降级校验。
- 阶段 5 已实现：增加 Review/Graphify 结果适配、来源与不确定性边界及统一 CLI 输入校验。

## [0.3.2] - 2026-09-08

### Changed

- 简化多人协作流程：日常使用 `master` 与短期工作分支，发布使用 PR Squash 和 SemVer 标签。
- 保留现有 `archive/*` 分支作为历史归档，不再为每个版本创建长期开发分支。
- 日常开发提交使用 `type(scope): 中文说明`，只有发布聚合提交带版本号。

### 文档驱动测试任务

- 新增 `docs/GoalAnchor-自动测试任务.md`，统一定义核心测试目标、检查范围、退出码和扩展规则。
- 新增 `scripts/run_test_task.py`，读取 Markdown 中的 JSON 测试定义，执行 pytest、固定回归夹具、GoalAnchor CLI 和关键文件检查。
- 测试运行器输出带任务哈希、提交版本、命令、耗时、日志和 JSON 断言结果的 `test-results` 报告。
- 新增 `docs/GoalAnchor-对话体验测试任务.md`，将真实对话体验与代码回归测试分开，覆盖 9 个施压、边界、正例和工具观察案例。
- 阶段 6 已实现固定回归夹具、版本比较器和跨平台 CI 工作流；CI 实际运行记录仍待补齐。
- 阶段 5 已实现：增加 Review/Graphify 结果适配、来源与不确定性边界及统一 CLI 输入校验。

## [0.3.1] - 2026-09-06

### Documentation

- 统一后续提交标题为 `vX.Y.Z 中文变更说明`，并要求标题版本号与 Git 标签一致。
- 明确版本递增、提交正文、发布标签和历史提交保护规则。

## [0.3.0] - 2026-08-29

### Changed

- 收紧 Plan Audit v3 诊断边界，明确调查、诊断和 A/B 步骤的阻塞不确定性与决策影响。
- 明确普通不确定性、验证失败或方案比较不单独触发前提复核，并扩展前提复核对象。

## [0.2.0] - 2026-08-29

### Added

- 加入任务目标、阻塞不确定性和决策关联字段。
- 明确诊断逻辑、决策相关性和前提复核规则。

### Notes

- 提交标题为 `v0.2.0 加入 Plan Audit v2 目标与验证约束`。
- 后续 Plan Audit v3 提交属于 `v0.3.0`，不属于本版本内容。

## [0.1.0] - 2026-08-29

### Added

- 导入 `skills` 目录及其引用、脚本、素材和代理配置。
- 恢复 Plan Audit 基础 `PLAN -> VERIFY -> EXECUTE` 规则。
- 增加 Graphify、Deeply 及系统 Skill 快照。

### Notes

- `v0.1.0` 是最初的 skills/Plan Audit 基线。
- 后续 Plan Audit v2/v3 提交保留在 Git 历史中，作为演进记录，不属于
  `v0.1.0` 的发布内容。

## 历史提交说明

历史记录使用中文描述实际变化；从 `v0.3.1` 起，所有新提交标题统一使用
`vX.Y.Z 中文变更说明` 格式，并与对应版本标签保持一致。历史提交不会被删除或
回写，版本边界以标签和本文件为准。

[未发布]: https://gitee.com/chen-yuyu6666/goal-anchor/compare/v0.2.0...master
[0.3.1]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.1
[0.3.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.0
[0.2.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.2.0
[0.1.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.1.0
