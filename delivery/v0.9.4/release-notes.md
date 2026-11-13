# GoalAnchor v0.9.4

验证证据版本。**无产品代码变更**：`goalanchor/`、`tests/`、`scripts/` 相对 0.9.3 零差异；本版本把 0.9.3 发布后完成的宿主验证证据与规范修订随仓库分发，并如实记录一个新发现的运行时缺陷。

## 验证结果（PI-Desktop 0.22.0 真实会话）

| 项 | 结果 |
|---|---|
| 插件升级 | 0.6.0 → 0.9.3（宿主 Plugins 页安装，manifest/runtime/registry 三处一致） |
| 能力报告 | `/goalanchor.capabilities` 返回实时报告（review available、host-completion） |
| 审核轮次 | **首个成功轮次**：LOW 单轮 1m 9s，结论 WARN（对未引用具体证据的候选是正确完成结论） |
| Guard 启用 | `/goalanchor-guard on` 执行成功，宿主确认「GoalAnchor Guard 已启用」 |

## 已知缺陷（未修复，如实记录）

**Guard 运行时拦截未生效**：Guard 启用后，Bash 探针（`echo GUARD_PROBE_OK`）在无批准的情况下正常执行（`tool.log` `tool.execution.completed`，session b0271a8e）。`guard.cjs` 的 `check()` 纯逻辑有测试覆盖，缺陷在宿主集成路径；根因候选：tool_call 钩子未被 0.22.0 调用、GuardState 被运行时重载重置、事件形状异常被静默 revoke。**该缺陷修复前，PI-Desktop 的 Guard 运行时拦截不能宣称可用。**

## 规范与证据修订

- 发布合规勘误：v0.9.3 的五项发布规范违规如实留痕（发布聚合提交正文四段、版本类型判定、构建产物入库方式、tag 指向、tag 内 manifest 状态）；规范文本同步修订消除 9.1 内部矛盾。
- `audit/README.md` 固化证据入库规则；未入库原始证据迁入工作区 `Doc/03_验证与测试/`。
- 完整功能验收矩阵基线刷新。

## 边界

- 原生宿主整体状态不变：Codex 深层闭环已验证；PI-Desktop 审核/Guard 启用已验证、执行闭环未完成；ZCode/Claude/DSH 未闭合；58 项原生场景按证据记录 `NOT_RUN`。
- 旧 `Workflow` 路径与序列化格式保持兼容；`ONE_TIME` 无 ledger 时返回 `LEGACY_UNVERIFIED`。
- Gitee 镜像 refs 已同步；Release 页面未创建（需 access token）。

## 安装

```sh
python -m pip install goalanchor-0.9.4-py3-none-any.whl
python -c "import goalanchor; print(goalanchor.__version__)"
```
