# v0.8/v0.9 严格运行时使用与兼容说明

本文描述 GoalAnchor v0.8.0 引入、v0.9.1 扩展的严格运行时。核心离线链已覆盖 Baseline、Evidence、Review、授权、一次性 grant、执行、UNKNOWN/reconcile、观察和关闭，并由回归测试验证；原生宿主仍按具体版本和模式记录 `NOT_RUN`、`LIMITED`、`ADVISORY` 或 `UNAVAILABLE`。宿主 fixture 和受控回调证据按各自层级解释。既有 `ReviewContext`、审核周期和 Skill API 保留，属于辅助检查路径；接入 `StrictWorkflow`、可信事件、持久账本和受控 executor 后才具备这里描述的门控。

## 信任边界与接入顺序

`workflow_protocol` 定义内容绑定值、严格 JSON 和 HMAC 签名事件；`workflow_store` 保存 SQLite 记录、事件与执行回执；`questions` 定义问题及其状态；`workflow` 串联基线、证据、审核、授权、执行、观察和关闭。

可信控制侧必须保管 `EventAuthority` 密钥、数据库、observer 和 executor。不要把签名对象、密钥、可写账本或通用签名接口暴露给模型。签名证明配置的可信控制侧发出事件，不能单独证明自然人亲自点击批准。模型文字“用户已同意”没有授权效力；CLI 不提供模型可调用的批准签名命令。持久化数据库不等于跨进程自动恢复密钥，部署方须自行安全保存并重载同一 authority。

接入顺序如下：

1. 由控制侧签署目标基线，固定验收 ID、预期值、对象和必要操作。
2. 由可信 observer 采集证据快照，提交绑定当前上下文的审核包及逐项异议处置。`review_packets` 为各角色冻结投递包，审核回执必须匹配 session、packet digest 和 `DELIVERED` 状态。synthetic receipts 明确标记为 synthetic，只证明 fixture 投递契约，不证明模型实际阅读。摘要绑定内容，不替代证据真实性核查。
3. 注册精确操作，绑定基线、工具、完整参数、工作目录与可选资源摘要前置条件；通过可信问题答复取得一次性授权，或显式签署受限自治 scope。
4. 执行前重核当前基线、审核、策略、依赖问题、期限及资源条件。事务预留并消费授权后，可信 executor 才能派发。
5. 保存实际执行回执，再由 observer 提交与成功回执、验收对象及当前基线关联的观察；满足全部关闭条件后才可 `CLOSED`。退出码为零本身不足以关闭目标。

scope 仅允许当前策略中的工具和 workspace，并受基线、审核和到期时间约束。Strict 操作注册依赖可信 composition root 提供的逐工具路径参数 schema；未配置 schema 或出现未声明的路径字段时 fail closed。它会解析 cwd、schema 指定的参数路径和资源前置条件；资源摘要会在取得写锁后及提交前重新核验。注册只建立绑定当前基线、完整操作摘要和路径 schema 摘要的 Strict 记录，不创建授权，也不执行操作。**cwd 不是沙箱**：任意 shell 参数、程序行为、未声明路径、网络和子进程副作用仍须由可信 executor 与宿主隔离机制约束。不要把 scope 描述成对任意 shell 命令的完整路径隔离。

## 一次性执行与恢复

相同 attempt ID 重复调用返回已有回执，不重新派发；授权在外部执行前消费。异常、断线或缺少关联完成事件会留下 `UNKNOWN`，不能解释成“未执行”。恢复把已消费但未完成的尝试标为 `UNKNOWN`，不自动重发。应先独立检查外部状态，再用绑定操作与新证据的可信 reconciliation 明确结果；不要通过换 attempt ID 绕过不确定结果。

`resume_transport` 是显式例外路径：需要一次新的签名用户许可，冻结原操作、原授权、原 backend 和幂等 key，并绑定有效的独立签名幂等测试证据。两段 CAS 分别保护恢复预留和消费，最多授予一次恢复 transport；重复请求返回记录。transport 被确认也不会改变原 `UNKNOWN`，仍须另行 `reconcile`。它不是自动重试，也不赋予通用 executor 幂等性。

