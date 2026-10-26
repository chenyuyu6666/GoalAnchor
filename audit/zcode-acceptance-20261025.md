# ZCode 本机验收记录 — 2026-10-25

本轮真实测试已完成：桌面 GLM-5.3-Flash 短句响应、四个独立新任务的审核投递，以及 App Server 的一次固定提示请求均取得可观察结果。完整 GoalAnchor v1.0 仍为 **PARTIAL/BOUNDED**，不创建新版本或发布。

## 真实宿主结果

| 验证项 | 结果 | 可复核证据 |
|---|---|---|
| 本机安装 | Desktop 3.16.3；内置 CLI 0.17.0；Skill 已发现且哈希一致 | zcode-local-probe-v1.0.json |
| 桌面短句 | 准确回复 ZCODE_GOALANCHOR_UI_SMOKE_OK；界面显示约 5 秒 | zcode-desktop-review-20261025.json |
| Reviewer-1 / Reviewer-2 | 两个独立新任务收到逐字相同的 prompt 和同一材料摘要；均返回 FAIL 并引用 C-4 缺证据 | 同上，含可见输出全文、提示、不同 workspace 标签 |
| Arbiter / Meta | 两个独立新任务收到原始材料和前序输出；均保留未处置 finding、拒绝候选验收结论 | 同上 |
| App Server | Coding Plan ark-code-latest / low，单次 send、相关 ACK 和 turn.completed/resultType=success；固定响应匹配 | zcode-protocol-plan-state-20261025.json 与同名 events.jsonl |
| 工具和清理 | 本次 App Server toolCallCount=0，session/close 回执 closed=true | 同上 |
| 度量 | 宿主 terminal.tokenCount=3140；客户端总耗时 17.406 秒；终态 duration=11398（宿主字段） | 同上；没有填补未知输入/输出 token 拆分 |
| 最终复测 | 新建独立协议会话，完整 input/session/turn 和 v4 订阅关联，success、tools=0、closed=true；tokenCount=3185，总耗时 13.937 秒 | zcode-protocol-final-20261025.json 与同名 events.jsonl |

四角色的验收题目是合成的缺证据案例，模型答复来自真实 ZCode。案例的正确表现是 FAIL：它与整个软件项目的验收状态不是同一个结论。Reviewer 是顺序投递到独立任务，未验证原生并行调度。
Skill 文件发现和同步已经单独验证；这四个无工具模型回合是否实际加载 Skill 全文未知，不据此宣称 Skill 自动触发完成。

## 修复及更正

- PATH 没有命令不等于桌面软件没安装；同时修正 DSH 安装记录。
- 旧探测器把“写出请求”记为 accepted，并读错了事件字段。旧记录明确更正为 UNKNOWN，不用缺失日志推断模型不可用。
- 0.17.0 的事件 envelope 是 session/event.params，终态还必须检查 resultType，不能只看事件名或 accepted。
- 会话创建会丢失 model.options 中的推理档位；同时传顶层 thoughtLevel 并读回核验。
- 计划状态独立于权限模式：通过相同 conversation topic/subscription 的 v4 完整 snapshot 核验 planEnabled=true；不能把 mode=build 简单判为计划状态失效。
- one-shot 实验 bridge 严格拒绝错误 envelope、多段/重复键 JSON、缺身份绑定和 Strict 请求；不再靠字段过滤或模型自报制造可信证明。
- 超时保留 UNKNOWN，不自动重发；没有注入账户授权或修改凭据。前几次探测可能设置了本轮隔离测试目录的模式偏好，最终成功运行没有使用该模式写入操作。

## 回归证据

- 首轮全量 pytest：1009 passed，84.10 秒；日志 P2 修复后的最终全量：1013 passed，77.15 秒，0 failures/errors/skipped。记录：zcode-slice-final-regression-20261025.xml。
- Ruff：python -m ruff check goalanchor tests scripts 通过。
- Mypy：goalanchor 和两个 ZCode 脚本共 41 个文件通过。
- Skill sync 与 git diff --check 通过。
- 双 Reviewer 有界复核、主裁决和独立 Meta 复核通过；两条日志 P2 已修，无未解决高优先级异议。记录：zcode-slice-review-20261025.json。
- 范围图谱：zcode-graphify-20261025/ 为本切片早期源码导航快照，后续修改需对照源哈希；不是全项目最终 Graphify。

## 边界与下一开发切片

本轮没有把 UI 可见输出作为 Strict 签名事件入账，没有真实消费授权，也没有执行副作用并完成 Strict closure。UI 的 draft/workspace 标签不声称是 runtime session ID；服务端模型、账户、principal 与签名 provenance 仍需可信 provider 绑定。已有 core fixture 与 native 观察分开计数。

下一切片：把已验证的协议终态和独立 session 信息接到 trusted review proof/delivery provider；逐项验证 Strict question、answer 来源、精确批准、一次性消费、撤销、取消、UNKNOWN/reconcile 和 closure。随后继续五宿主矩阵、真实效率基线与发布门禁。完整计划未完成前保持不发布。
