# v0.9.2

日期：2026-10-24  
基线：v0.9.1 及 PR #16 合并提交  
版本类型：PATCH（兼容性与 fail-closed 修复）

## 更新内容

- legacy `ONE_TIME` 授权在没有显式消费 ledger 时返回 `LEGACY_UNVERIFIED`，不再把无状态资格检查当作一次性执行许可。
- `OneTimeAuthorizationLedger` 提供进程内原子消费，重复消费和并发竞争均 fail-closed；跨进程可信消费继续由 Strict Workflow 持久化账本承担。
- 保持 `AUTONOMY_GRANT`、自动动作、旧授权序列化格式和 legacy Workflow 的持久化 reserve/consume 行为兼容。
- 收紧效率治理和 review delivery binding 的非法输入校验，避免未知或不完整数据被当作可用证据。

## 边界

- 本版本不宣称 Codex、Claude、DSH、ZCode 或 PI-Desktop 的 Strict 原生闭环完成。
- 进程内 ledger 不构成跨进程或宿主可信边界。
- T10–T14 的真实 runtime dispatch、成本收益和原生宿主验收仍按验收矩阵保持 `PARTIAL/BOUNDED`。

## 验证

发布前以最终 tag 对应的 CI、全量 pytest、Ruff、Mypy、Skill sync、PI-Desktop Node 测试、wheel/sdist 构建和包外导入结果为准。