`reuse_observation` 允许受证据约束的无影响观察复用：原 criterion 必须完全相同，当前独立签名对象摘要须匹配，且原观察 fingerprint 与签名来源链须可验证。复用不会复活旧授权，也不替代当前基线审核或必要操作验收。`update_presentation` 的无影响更新仅限展示 title，不能借此修改语义基线。

缺少可信来源 proof 的旧授权明确拒绝为 `LEGACY_UNVERIFIED`；当前 schema 版本检查与这项拒绝规则均不构成通用旧记录迁移工具。

## 可复制的 CLI

从已安装环境或仓库根目录运行；demo 目录必须尚不存在：

```powershell
python -m goalanchor workflow capabilities
python -m goalanchor workflow demo --directory .\workflow-demo-v08
python -m goalanchor workflow recover --database .\workflow-demo-v08\state.db
```

`demo` 使用本地 synthetic reviewer、arbiter、meta reviewer 和测试批准，写入隔离目录并回读验收；输出 `review_mode=synthetic-demo`、`host_execution_verified=false`。它证明本地集成路径，不能证明独立模型审核或宿主原生强制执行。

`status` / `audit` 是零写入检查，**只接受离线、已 checkpoint 并显式切换为 DELETE journal 的数据库**。运行时通常使用 WAL；不要直接对活跃 WAL 库运行这两个命令。停止所有写入者后，可在 PowerShell 中准备离线库：

```powershell
python -c "import sqlite3; c=sqlite3.connect(r'.\workflow-demo-v08\state.db'); print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()); print(c.execute('PRAGMA journal_mode=DELETE').fetchone()); c.close()"
python -m goalanchor workflow status --database .\workflow-demo-v08\state.db
python -m goalanchor workflow audit --database .\workflow-demo-v08\state.db
```

`recover` 是显式写入操作，可以打开 WAL 数据库，可能创建或更改 SQLite sidecar；输出 `dispatched=false`。`status` 返回目标和回执，`audit` 返回事件，不会启动模型，也不是密码学完整性审计命令。正常输出使用 `goalanchor.workflow.v1` envelope；错误写 stderr 并返回非零退出码。`capabilities` 报告 CLI 层能力，不是本机宿主探测结果。

## Strict baseline 与证据来源

`StrictWorkflow` 是与旧 `Workflow` 并列的增量 API。旧 `Workflow` 的对象、JSON、历史空证据行为保持兼容；旧记录不会因此变成 Strict 证明。当前严格路径支持创建目标基线、添加证据快照、构造审核上下文、审核、授权、一次性 grant、受限执行、UNKNOWN/reconcile、观察和关闭。不能把旧审核、旧 grant、legacy `CLOSED` 或 legacy `VerificationResult` 当作严格授权或完成证明。

构造 Strict 路径时，控制侧必须注入 `trusted_runtime_binding_provider` 和 `trusted_event_verifier`。前者由可信 composition root / 宿主 Adapter 提供当前规范化的 host facts、principal、account、session 和递增 generation，并提供 `acquire_lease(expected_digest)` 上下文管理器。租约持有期间，这些绑定事实不得轮换。租约能力缺失、不可用或与预期摘要不符时，操作分别 fail closed 为 `UNAVAILABLE` 或 `STALE`。模型或 Agent 不得提供这些事实，也不得自行声明用户身份。

Strict 基线证明固定一组 `WorkflowPolicy`、host、principal、account、session 与 generation 摘要；每项证据必须绑定相同摘要。用户账号、会话、模型、mode 或 Hook 事实变化后，旧基线和证据不能生成新上下文；须重新确认基线并重新采集证据。证据签名方 `SERVICE` 的身份单独保存在来源证明中。Adapter 对 Hook 的声明仍只是可信控制侧声明；它不能证明原生 Hook 实际工作或覆盖全部工具，原生平台结论必须由 P5 的逐宿主实测提供。

