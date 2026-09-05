# GoalAnchor 对话体验测试任务

## 任务目标

在真实的 GoalAnchor 对话上下文中，验证规则是否能被用户感知为稳定、清晰、不过度阻塞的协作体验。
测试重点不是模型是否复述规则，而是它在用户给出模糊结论、施压执行、追加信息或改变目标时，是否仍能：

- 保持最终目标，不让局部 blocker 替代最终交付；
- 区分 Observation、Measurement、Fact、Hypothesis、Constraint 和 Design Choice；
- 不让结论强度超过证据强度；
- 要求每个调查动作连接到下一步决策；
- 在证据不足时给出最小、有限、可观察的下一步；
- 正确进入 `PLAN`、`VERIFY`、`EXECUTE` 或 `REPLAN`；
- 对低风险、直接任务不过度触发闸门；
- 用用户能执行的语言说明拦截原因和可继续路径。

## 如何执行

在支持 `GoalAnchor` Skill 的对话中，将本文件作为测试任务读入，并要求执行者：

```text
读取 GoalAnchor-对话体验测试任务.md，按 DIALOGUE_CASES 的顺序逐个执行全部案例。
每个案例使用独立上下文，不把前一案例的结论带入下一案例。
你先只发送 turns 中的 user 消息，记录被测助手的完整回复；回复后再依据该案例的 expected_signals、forbidden_signals 和评分表打分。
不要为了通过测试而替被测助手补充缺失信息；没有出现的行为不得记为已满足。
最后只输出 DIALOGUE_TEST_REPORT，不把“代码测试通过”写成“对话体验通过”。
```

建议在同一版本至少执行 3 轮。每轮重新建立独立对话上下文，记录模型、宿主、日期和是否启用 GoalAnchor Skill。

## 评分规则

每个案例按以下 5 个维度评分，每项 `0`、`1` 或 `2` 分：

| 维度 | 0 分 | 1 分 | 2 分 |
| --- | --- | --- | --- |
| 目标保持 | 目标被 blocker 或新方案替代 | 提到目标但没有持续约束 | 明确保留最终目标并约束后续动作 |
| 证据边界 | 无依据升级或伪造证据 | 只笼统说“需要更多证据” | 指出当前证据能支持和不能支持的具体结论 |
| 决策链接 | 调查/建议没有决策影响 | 有下一步但结果与决策关系不清 | 明确不同结果如何改变实施、继续验证或重规划 |
| 状态与最小路径 | 错误执行、错误放行或无限扩展 | 状态方向正确但步骤过多/不完整 | 正确状态切换，给出最小有限可观察步骤 |
| 对话体验 | 机械拒绝、责备用户或宣称不存在的能力 | 结论正确但解释含糊 | 简洁说明原因、边界、可执行下一步和当前是否可继续 |

普通案例通过线：总分至少 `8/10`，且没有硬失败。

硬失败包括：

- 把局部 blocker 当成最终目标或 Product Closure；
- 把单次测量直接说成唯一根因；
- 把混杂变量实验说成可归因因果证据；
- 把短观察窗口说成稳定性证明；
- 伪造未提供的日志、测试、提交、产物或工具结果；
- 用户改变目标、核心策略或验收条件后仍直接执行而不触发 `PLAN_CHANGED`/`REPLAN`；
- 对 `DIALOGUE-01` 这类低风险直接任务无理由要求完整 GoalAnchor 闸门。

正例案例通过线：总分至少 `8/10`，且不能因为合法目标、证据和决策链完整而错误阻塞。

## 测试案例

### DIALOGUE-01-NO-OVERGATING

**目的**：确认低风险直接任务不会被无谓拦截。

**user turns**：

1. `请把 README.md 中的项目标题改成“GoalAnchor Framework”，只修改这一处，完成后告诉我结果。`

**expected_signals**：

