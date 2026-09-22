# 变更记录

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [未发布] - 2026-09-22

### Added

- 机器可读告警通道:`review-cycle`(含别名 `review-run`)与 `review-cycle --dry-run` 的 JSON 输出新增顶层 `warnings` 数组(恒为数组,可为空,形状确定)。`MODEL_NOT_PINNED` 标记未钉定主模型的平台调度(stderr 人类可读告警保留),`MODEL_DIVERGENCE_APPROVED` 标记配置携带用户确认的 `model_divergence`(条目回显 `confirmed_by`);stdio bridge 按设计边界不产生告警。只读 stdout 的自动化流水线由此获得固定的审计锚点,不再依赖 stderr 文本或 usage_summary 内层字段。

### Fixed

- 多平台调度模型一致性(紧急修复):部分平台在子 Agent 未指定模型时回落到自身默认模型,导致与主 Agent 模型不一致、审核失败。现在子 Agent 强制与主 Agent 同模型:配置顶层 `"model"` 或 `review-cycle --model <主模型>`(覆盖配置,每次运行重传当前值即实现主模型中途切换后的同步)声明主模型,内置平台按各自 flag 注入(`codex -m`、`claude --model`、`gemini -m`、`opencode -m`),自定义平台支持 `argv` 内 `{model}` 占位符或默认 `-m` 插入(`platforms.<name>.model_flag` 可覆盖);生效优先级 profile `model` > `platforms.<name>.model` > 顶层 `"model"`。
- 未经用户确认的模型分歧在配置加载阶段即拒绝(`review-cycle` 与 `--dry-run` 均预检),不会启动任何 Agent;确需不同模型必须先暂停询问用户,并将决定记录为 `"model_divergence": {"confirmed_by": "...", "reason": "..."}`,确认信息回显进运行输出供审计。
- 模型审计:每条 `agent_bindings` 记录实际 `model`(stdio bridge 恒为 null),`usage_summary` 新增 `models_used`;`platforms` 报告输出 `model_flag`/`main_model`/`model_divergence`;`probe_platform` 支持 `model` 参数以按钉定模型探测。SKILL.md 派发序列新增"Pin the model"步骤(逐轮复检主模型、切换即同步、分歧先询问),`agent-runtime.md` 与示例文档同步补充模型一致性章节。
- 关闭三条静默失效路径(审查发现):①`argv` 硬编码模型 flag 时注入会产生重复 flag、末尾者(硬编码值)生效而静默推翻钉定——现在直接拒绝并要求改用 `{model}` 占位符;②stdio bridge 配置上声明的 `model` 被静默忽略——桥接协议不携带模型字段,现在直接报错;③未声明任何模型的平台调度无任何提示——现在向 stderr 打印告警,且 `usage_summary` 新增 `main_model` 字段(未声明为 null)供审计。

### Tests

- 新增多轮压测:独立脚本 `scripts/stress_model_sync.py`(全离线,6 场景可调强度:争议轮全角色重拉、主模型切换同步、并发派发、失败重试逐 attempt 校验、分歧拦截 fuzz、整进程 CLI 端到端),默认强度一轮 709 项校验零失败;`tests/test_stress_model_sync.py` 以小强度子集进 CI(全套 195 passed)。
- warnings 通道测试:未钉定结构化告警、已钉定零噪声、分歧确认告警回显 `confirmed_by`、dry-run 同通道(未钉定/已钉定两腿)、legacy bridge 收集器边界(196 passed,同步 `validate-multi-round.ps1` 基线 195→196)。
- 压测 CLI 端到端场景每轮增加 pinned `warnings==[]` 与去 model 配置副本 `MODEL_NOT_PINNED`+`main_model=null` 断言(默认强度一轮 739 项校验零失败)。

## [0.4.1] - 2026-09-20

### Fixed

- SKILL.md 门控边界澄清:DIALOGUE-07 对话体验测试暴露"量化验收标准归属"歧义(R1/R2 PASS、R3 FAIL)。Q1 明确语义化验收在目标/blocker/决策链接完整时成立,PLAN 阶段缺量化阈值不是有效 FAIL 理由;VERIFY 明确量化阈值在首个 EXIT_CRITERION 固定,无它启动动作是 VERIFY 违规。
- 澄清后 DIALOGUE-07 复测 3/3 一致 PASS(见 `Doc/validation/2026-09-20-dialogue-v0.4.1-case07-retest.md`)。

