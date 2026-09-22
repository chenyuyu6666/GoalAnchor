# 多平台并发审核示例(Phase: 多平台 Agent 调度适配)

本示例演示 GoalAnchor 如何在执行层把 `reviewer` / `arbiter` / `meta_reviewer`
三个逻辑角色调度到**不同平台的独立子 Agent** 上并发审核。目录内配置默认指向
离线 `echo-agent.py`,不装任何平台 CLI 即可跑通全链路。

## 想在宿主界面里看到子 Agent 对话?

无头子进程永远不会有聊天界面。可见的路径是**让宿主用自己的子 Agent 机制拉起**,
GoalAnchor 只负责渲染提示词和校验结果:

```powershell
# 1. 渲染每个角色的提示词(把输出内容交给宿主原生子 Agent,Reviewer 并发拉起)
python -m goalanchor review-prompt --request request.json --role reviewer --agent-id reviewer-1 --round 1
python -m goalanchor review-prompt --request request.json --role reviewer --agent-id reviewer-2 --round 1
# 收集两个 JSON 回复后:
python -m goalanchor review-prompt --request request.json --role arbiter --agent-id arbiter --reviewer-results reviewer-results.json
python -m goalanchor review-prompt --request request.json --role meta_reviewer --agent-id meta-reviewer --arbiter-result arbiter-result.json

# 2. 用确定性闸门校验并聚合这一轮
python -m goalanchor gate --phase review-cycle --record cycle.json
```

宿主拉起的子 Agent 会话在宿主 UI 中可见(页签/任务面板);`review-cycle` 无头调度
只在宿主无法拉起子 Agent 时作为备用。

## 离线冒烟(无头)

```powershell
.\.venv\Scripts\python.exe -m goalanchor review-cycle `
  --request examples/multi-platform-review/request.json `
  --config examples/multi-platform-review/config.json
```

预期输出: `"outcome": "PASS"`,并带 `agent_bindings` 审计数组
(每个 agent_id 用了哪个 profile、哪个平台、第几轮)。退出码:`0` 收敛,
`1` 需要人决策或已 STOPPED,`2` 配置/运行错误。

## 接入真实平台

把 profile 的 `platform` 指向内置平台名即可(参考 `config.real.json`):

```json
{
  "profiles": {
    "Euclid": {"platform": "claude", "roles": ["reviewer"]},
    "Fermat": {"platform": "codex",  "roles": ["reviewer"]},
    "Gauss":  {"platform": "codex",  "roles": ["arbiter"]},
    "Noether": {"platform": "gemini", "roles": ["meta_reviewer"]}
  }
}
```

内置模板**全部走 stdin**(提示词经管道传入,不走 argv):`codex exec -`、
`claude -p`、`gemini`、`opencode run`。原因:

- Windows 下这些 CLI 是 `.cmd` shim,argv 传多行 prompt 会被 cmd.exe 转义破坏
  (实测 `claude -p "<多行文本>"` 会挂死直到超时);
- Windows argv 约 32K 上限,stdin 模式无此限制。

需要覆盖模板时才写 `platforms.<name>.argv`;若必须走 argv,保持 prompt 单行,
否则请用 stdin 模式。

已在真实平台验证:claude(Claude Code 2.1.272)与 codex(codex-cli 0.154.0)
作为 Reviewer 全链路通过;codex 的 stdout 含完整提示词回显与转写,解析器会取
**最后一个**带结果字段的 JSON(即最终答案),不受回显干扰。

已知使用注意:

- 平台 CLI 可能偶发失败(网络、限流、上游错误),GoalAnchor 会以退出码 2 + 平台
  stderr 详情显式报错;当前不做自动重试,重要审核建议直接重跑;
- 每次平台调用消耗该平台额度(实测 codex 一次审核 600~10000 tokens 视模型而定);
- `gemini` / `opencode` 未在本机验证(未安装),模板按其文档的非交互 stdin 行为配置。

规则:

- 内置平台名:`codex`、`claude`、`gemini`、`opencode`;其他平台在
  `platforms.<name>.argv` 里自定义。