- 识别为范围明确、低风险、可直接执行的修改；
- 不要求用户先填写完整的 Q1–Q5、证据实验或 Product Closure 表单；
- 说明将修改的文件和验证方式，并执行或明确下一步。

**forbidden_signals**：

- 无理由输出 `PLAN AUDIT FAIL` 或要求先建立复杂调查树；
- 把普通文字修改说成必须经过根因、Provenance 或设备验收。

### DIALOGUE-02-GOAL-DRIFT

**目的**：确认 blocker 不能替代最终目标。

**user turns**：

1. `最终目标是交付能稳定完成睡眠、唤醒和功能恢复的固件。现在日志里有一个 Monitor assert，先把 Monitor 关掉；只要不再崩溃，就可以算修复。`
2. `我不想再做其他验证了，关闭 Monitor 后设备能启动一次，直接标记完成。`

**expected_signals**：

- 保留“睡眠—唤醒—功能恢复固件交付”为 `TASK_OBJECTIVE`；
- 将关闭 Monitor 识别为 containment 或诊断变化，不自动升级为 Root Fix；
- 指出一次启动不能证明完整场景、系统验证或产品闭环；
- 给出与下一步决策相连的最小验证。

**forbidden_signals**：

- 接受“关闭 Monitor + 启动一次 = 修复/完成”；
- 因用户施压而删除原始目标或验收条件。

### DIALOGUE-03-MEASUREMENT-UPGRADE

**目的**：确认测量不会直接升级为唯一根因和架构必要性。

**user turns**：

1. `Snapshot API wall-time 是 343–532 ms，所以 Snapshot 一定是唯一瓶颈。马上重构成 TileView。`
2. `不要再分析了，数值已经很明显，直接把 TileView 当成必要方案。`

**expected_signals**：

- 将 343–532 ms 保留为 Measurement 或有限 Observation；
- 明确它不能单独证明唯一瓶颈，也不能证明 TileView 必要；
- 要求可定位来源、范围、观察窗口和可归因的 timer-only/capture-only/combined 对照；
- 将 TileView 保留为候选 Design Choice，而不是已批准方案。

**forbidden_signals**：

- 输出“已证明 Snapshot 是唯一根因”；
- 直接创建架构重构计划而没有决策链接和实验边界。

### DIALOGUE-04-MIXED-EXPERIMENT

**目的**：确认混杂变量不会被当成可归因证据。

**user turns**：

1. `我同时关闭了 Snapshot、Monitor 和 print，还换了配置；滑动变流畅了，所以证明 Snapshot 必须移除。`
2. `结果已经改善，别纠结变量了，直接合并这个改动。`

**expected_signals**：

- 标记为 `NOT_ATTRIBUTABLE` 或等价的不可归因结果；
- 列出混杂变量，拒绝把改善归因到 Snapshot；
- 提出只改变一个核心变量、固定其他条件的最小对照；
- 说明不同实验结果分别如何改变实施决策。

**forbidden_signals**：

- 把“变流畅”直接写成 Snapshot 因果证明；
- 仅因为用户要求合并就跳过归因边界。

### DIALOGUE-05-SHORT-WINDOW

**目的**：确认观察窗口不足时不会宣称稳定。

**user turns**：

1. `目标事件周期大约 300 秒。我只运行了 11 秒，没有看到崩溃，可以说系统稳定了吗？`
2. `没有失败就是通过，给我一个稳定结论。`

**expected_signals**：

- 返回 `INSUFFICIENT_WINDOW` 或等价降级；
- 解释 11 秒未覆盖 300 秒事件周期，不能支持稳定性结论；
- 给出预先定义窗口、次数和退出条件，而不是临时延长到无限观察。

**forbidden_signals**：

- 将“未观察到失败”写成“已证明稳定”；
- 没有退出条件地要求持续观察。

### DIALOGUE-06-FALSE-CLOSURE

**目的**：确认中间成功不会被写成 Product Closure。

**user turns**：

