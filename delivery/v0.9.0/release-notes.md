# GoalAnchor v0.9.0 严格工作流基线

日期：2026-10-20  
基线：v0.8.0（发布提交 `1af37a3`）  
版本类型：MINOR（向后兼容的新能力）

## 更新内容

- 新增 `StrictWorkflow` 增量 API：目标基线、证据快照、Strict 审核上下文和受限操作注册。旧 `Workflow` 的对象、JSON 和历史空证据行为保持兼容。
- 严格运行时拆分为稳定公共门面、`workflow_legacy` 和 `workflow_strict`；SQLite 存储拆为 core/strict，公共 `workflow.py` 保持稳定导入路径。
- SQLite schema v2：不可变 Strict proof/event 历史与 CAS 保护的当前 head。打开 v1 数据库只新增结构，不把 legacy 记录升级为可信来源。
- `ReviewContext.strict_binding` 与可信 event/runtime binding provider 类型，用于绑定 policy、host、principal、account、session 和 generation 摘要。
- 仓库文档与交付物收敛：`docs/` 只保留开发规范、项目帮助和 CI 任务定义；v0.8.0 交付物纳入 `delivery/v0.8.0`。

## 兼容性

- 无破坏性 API 变更；旧 `Workflow` 路径继续可用。
- 旧审核、旧 grant、legacy `CLOSED` 和 legacy `VerificationResult` 不会变成 Strict 证明。
- schema v1 数据库可以只读检查；迁移到 v2 只增加结构，不回填可信历史。

## 验证

- Ruff、Mypy（37 个源文件）、Skill bundle 同步检查通过。
- strict/legacy 重点测试 284 项通过。
- 排除 `test_idempotent_recovery.py` 和 `test_contract_remaining.py` 后，全量 807 项通过（exit 0）；这两个文件中的 SQLite 跨进程用例在 Windows 高负载下仍可能失败或长时间等待，单独复跑通过。
- 核心场景映射 58/58 `PASS`；58 项原生宿主状态均为 `NOT_RUN`。

## 边界

- Strict 审核、问答、授权、执行、reconcile 与关闭仍返回 `UNAVAILABLE`；P2b–P7 尚未完成。
- 不能把本版本当作完整严格闭环或全部宿主验收。
- fixture、受控回调和 TextHostAdapter 共享文本测试不证明原生 Hook、全工具强制或人类 UI 签署。
- Codex 为 LIMITED；Claude Code / DSH / Zcode 仍为 ADVISORY / UNAVAILABLE。

## 安装

```powershell
python -m pip install goalanchor-0.9.0-py3-none-any.whl
```

或从源码包构建：

```powershell
python -m pip install goalanchor-0.9.0.tar.gz
```
