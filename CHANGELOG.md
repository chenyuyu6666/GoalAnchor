# 变更记录

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [未发布]

### Added

- 显式 `resume_transport`：一次新用户许可、原授权/backend/key 冻结、可信幂等测试证据和两段 CAS；原 UNKNOWN 仍需独立 reconcile。`reuse_observation` 绑定完全相同的 criterion、当前对象摘要与原观察来源链，旧授权不复活；presentation 更新仅限 title。
- host binding 冻结可信 embedding 的版本/model/mode/Hook 事实，变化后失效，不自证原生 Hook。`review_packets` 与 session/digest/DELIVERED 回执强制绑定；synthetic 回执不证明模型阅读。五 host 的 TextHostAdapter 共用可信文本 fixture，不等于五原生宿主验证。
- 旧授权缺少可信来源 proof 时明确返回 `LEGACY_UNVERIFIED`，无通用旧记录迁移。

- v0.8 严格运行时本地候选：内容绑定基线、可信签名事件、证据快照、逐项异议处置、结构化问题、持久化一次性授权、受限 scope、回执及验收观察；既有辅助 API 保留。
- `python -m goalanchor workflow` 提供 synthetic `demo`、`capabilities`、离线 DELETE journal 的零写入 `status` / `audit`，以及允许 WAL 的显式写入 `recover`；恢复不重发操作。
- Codex App Server 控制器绑定精确 callback；0.162.1 已取得真实模型命令、问答 fixture、拒绝/取消与固定命令批准闭环证据。批准闭环使用 synthetic review 和 explicit test approval，未证明人类 UI 或独立多模型审核。
- [严格运行时使用与兼容说明](docs/workflow-v0.8.md)：可信密钥/数据库边界、cwd 非沙箱、UNKNOWN 处理和宿主证据分层。

- v0.8.0 首批：`ReviewContext.evidence_digests` 可选绑定全部证据引用的 SHA-256；校验格式和覆盖范围、冻结输入、规范化顺序，并随上下文序列化传递。无摘要的旧调用保留原指纹及 JSON 形态。

### Fixed

- 审核周期从实际 Reviewer/Arbiter 结论检测分歧，避免自报“无分歧”吞掉异议；沿既有最多两轮路径处理，一致失败无需额外复审。
- PASS 关闭前校验结果验收集合与当前 GoalContract 一致，拒绝用替换、缺失或新增的标准关闭原目标；保留阶段检查顺序及既有 WARN/FAIL、空证据策略。

### Notes

- 严格运行时是 v0.8.0 候选；源码已合入公开 GitHub master，但尚未作为正式 v0.8.0 版本发布（无 v0.8.0 tag/GitHub Release）。核心场景映射 58/58 PASS，对应宿主状态为 55 NOT_RUN、2 个共享 TextHostAdapter fixture PASS、1 个本地 controller fixture PASS，58 项原生宿主状态均 NOT_RUN。这些 fixture/受控回调不证明人类 UI 或全工具原生强制；全工具强制未覆盖，Claude / DSH / Zcode 为 ADVISORY / UNAVAILABLE。scope 的 cwd/声明路径检查不构成 shell 沙箱，控制侧须保护密钥、账本与 executor。

- 本批不是 v0.8.0 发布；首批之后增加的严格运行时按独立指南说明，不据此宣称全部规格通过。摘要由调用方提供，不证明真实性；无摘要时仍为仅引用绑定。

## [0.7.0] - 2026-10-13

### Added

- PI-Desktop Skill/Extension 插件构建入口、零依赖 Python JSON 桥接及独立同模型审核；可生成目录和 `.piplug`，复用既有审核裁决与两轮策略。
- 主动启用的会话工具门控：三个阶段预检、用户确认、完整参数绑定和单次批准；撤销世代阻止迟到确认恢复批准。
- 以 0.18.0 为最低接口基线的能力诊断与后续协议兼容测试；Skill 展示字段投影兼容桌面解析器，规则保持单一来源。
- 桌面安装/使用/宿主限制说明、Python 与 Node 行为回归、发行包外构建验证。