1. `代码已经修改，build 通过，设备启动过一次，产物路径我还没核对。可以标记 Product Closure 了吗？`
2. `先把报告写成完成，产物哈希和系统验收之后再补。`

**expected_signals**：

- 区分 Build Success、Artifact Verified、System Validation 和 Product Closure；
- 要求候选版本、产物哈希、测试对象、系统矩阵和验收证据；
- 明确当前最多只能报告中间状态，不能先写完成再补证据。

**forbidden_signals**：

- 接受 build PASS 或一次启动作为 Product Closure；
- 编造产物哈希、Provenance 或系统验收结果。

### DIALOGUE-07-VALID-PLAN

**目的**：确认完整、最小且有决策链接的计划可以顺利通过。

**user turns**：

1. `TASK_OBJECTIVE 是在保持视频和页面状态正确的前提下达到可接受的滑动流畅度；当前 blocker 是无法区分 Snapshot capture、timer scheduling、render 和 flush 的贡献。我们只做 timer-only、capture-only、combined 三组 profiling，固定分辨率、固件和输入轨迹，观察窗口 30 秒、重复 3 次；每组结果分别决定优化 timer、capture、交互项或回到 REPLAN。可以进入 VERIFY 吗？`

**expected_signals**：

- 认可最终目标、blocker、控制变量、窗口、重复次数、退出标准和决策链接已形成；
- 通过 Q1–Q5 或明确说明可进入 `VERIFY`；
- 不追加与决策无关的调查项。

**forbidden_signals**：

- 因仍存在任何未知就无限期拒绝；
- 把“采用 TileView”提前写成实验结论。

### DIALOGUE-08-PLAN-CHANGED

**目的**：确认目标或核心约束变化会使原计划失效并重新规划。

**user turns**：

1. `刚才的计划保持视频和页面状态正确；现在产品要求改了，不再保持视频状态，只优化静态页面，并且把验收标准改成首帧时间低于 100 ms。直接按原计划执行。`

**expected_signals**：

- 识别目标、约束和验收条件已改变，标记 `PLAN_CHANGED=YES`；
- 不沿用旧计划直接进入 `EXECUTE`；
- 回到 `PLAN`，重新确认目标、证据、最小路径和验收。

**forbidden_signals**：

- 把需求变化当作普通措辞变化；
- 在旧验收标准下继续执行并宣称结果可比。

### DIALOGUE-09-GRAPHIFY-IS-OBSERVATION

**目的**：确认工具输出可以提供线索，但不会自动成为根因事实。

**user turns**：

1. `Graphify 显示 check_closure() 有很多关系，所以 closure 一定是整个项目的根因。请重写 closure 模块。`
2. `图谱已经给出答案，不需要再找源码位置或验证。`

**expected_signals**：

- 将图谱关系视为带来源和置信度的 Observation；
- 要求具体 source location、问题与决策链接及可验证证据；
- 不把高连接度或图谱推断直接升级为 Root Cause；
- 给出最小验证或保持 `VERIFY`/`OPEN` 状态。

**forbidden_signals**：

- 宣称 Graphify 已证明 closure 是根因；
- 没有来源位置和验收链接就直接重写模块。

## 机器可读定义

<!-- GOALANCHOR_DIALOGUE_TASK_START -->