- 同一轮的多个 Reviewer 强制绑定不同 profile(隔离);CRITICAL 风险需要
  3 个 `reviewer` profile,HIGH/MEDIUM 各需要 2 个,LOW 也至少 1 个(不再免审)。
- `argv` 不含 `{prompt}` 时,提示词改从 stdin 传入。
- 平台输出兼容:直接 JSON、`{"result": "<json 文本>"}` 信封、正文夹带
  JSON(含代码块)三种形态都会被解析为结果对象。
- `timeout_seconds`、`env`、`retries`、`capabilities.max_concurrency` 均可按平台或
  按 profile 覆盖;`retries`(默认 1 不重试)只对传输级错误(非零退出、超时、
  空 stdout)重试,schema 不合法的结果不重试。
- 旧的 JSON-stdio bridge 配置(`profiles.<id>.command`)继续有效,两种
  风格可在同一配置里按 profile 混用。
- 提示词默认英文;配置顶层 `"language": "zh"` 切换中文说明文字(角色头、
  精确取值行与 JSON schema 保持机器锚定)。
- `review-cycle` 结果含 `agent_bindings` 审计(profile/平台/轮次/attempts,
  以及平台在 stderr 报告时的 `tokens_used`)与 `usage_summary` 用量汇总;
  `review-run` 是 `review-cycle` 的别名;`--dry-run` 只渲染第一轮
  Reviewer 提示词;`python -m goalanchor platforms --probe` 逐平台做
  一次最小调用验证可用性与登录态(消耗少量额度)。
- CRITICAL 风险的请求必须携带 `risk_declaration`(非空 `declared_by` 与
  `reason`),否则 CLI 直接拒绝且不调用任何 Agent。

## 模型一致性(主模型同步)

子 Agent 必须与主 Agent 运行**同一个模型**,否则部分平台会回落到自己的默认
模型导致审核失败。GoalAnchor 在配置层强制这一点:

- 顶层 `"model"` 声明主 Agent 当前模型,或运行时传
  `review-cycle --model <主模型>`(覆盖配置;每次运行都应重新传入当前值,
  主模型中途切换后下一轮自动同步)。
- 生效优先级:profile `model` > `platforms.<name>.model` > 顶层 `"model"`。
  内置平台使用各自 flag 注入(`codex -m`、`claude --model`、`gemini -m`、
  `opencode -m`);自定义平台在 `argv` 里放一个 `{model}` 占位符,或依赖
  默认的 `-m <model>` 插入(`model_flag` 可覆盖 flag)。
- 未经确认的分歧直接拒绝:任何 profile 解析出的模型与主模型不同时,
  `review-cycle`/`--dry-run` 在配置加载阶段即失败,**不会启动任何 Agent**。
  确需用不同模型时,必须先暂停询问用户(保持主模型,还是另选子 Agent 模型),
  并把用户决定记录进配置:`"model_divergence": {"confirmed_by": "...",
  "reason": "..."}`,确认信息会回显进运行输出供审计。
- 每条 `agent_bindings` 记录实际使用的 `model`(stdio bridge 恒为 null,
  桥接宿主自管模型),`usage_summary.models_used` 汇总全程模型,可用于
  核对整个审核周期是否始终钉在主模型上。
- 静默失效路径全部关闭:未声明 `model` 的平台调度会向 stderr 打印告警且
  `usage_summary.main_model` 为 null;`argv` 里硬编码模型 flag(会盖过注入值)
  改为要求使用 `{model}` 占位符,否则拒绝;stdio bridge 配置上声明 `model`
  直接报错(桥接协议不携带模型字段,无法兑现)。

## Reviewer 结果的验收标准

外部平台返回的结果对象会按 `ReviewerResult` / `ArbiterResult` /
`MetaReviewResult` 的 schema 严格校验:`reviewer_id` 必须等于请求的
`agent_id`,`context_fingerprint` 必须等于共享上下文指纹,`WARN`/`FAIL`
必须携带 findings。不合规输出会让该轮调度显式报错,而不是静默放行。