### Notes

- 0.18.0 实际扩展加载器及安装/LOW 模型审核已验证；0.19.1 已验证原生安装、LOW 审核、确认/单次工具批准、Stop 与卸载后的回合刷新清理。未知未来版本不作为已验证成果。
- 真实桌面测试修复 ToolSearch 与 Read/Glob/Grep 大小写白名单遗漏，样例使用实际核心 Write；同版本重装需完整重启以避免依赖缓存。
- 宿主限制：审核工具 110 秒预算、completion 90 秒预算与限流；无公开可见子会话派发/远程取消 API，Hook 异常可能被宿主忽略。

## [0.6.0] - 2026-09-22

本版本是一次工程质量与分发能力的优化批次（无破坏性 API 变更），由对发布候选的系统盘点驱动。

### Added

- **wheel 现携带可安装 Skill**：`goalanchor/_skill/`（SKILL.md 与 references/agents）作为 package data 打入发行包，`pip install goalanchor` 的用户在仓库外也能运行 `python -m goalanchor install`。新增 `scripts/sync_skill_bundle.py` 以 `skills/goalanchor` 为单一来源同步包内副本，CI 用 `--check` 检测漂移。
- **PEP 561 `py.typed` 标记**：下游类型检查器可读取库的内联类型。
- **Gitee Go 流水线**：`.workflow/gitee-go.yml`（仓库真实远端），与 GitHub Actions 对齐。
- **质量门禁**：CI 新增 ruff（强制）与 mypy（可见基线）步骤及包副本同步检查。

### Changed

- **`platforms` 模块重构为子包**（1070 行单文件 → `platform_common` / `platform_prompt` / `platform_parse` / `platform_config`)，职责分离；`goalanchor.platforms` 门面重导出全部原公共符号，导入路径向后兼容。
- **dispatch 不再重复解析 argv**：argv 模板在配置加载期解析一次并缓存（`_prepared_argv`)，消除「配置校验」与「派发」两层 `_apply_model` 口径漂移的根源。
- README（中英文双版本）：优化表述与层次；仓库结构处明确 **`docs/`（仓库内、随包版本化）与 `Doc/`（仓库旁、设计/记录中心）** 的分工。
- `validate-multi-round.ps1` 增加依赖自检：隔离目录缺失或 pytest/pyyaml 残缺时，先报出可执行的重建指令，而非在后续 pytest 处模糊失败。
- `_enum` 辅助函数泛型化（TypeVar），返回具体枚举类型，提升类型精度。

### Tests

- ruff 自动整理全项目导入排序与未用符号；修复 1 处真实问题（`evaluate_fixtures.py` 使用未导入的 `Sequence`)。
- `tests/test_platforms.py` 增加测试类索引注释（导航改进；物理拆分为多文件属高 churn 的可选项，未做）。
- 全量 **206 passed**;`validate-multi-round.ps1` 基线 206;压测默认强度 **739 项校验零失败**。

### Notes

- mypy 以非阻断基线运行：代码早于类型化，存在一批已知的动态属性/边界 object 类型精度项（非运行时缺陷），收敛后改为强制门禁。
- CI YAML（GitHub Actions / Gitee Go）语法需在对应平台首次启用时核验，本地不可执行。

## [0.5.1] - 2026-09-22

### Fixed

