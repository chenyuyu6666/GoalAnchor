# v0.8.0 严格运行时使用与兼容说明

本文描述 GoalAnchor v0.8.0 正式版中的严格运行时。58 项核心场景映射均通过，58 项原生宿主状态均为 `NOT_RUN`；宿主 fixture 和受控回调证据按各自层级解释。既有 `ReviewContext`、审核周期和 Skill API 保留，属于辅助检查路径；只有接入 `Workflow`、可信事件、持久账本和受控 executor 的执行才具备这里描述的门控。

## 信任边界与接入顺序

`workflow_protocol` 定义内容绑定值、严格 JSON 和 HMAC 签名事件；`workflow_store` 保存 SQLite 记录、事件与执行回执；`questions` 定义问题及其状态；`workflow` 串联基线、证据、审核、授权、执行、观察和关闭。

可信控制侧必须保管 `EventAuthority` 密钥、数据库、observer 和 executor。不要把签名对象、密钥、可写账本或通用签名接口暴露给模型。签名证明配置的可信控制侧发出事件，不能单独证明自然人亲自点击批准。模型文字“用户已同意”没有授权效力；CLI 不提供模型可调用的批准签名命令。持久化数据库不等于跨进程自动恢复密钥，部署方须自行安全保存并重载同一 authority。

接入顺序如下：

1. 由控制侧签署目标基线，固定验收 ID、预期值、对象和必要操作。
2. 由可信 observer 采集证据快照，提交绑定当前上下文的审核包及逐项异议处置。`review_packets` 为各角色冻结投递包，审核回执必须匹配 session、packet digest 和 `DELIVERED` 状态。synthetic receipts 明确标记为 synthetic，只证明 fixture 投递契约，不证明模型实际阅读。摘要绑定内容，不替代证据真实性核查。
3. 注册精确操作，绑定基线、工具、完整参数、工作目录与可选资源摘要前置条件；通过可信问题答复取得一次性授权，或显式签署受限自治 scope。
4. 执行前重核当前基线、审核、策略、依赖问题、期限及资源条件。事务预留并消费授权后，可信 executor 才能派发。
5. 保存实际执行回执，再由 observer 提交与成功回执、验收对象及当前基线关联的观察；满足全部关闭条件后才可 `CLOSED`。退出码为零本身不足以关闭目标。

scope 仅允许当前策略中的工具和 workspace，并受基线、审核和到期时间约束。运行时检查解析后的 cwd 和声明的资源前置条件路径。**cwd 不是沙箱**：任意 shell 参数、程序行为、未声明路径、网络和子进程副作用仍须由可信 executor 与宿主隔离机制约束。不要把 scope 描述成对任意 shell 命令的完整路径隔离。

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
