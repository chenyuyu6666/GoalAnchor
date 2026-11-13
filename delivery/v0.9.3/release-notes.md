# GoalAnchor v0.9.3

本版本是向后兼容发布，承接 PR #22（v1.0 切片）、#23/#24/#25 的验收证据与文档同步；**不宣称 v1.0 完成**。

## 更新

### 严格工作流

- 补全 `StrictWorkflow` 生命周期：scope grant/revoke、accepted-risk、resume、observation reuse、cancellation 与 closure。
- 审核证明链采用 append-only proof/head、原子 attestation、CAS 串行化与消费侧统一重验；证据删除/篡改/过期仍 fail-closed。
- 宿主中立公共接口齐备：宿主能力描述、`TrustedRuntimeBindingProvider`、`TrustedReviewProofProvider`、review delivery proof 与 `EfficiencyObservationProvider`。

### 效率治理

- `EfficiencyRuntime` fail-closed 准入门面；`TaskComplexityEngine`、`BudgetGovernor`、`ProgressWatchdog`、`PromptOptimizer`/context reuse 可独立使用。
- UNKNOWN 或非法计量一律 fail-closed，不把缺失数据当作零成本。

### 宿主与桥接

- Codex App Server：五场景（smoke/question/cancel/decline/accept）与真实四角色审核深层闭环，含审批回调、派发、回执、观察、closure 与 UNKNOWN/reconcile。
- ZCode：新增 app-server 传输桥接与协议探测器；桥接**拒绝** `strict_binding`，不把模型输出、环境标签或暴露的凭据当作身份证明。
- PI-Desktop：验证审核工具与 host-completion 路径（LOW/1 轮 PASS），原生 Strict 执行闭环仍 `NOT_RUN`。

### 修复

- Windows cp1252 编码崩溃：子进程、CLI 与宿主传输全面强制 UTF-8。
- 审核消费侧改用冻结上下文，闭合 reconcile→close 缺口。
- pytest 收紧至 `>=9.0.3,<10.0`（PYSEC-2026-1845）；项目范围 pip-audit 零漏洞。

## 安装

```sh
python -m pip install goalanchor-0.9.3-py3-none-any.whl
python -c "import goalanchor; print(goalanchor.__version__)"
```

## 边界

- 原生宿主仍为 `PARTIAL/BOUNDED`：Codex 深层闭环已验证；PI-Desktop 仅审核工具级；ZCode/Claude/DSH 原生 Strict 闭环未闭合。58 项原生场景按证据记录 `NOT_RUN`。
- 全项目 Graphify 语义重建、P5/P6 更大样本、P7 正式发布仍未完成。
- Gitee 镜像因远端分叉（Gitee 独有 2 个 README 提交、缺 28 个提交）**本次未同步**，延后处理。