- `MODEL_NOT_PINNED` 告警措辞修正(stderr 与 `warnings` 双通道):原 message 在「platform/profile 级已钉定、仅顶层主模型未声明」的合法配置下断言「未声明任何子 Agent 模型、可能回落平台默认模型」——与事实相反(子 Agent 确被钉定,但一致性保证因主模型缺失而不可执行)。现改为如实说明:主模型未声明 → 一致性不可强制,子 Agent 可能运行于 platform/profile 级声明模型或平台默认值。触发时机不变(主模型未声明即告警,该情形需要信号)。来源:GA-V050-AUDIT 独立审核 AUD1-01/A2-F1(P2)。
- `argv` 模型 flag 防护:带**自有取值**的模型 flag 一律拒绝——裸写(`-m preset`)与附着值(`--model=opus`);不误拒前缀相近的无关选项(`--model-config=x`)。`{model}` 占位符仅在**模型取值槽**被填充(独立元素、`-m {model}`、`--model={model}`、`--model{model}`),出现在其他参数文本中的 `{model}` 按字面传递;紧贴短选项(`-mopus`)刻意不做检测(无法与同前缀无关选项区分,见第三轮条目②)。来源:AUD1-02 + Meta META-04 + 三轮核验收敛。
- argv 模板校验前移至**配置加载阶段**(`PlatformDispatchConfig` 构造期),覆盖 `review-cycle`、`--dry-run` 与 `platforms --config` 三条路径:硬编码模型 flag 与「`{model}` 占位符但无模型可填」现在都在启动任何 Agent 之前失败;保留条件语义(无可注入模型时 argv 钉定配置依旧合法)。来源:A2-F4 + Meta META-01/03。
- 平台配置内的桥接 profile 声明 `model` 现在直接拒绝(此前仅 legacy 桥接配置报错,平台形态配置会静默丢弃该声明并在审计中记为 null)。来源:AUD1-03。
- `platforms --config <cfg> --probe` 现在按 dispatch 同一优先级解析出的模型探测(profile 级 > 平台级 > 顶层,经该平台首个 enabled profile 解析),且配置在任何探测之前加载校验——无效配置快速失败、不再消耗探测调用。此前新增的 `probe_platform(model=...)` 形参无生产调用点,探测始终走未钉定链路,与文档宣称不符。来源:AUD1-04/A2-F3 + Meta META-03 + 四轮核验收敛。
- `[0.5.0]` 段 Tests 数字自相矛盾修正(709/195 与 739/196 并存),改为显式标注阶段演进。来源:AUD1-05/A2-F2。
- 第二轮复核加固(GA-V051-VERIFY):①配置加载期 argv 预检跳过 `enabled=false` 的 profile——它们永不派发(`PlatformDispatchConfig.binding()` 拒绝、`AgentCatalog.select` 跳过),其 argv 不应使整份配置失败;②`{model}` 占位符与带自有取值的硬编码 flag 并存同样拒绝(替换值会与第二个 flag 并存且多数 CLI 后者生效);③`platforms --config --probe` 的模型解析补齐 profile 级钉定;④`declared_main_model` docstring 同步为真实语义(残留的「未钉定/可能回落默认模型」断言清除)。
- 第三轮核验加固(GA-V051-VERIFY2):①恢复 `-m {model}` / `--model={model}` 合法模板(flag 的取值槽恰为占位符时视为占位符位点并原地填充;上一轮加固曾误拒该写法——占位符与自有取值必须区分);②紧贴短选项(`-mopus`)检测整体移除:无法与同前缀无关选项(`-max-tokens`)区分,保留检测会误拒合法配置,该拼写改为文档明确不覆盖(不要紧贴取值);③分歧校验同样跳过 disabled profile——与 argv 预检口径统一(「不可达 profile 不阻断配置」),同时使「禁用分歧 profile」成为可行修复路径;④probe 模型解析改为与 dispatch 完全同优先级(profile 级 > 平台级 > 顶层,经该平台首个 enabled profile 解析);⑤注释与发布说明修正:`select_agents` 实为 `AgentCatalog.select`;预检口径写明「所有 enabled 平台 profile 均须可派发,未使用者请置 enabled=false」。

### Tests

- 新增/迁移:附着 flag 拼写拒绝(+前缀相近选项不误拒,含自定义平台 `-max-tokens` 合法路径)、`-m {model}`/`--model={model}`/`--model{model}` 合法模板接受、参数文本中的 `{model}` 按字面传递且不跳过注入、`{model}` 与自有取值 flag 并存拒绝、平台形态桥接 profile 声明 model 拒绝、disabled profile 不参与预检且其分歧模型不阻断、platform 级钉定告警措辞真实性(含 argv 注入证据)、`platforms --config --probe` 模型传递(profile/平台/顶层三层优先级)、无效配置先失败零探测;两条既有测试的失败点从派发期迁移到配置加载期(**206 passed**,同步 `validate-multi-round.ps1` 基线 196→201→203→205→206)。