schema v2 在 SQLite 中保存不可变的 Strict proof/event 历史和 CAS 保护的当前 head。首次创建 v2 或将 v1 数据库迁到 v2 时只新增结构，不把 legacy baseline、evidence、review、授权或关闭记录迁移成可信来源。迁移前先在内存数据库验证 SQLite 外键与 deferred-FK 能力，并在目标数据库写锁内重读 schema version。`status` / `audit` 对离线 DELETE-journal 的 v1、v2 数据库都只读；活跃 WAL 库仍拒绝零写检查。

Strict 审核上下文从单个只读 SQLite 快照读取当前基线、proof head、来源事件和投影，并在外部 proof 核验后再次读取以检测并发变化。只有 generic legacy baseline 时返回 `LEGACY_UNVERIFIED`；legacy-only evidence 不会进入上下文。Strict 上下文可能包含零项证据，但它只表示“当前没有严格证据”，不表示治理通过、执行成功或目标完成。provider 后续核验可以刷新 nonce、provider digest 或临时 expiry，但这不会修改、续期或复活已保存的 proof；完全相同的源事件只有在持久 proof 仍有效、来源身份/目的/subject/binding 一致且它仍是当前 head 时才可幂等复用。

## 宿主绑定与兼容证据层级

`host_binding` 冻结可信 embedding 提供的 host、user agent/版本、model、mode、hook enabled 与 hook trust 事实，并纳入策略指纹；更改这些事实会使旧上下文与授权失效。它是控制侧声明的绑定，不能自证原生 Hook 已安装、可信或覆盖全部工具。

`TextHostAdapter` 在 Codex、Claude、DSH、Zcode、PI-Desktop 上共享可信文本 fixture 路径，验证签名问题/答复及协议契约；这不是五个原生宿主 UI 或原生 Hook 的实测。

| 路径 | 当前边界 |
| --- | --- |
| Codex App Server 0.162.1 | `LIMITED`：实际模型命令批准 callback 可接入精确操作；未覆盖全部工具的强制执行 |
| Claude / DSH / Zcode | `ADVISORY`；缺少可验证的原生执行强制能力时返回 `UNAVAILABLE` |
| PI-Desktop | host contract 标为 `ADVISORY`；既有插件能力需按独立插件指南与版本证据判断 |

Codex 0.162.1 已实测真实模型请求、结构化问答 fixture、拒绝与取消；当前证据另含绑定 host 的批准路径与 signed-core-question JSON。批准闭环也已用真实 `gpt-6-astra` 命令 callback 验证：可信控制侧为精确固定 print callback 建立签名测试批准，账本一次消费，controller 回复 `accept`，宿主实际退出码 0，observer 验收后 `CLOSED`。该实验的 review provider 为 `synthetic-fixture`，approval provider 为 `explicit-test-fixture`；没有证明人类 UI 点击或真实独立多模型审核。早期不支持命令与 scope 的失败记录仍须保留，不能用后续成功抹除。

controller 校验 thread、turn、item、cwd 与完整 callback 参数，只回复 `accept`、`decline` 或 `cancel`，不使用 `acceptForSession`。`environmentId=local` 为当前支持边界；`proposedExecpolicyAmendment` 仅保留绑定，不应用策略扩权。文件修改批准及绕过 callback 的宿主工具未被该闭环覆盖。部署方必须控制哪些宿主工具可绕过 callback；安装 Skill、协议可传输或纯 decision helper 返回可执行候选均不等于运行时授权。

v0.8.0 的发布证据包括源码、自动化检查、持久账本/恢复验证及覆盖映射。58 项核心场景映射通过不代表通用宿主强制门控、真实人类 UI 或所有宿主的原生验收；Codex 回调使用测试授权，其他宿主能力仍以各自兼容状态为准。
