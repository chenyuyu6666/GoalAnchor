# GoalAnchor for PI-Desktop

此目录是可导入的桌面插件，包含标准 GoalAnchor Skill、Python 规则内核、同模型独立审核工具及显式工具批准扩展。最低接口基线为 vastsa/PI-Desktop 0.18.0；后续版本按公开接口能力检测，不宣称未来版本已验证。

## 安装与设置

1. 安装 Python 3.11+，保证 `python` 在 PI-Desktop 进程的 PATH 中。Python 内核已随插件提供，不需要另行 pip 安装 GoalAnchor。
2. 在 PI-Desktop 的 Plugins 页选择安装本地包，选择 `.piplug`；开发测试可选择 Load development plugin 并选择本目录。按宿主界面审阅权限并启用。不要只复制到 `~/.pi`。
3. 插件需要 `agent.prompt.inject`（Skill）、`agent.extension`（工具 Hook）、`agent.tool.register`（工具）、`agent.complete`（模型额度）和 `session.read`（核对当前模型身份）。不读取 API key，不申请网络或文件写入 API 权限。受信任扩展自身具备 Node/Python 执行能力，宿主权限模型不是 OS 沙箱。
4. 如果 Python 不在 PATH：在插件设置的 Python 字段填可执行文件完整路径（不加引号）；并在每个门控会话执行 `/goalanchor-python 完整路径`。也可在启动桌面前配置 `GOALANCHOR_PYTHON`，两个入口都会使用它。
5. 新建或继续一个已加载扩展的会话。搜索 `GoalAnchor: Compatibility report` 查看版本和可用能力；输入 `/goalanchor-status` 查看当前会话门控状态。

同版本重装或开发期间修改扩展后，完整退出并重新启动 PI-Desktop，再初始化会话。实测运行中的依赖模块可能仍使用旧缓存；仅关闭窗口或启动第二个实例不能保证更换进程。重新启动后批准不会继承，需重新开启门控。

## Skill 与审核

GoalAnchor Skill 出现在宿主 Skill 目录中，保留同一个规则源。使用 `/skill:local.goalanchor/goalanchor` 或在 Skill 入口选择本插件的 GoalAnchor，让 Agent 按 PLAN → VERIFY → EXECUTE 工作；这能与已有的同名手动导入 Skill 区分。

在普通 Agent 模式要求 Agent 调用 `goalanchor_review`（实际工具名由宿主命名空间包装，例如 `plugin_local_goalanchor_goalanchor_review`），传入：

```json
{
  "request": {
    "context": {
      "goal": "修复登录超时并交付",
      "prompt": "审核候选修复是否满足目标与约束",
      "constraints": ["不得引入回归"],
      "evidence_refs": ["回归测试记录"],
      "candidate": "待审核方案及证据内容"
    },
    "risk": "HIGH",
    "max_rounds": 2
  }
}
```

证据引用字符串不证明证据真实；在 candidate/prompt 中提供审核需要的实际材料。CRITICAL 还必须携带 `risk_declaration: {"declared_by":"用户标识","reason":"风险声明"}`。

每个角色使用当前会话模型和独立、无工具的 completion。审核结果包含裁决、角色结果、模型与轮次审计。上下文不会自动混入原对话；独立调用不能保证模型判断独立或事实真实。0.18.0 没有公开的可见子会话派发接口，不能在侧栏看到每个角色的新聊天。

用 `goalanchor_gate` 检查指定 `phase` 与 `record`，例如 plan、verify、execute、closure。检查结果不会授予工具执行权限。Plan/Goal 模式受宿主工具策略限制，使用 Skill 与 `/goalanchor-check` 做静态/确定性检查；本插件的模型审核工具仅声明 Agent 模式可用。

## 主动开启工具门控

普通会话默认不启用门控。需要它时：

1. 在工作区准备一个 JSON 文件，格式参考本包 `examples/approval.json`，填写真实目标、PLAN 工作项、VERIFY 条件、EXECUTE 预检以及完整工具调用。工具名和参数必须与实际调用完全一致；样例目标文件若已存在，应先更换目标。
2. `/goalanchor-guard on`
3. `/goalanchor-check 相对路径.json`：运行三个阶段检查，不批准执行。
4. `/goalanchor-approve 相对路径.json`：重新检查，然后由宿主确认框显示目标、约束、验收、模型、恢复路径与完整操作。只有用户确认才产生一次性批准。
5. Agent 执行批准操作。额外参数或文本变化也需要重新批准。已核实的只读核心工具（Read/Glob/Grep）、ToolSearch 工具发现和本插件的检查工具仍可用；Write/Edit/Bash 与未知工具必须明确批准。工具名区分大小写，样例使用桌面的 Write，不是 Pi CLI 的 write。
6. `/goalanchor-guard off` 关闭门控。

批准在当前会话内生效，单次消费，参数/模型不匹配、工具失败、Agent 回合结束、会话/运行时销毁会撤销。宿主重建运行时不会继承批准。批准只覆盖声明的调用，不能静态证明 shell 脚本内部行为符合目标。

## 兼容、失败与卸载

- 同一插件使用稳定公开 API，接受非关键新增字段。必需模型身份/审核接口缺失时明确报错；可选展示能力缺失时降级。能力报告与实际验证版本分开记录。
- 模型完成调用单次宿主预算 90 秒，整个插件工具宿主预算 110 秒；本桥接总预算 100 秒。完整两轮审核可能超时，超时不会变成 PASS。0.18.0 的每插件限制是 8 次 completion / 60 秒，连续审核或 CRITICAL 复审可能限流。
- Stop/卸载会终止本地 Python 桥接并丢弃晚到结果。宿主没有公开的远端 completion 取消参数，因此已经发出的模型请求可能继续消耗额度。
- Extension Hook 是协作式检查。宿主会忽略异常/超时的 Hook 并继续；插件自身错误返回显式 block，但不能在宿主失效时提供强制安全隔离。
- 本地 JSON 桥接协议版本为 1，不支持的版本明确拒绝；不依赖私有 IPC 或数据库。
- 在 Plugins 页禁用/卸载即可移除工具和 Skill；当前会话的扩展命令/Hook 在宿主下一回合加载边界刷新，完整退出也会清理，不修改宿主核心文件。若使用开发插件，卸载记录后可删除构建目录。

实际 GUI 启用、模型调用和用户权限授予必须在安装后通过桌面界面验证。离线测试与真实运行包接口检查不冒充这一验证。