## [0.5.0] - 2026-09-22

### Added

- 机器可读告警通道:`review-cycle`(含别名 `review-run`)与 `review-cycle --dry-run` 的 JSON 输出新增顶层 `warnings` 数组(恒为数组,可为空,形状确定)。`MODEL_NOT_PINNED` 标记未钉定主模型的平台调度(stderr 人类可读告警保留),`MODEL_DIVERGENCE_APPROVED` 标记配置携带用户确认的 `model_divergence`(条目回显 `confirmed_by`);stdio bridge 按设计边界不产生告警。只读 stdout 的自动化流水线由此获得固定的审计锚点,不再依赖 stderr 文本或 usage_summary 内层字段。

### Fixed

- 多平台调度模型一致性(紧急修复):部分平台在子 Agent 未指定模型时回落到自身默认模型,导致与主 Agent 模型不一致、审核失败。现在子 Agent 强制与主 Agent 同模型:配置顶层 `"model"` 或 `review-cycle --model <主模型>`(覆盖配置,每次运行重传当前值即实现主模型中途切换后的同步)声明主模型,内置平台按各自 flag 注入(`codex -m`、`claude --model`、`gemini -m`、`opencode -m`),自定义平台支持 `argv` 内 `{model}` 占位符或默认 `-m` 插入(`platforms.<name>.model_flag` 可覆盖);生效优先级 profile `model` > `platforms.<name>.model` > 顶层 `"model"`。
- 未经用户确认的模型分歧在配置加载阶段即拒绝(`review-cycle` 与 `--dry-run` 均预检),不会启动任何 Agent;确需不同模型必须先暂停询问用户,并将决定记录为 `"model_divergence": {"confirmed_by": "...", "reason": "..."}`,确认信息回显进运行输出供审计。
- 模型审计:每条 `agent_bindings` 记录实际 `model`(stdio bridge 恒为 null),`usage_summary` 新增 `models_used`;`platforms` 报告输出 `model_flag`/`main_model`/`model_divergence`;`probe_platform` 支持 `model` 参数以按钉定模型探测。SKILL.md 派发序列新增"Pin the model"步骤(逐轮复检主模型、切换即同步、分歧先询问),`agent-runtime.md` 与示例文档同步补充模型一致性章节。
- 关闭三条静默失效路径(审查发现):①`argv` 硬编码模型 flag 时注入会产生重复 flag、末尾者(硬编码值)生效而静默推翻钉定——现在直接拒绝并要求改用 `{model}` 占位符;②stdio bridge 配置上声明的 `model` 被静默忽略——桥接协议不携带模型字段,现在直接报错;③未声明任何模型的平台调度无任何提示——现在向 stderr 打印告警,且 `usage_summary` 新增 `main_model` 字段(未声明为 null)供审计。

### Tests

- 新增多轮压测:独立脚本 `scripts/stress_model_sync.py`(全离线,6 场景可调强度:争议轮全角色重拉、主模型切换同步、并发派发、失败重试逐 attempt 校验、分歧拦截 fuzz、整进程 CLI 端到端);warnings 断言加入前为 709 项校验/195 passed,加入后默认强度一轮 **739 项校验零失败/196 passed**;`tests/test_stress_model_sync.py` 以小强度子集进 CI。
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

[未发布]: https://github.com/chenyuyu6666/GoalAnchor/compare/v0.7.0...master
[0.7.0]: https://github.com/chenyuyu6666/GoalAnchor/tree/v0.7.0
[0.6.0]: https://github.com/chenyuyu6666/GoalAnchor/tree/v0.6.0
[0.5.1]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.5.1
[0.5.0]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.5.0
[0.4.1]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.4.1
[0.4.0]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.4.0
[0.3.4]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.3.4
[0.3.3]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.3.3
[0.3.2]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.3.2
[0.3.1]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.3.1
[0.3.0]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.3.0
[0.2.0]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.2.0
[0.1.0]: https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.1.0