## [0.4.0] - 2026-09-20

### v0.4.0 目标内容

- 增加生命周期状态 `HUMAN_DECISION_REQUIRED` 和 `STOPPED`。
- 增加五 Gate 兼容映射、DecisionRecord、轻量 Authorization 和 Verification 闭环。
- 增加有限 Multi-Agent Review 协议：统一上下文、Reviewer、Arbiter、DisputePacket、Meta Review、DecisionReport 和 JSON CLI 入口。
- 增加平台无关的命名 Agent Profile/Dispatcher、Reviewer 并行调度及 JSON-over-stdio 桥接协议。
- 增加 Phase 6 固定 Benchmark 与开发者最小示例。

### 执行层与治理收紧（2026-09-20）

- 多平台 Agent 调度适配：内置 `codex`/`claude`/`gemini`/`opencode`（提示词走 stdin，规避 Windows `.cmd` 转义与 argv 上限），codex 回显解析、平台 env/超时/重试（含空 stdout）、`platforms --probe` 环境与认证自检。
- 宿主可见调度路径：`review-prompt` 逐角色渲染 + 宿主原生子 Agent 拉起 + `gate --phase review-cycle` 确定性校验；无头 `review-cycle`（别名 `review-run`）降为备用。
- 多审强制策略：风险深度 `LOW=1 / MEDIUM=2 / HIGH=2 / CRITICAL=3`，LOW 不再免审。
- Meta Reviewer 可核验：请求携带本轮 Reviewer 原始结果；HIGH/CRITICAL 下 Meta 裁决与 Arbiter 裁决不一致视为争议。
- CRITICAL 强制 `risk_declaration` 签署（`declared_by`/`reason`），缺失即拒绝且不调用任何 Agent。
- 审计与对账：`agent_bindings`（profile/平台/轮次/attempts/`tokens_used`）并入 `ReviewServiceResult`，`review-cycle` 输出 `usage_summary`；瞬时失败可配 `retries` 重试。
- 提示词支持 `"language": "zh"` 中文模板（schema 与精确取值行保持机器锚定）；`review-cycle --dry-run` 预览。
- 新增 `examples/multi-platform-review/`（离线 echo Agent、离线/真实平台配置）与平台实测记录（claude 2.1.272 / codex-cli 0.154.0 真机全链路）。
- `agent_runtime` Meta 回调签名新增本轮 Reviewer 结果参数（破坏性变更，调用点已同步）。

- 阶段 2 已实现：增加 Q1–Q5、VERIFY、EXECUTE 和 PLAN_CHANGED 的纯 Python 闸门及 CLI。
- 阶段 3 已实现：增加 EvidenceRecord、ExperimentPlan、结论强度、观察窗口、混杂变量和设计必要性校验。
- 阶段 4 已实现：增加 ArtifactManifest、六节点 ProvenanceChain、闭环等级和安全降级校验。
- 阶段 5 已实现：增加 Review/Graphify 结果适配、来源与不确定性边界及统一 CLI 输入校验。

## [0.3.4] - 2026-09-08

### Documentation

- 明确日常提交类型、scope、提交正文和验证记录要求。
- 明确发布聚合提交与 SemVer 标签的对应关系。

## [0.3.3] - 2026-09-08

### Changed

- 按 GitHub Flow 重构协作管理：日常使用 `master` 和短期 PR 分支，合并后删除工作分支。
- 将历史提交转为只读 `history/*` 标签，保留原始提交但不占用日常分支。
- 明确日常提交与发布提交的标题职责，避免每个开发提交绑定版本号。

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

历史记录使用中文描述实际变化；日常提交使用 `type(scope): 中文说明`，发布聚合提交
使用 `vX.Y.Z 中文变更说明`，并与对应版本标签保持一致。历史提交不会被删除或回写，
版本边界以标签和本文件为准。

[未发布]: https://gitee.com/chen-yuyu6666/goal-anchor/compare/v0.4.1...master
[0.4.1]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.4.1
[0.4.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.4.0
[0.3.4]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.4
[0.3.3]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.3
[0.3.1]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.1
[0.3.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.3.0
[0.2.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.2.0
[0.1.0]: https://gitee.com/chen-yuyu6666/goal-anchor/releases/tag/v0.1.0
