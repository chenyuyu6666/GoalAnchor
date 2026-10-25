# Provider-backed Strict 审核证明契约

本接口由可信 composition root 配置；模型不能选择或修改 verifier、签名 authority、当前 principal/account 或运行时 generation。验证通过证明的是配置的信任边界，而非自然人身份。

## 输入与证明

StrictWorkflow 的 trusted_review_verifier.verify 签名不变。返回值必须满足 goalanchor.review-proof/v1，空字典不再表示通过。

证明独立于 bundle，避免 review_digest 的循环定义。字段包括：

- schema_version、review_digest、context_fingerprint、policy_digest、host_binding、runtime_binding_digest；
- round_number、skill_digest、issued_at、expires_at、roles、proof_digest；
- 每角色的 agent/session/model、input/turn/source event 标识、完整 packet 和原始 JSON response；
- packet/result/source event 摘要、完整 host.review.completed 签名来源。

所有角色的模型必须精确等于 host binding 的主模型，独立 session/input/turn/source 标识不得跨角色或跨轮次复用。Reviewer 的 packet 由现有 build_packets 生成，同轮材料相同。原始 response 必须与该角色完整结构化报告一致，不能忽略错误字段后拼成 PASS。

normalize_review_proof 只负责结构和绑定检查，不持有验签密钥，也不能代替真实来源认证、证明 Skill 已实际加载或证明账户身份。自定义 provider 必须先完成这些可信来源验证；来源不足时返回 None 或 TrustedProofUnavailable/TrustedProofRejected，不能用已有 audit JSON 自签提升信任。

## 持久化与重放

带 provider 的提交会生成 observer authority 签名的 controller.review.attested companion event，绑定原 observer.review 事件、完整 proof 和 record/chain/head 摘要。同一 SQLite 事务保存原事件、companion、投影及专用 strict_review_attestations 行。

新表只增加 v2 结构，不迁移或补签旧记录；UPDATE/DELETE/REPLACE 由 SQLite trigger 拒绝。缺表、缺保护或缺 companion/attestation 均拒绝。当前 proof 的日期与声明保持不可变，重新调用 provider 不得延长已存期限。

幂等返回和竞争恢复也要重新验证事件签名、当前上下文、历史、proof 和期限。启动 provider 后不能自动使用旧离线审核；移除 provider 也不能把已保存的 provider-backed 审核降级为离线通过。

## 权限消费

授权申请、批准、scope、风险处置、execute、resume 和 close 读取统一的已验证历史。只有授权和派发要求 PASSED；风险处置允许真实的待复审 finding。deny、revoke、cancel 等减少权限操作仍可使用。即使没有在途 receipt，cancel 也会写入事件历史，并阻止之后的授权、消费和派发。

新授权问题绑定 proof digest，换轮后需要新问题；grant 的固定声明与审核证明另有 controller 签名绑定，修改投影中的 proof/期限不能改写授权。消费事件与 revoke/cancel 事件在 SQLite append-only 历史中检查，修改 grant 状态不能恢复已消费或撤销的权限。最终派发以已验证快照执行 CAS，并使用 review、Baseline、Evidence、grant 和 parent scope 中最早的期限及运行身份 lease。恢复记录属于执行记账，不提升 goal generation，不使自己的审核上下文立即失效。

## 验收范围

新增证明格式、不可变持久化和消费门控使用离线受控 authority/provider fixture 验证，包含跨进程竞争与提交前崩溃。它们不能计作 ZCode 原生账号、签名 receipt provider 或 Strict 真实授权执行闭环已经完成。

既有无 review provider 的受控离线模式和 legacy Workflow 的 API/JSON 保持；它们不获得新的 native proof 身份。当前真实 ZCode 证据仍是已验证的传输/UI 测试，可信 receipt provider 的宿主接入与账户来源还需下一阶段实现和验收。