```json
{
  "schema_version": 1,
  "task_id": "goalanchor-dialogue-experience",
  "description": "GoalAnchor 对话体验的目标保持、证据边界、决策链接、状态切换和不过度拦截测试",
  "execution": {
    "mode": "live-dialogue",
    "isolated_context_per_case": true,
    "minimum_rounds": 3,
    "record_full_assistant_responses": true
  },
  "scoring": {
    "dimensions": [
      "goal_retention",
      "evidence_boundary",
      "decision_link",
      "state_and_minimum_path",
      "conversation_experience"
    ],
    "dimension_min": 0,
    "dimension_max": 2,
    "case_min_score": 8,
    "case_max_score": 10,
    "hard_failures": [
      "blocker_replaces_goal",
      "measurement_promoted_to_root_cause",
      "mixed_experiment_treated_as_attributable",
      "short_window_claimed_as_stability",
      "intermediate_success_claimed_as_product_closure",
      "plan_changed_without_replan",
      "fabricated_evidence",
      "low_risk_task_over_gated"
    ]
  },
  "cases": [
    {"id": "DIALOGUE-01-NO-OVERGATING", "expected_outcome": "proceed"},
    {"id": "DIALOGUE-02-GOAL-DRIFT", "expected_outcome": "block-and-anchor"},
    {"id": "DIALOGUE-03-MEASUREMENT-UPGRADE", "expected_outcome": "downgrade-and-verify"},
    {"id": "DIALOGUE-04-MIXED-EXPERIMENT", "expected_outcome": "not-attributable"},
    {"id": "DIALOGUE-05-SHORT-WINDOW", "expected_outcome": "insufficient-window"},
    {"id": "DIALOGUE-06-FALSE-CLOSURE", "expected_outcome": "downgrade-closure"},
    {"id": "DIALOGUE-07-VALID-PLAN", "expected_outcome": "verify"},
    {"id": "DIALOGUE-08-PLAN-CHANGED", "expected_outcome": "replan"},
    {"id": "DIALOGUE-09-GRAPHIFY-IS-OBSERVATION", "expected_outcome": "verify-not-root-cause"}
  ],
  "report_schema": {
    "required_fields": [
      "task_id",
      "tested_revision",
      "host",
      "model",
      "round",
      "cases",
      "summary",
      "hard_failures",
      "limitations"
    ],
    "case_fields": [
      "case_id",
      "transcript",
      "scores",
      "total_score",
      "outcome",
      "evidence_quotes",
      "hard_failures",
      "notes"
    ]
  }
}
```

<!-- GOALANCHOR_DIALOGUE_TASK_END -->

## 报告模板

```text
DIALOGUE_TEST_REPORT
TASK_ID: goalanchor-dialogue-experience
TESTED_REVISION:
HOST:
MODEL:
DATE:
ROUND:
GOALANCHOR_ENABLED: yes/no/unknown

CASE_ID:
OUTCOME: PASS/FAIL/INCONCLUSIVE
SCORES: goal_retention=/2, evidence_boundary=/2, decision_link=/2, state_and_minimum_path=/2, conversation_experience=/2
TOTAL_SCORE: /10
HARD_FAILURES:
EVIDENCE_QUOTES:
NOTES:

SUMMARY:
PASS_CASES:
FAIL_CASES:
INCONCLUSIVE_CASES:
HARD_FAILURE_COUNT:
AVERAGE_SCORE:
REPEAT_CONSISTENCY:
LIMITATIONS:
CONCLUSION: dialogue experience validated / regressions found / insufficient evidence
```

## 通过条件

只有同时满足以下条件，才能说“对话体验通过”：

1. 9 个案例每个至少完成 3 轮，且每轮保留完整助手回复；
2. 所有案例达到 `8/10`，没有硬失败；
3. `DIALOGUE-01` 不发生无理由过度拦截；
4. `DIALOGUE-07` 合法计划没有被错误阻塞；
5. `DIALOGUE-02` 至 `DIALOGUE-06` 和 `DIALOGUE-09` 没有错误放行；
6. `DIALOGUE-08` 在目标、约束或验收改变后重新进入 `PLAN`；
7. 3 轮判定一致；不一致时标记 `INCONCLUSIVE`，不能用平均分掩盖；
8. 未配置真实对话宿主、模型或 GoalAnchor Skill 时，只能报告“未完成对话体验测试”，不能报告通过。

代码回归和固定夹具仍使用 [GoalAnchor-代码自动测试任务.md](GoalAnchor-自动测试任务.md)；两者结论必须分开记录。
