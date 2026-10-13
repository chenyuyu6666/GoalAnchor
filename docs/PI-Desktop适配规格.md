# PI-Desktop 适配规格

目标：为 vastsa/PI-Desktop 提供可导入的 GoalAnchor Skill/Agent Extension 插件，以 0.18.0 为最低验证基线，兼容后续保留所需公开协议的版本。复用现有 Python 内核，完成安装、审核、工具门控与可复现验证。用户要求覆盖后续版本，不能将实现锁死在 0.18.0。

## 能力地图与构建顺序

| 模块 | 责任 | 依赖 |
|---|---|---|
| pi-desktop-package | 插件 manifest、Skill、Python 内核、可导入目录和包 | — |
| pi-desktop-bridge | JSON-over-stdio、闸门、角色提示词和结果校验 | pi-desktop-package |
| pi-desktop-runtime | 桌面模型审核、工具 Hook、命令、状态和取消 | pi-desktop-bridge |

用户已确认模块范围。构建顺序：package → bridge → runtime。

修订规格、实现计划与测试任务已获用户批准。实现、离线/发行包接口验证及所列范围的真实 GUI/模型验收完成；详见 [验证记录](PI-Desktop验证记录.md)。未知未来版本不作为实测成果。

## 接口依据

- https://github.com/vastsa/PI-Desktop
- https://pi-docs.aiuo.net/spec/07-plugins/16-trusted-extensions
- https://pi-docs.aiuo.net/spec/07-plugins/03-plugin-api
- 本机 0.18.0 发行包：插件贡献 `skills` 与 `agentExtensions`，支持 `agent.complete`、工具注册、ExtensionAPI `tool_call` 阻断。
- 不把 Pi CLI 的 `.pi/skills` 安装视为桌面加载。桌面使用 Plugins 的本地导入/开发插件入口；不直接写数据库或授权记录。

## 验收

1. 构建一个不含第三方生产依赖的插件目录与可导入包，携带单一规则源及 Python 模块；不要求源码仓库位于 Python 搜索路径。
2. 桌面模型审核复用 Python `run_review_cycle_with_adapter`；每次调用独立上下文，同模型，风险人数、争议复审和最多两轮不变。返回角色、模型、轮次和用量审计；未知模型身份或不一致必须拒绝。
3. 工具门控只在用户主动开启时生效。绑定明确的工具名和完整参数，执行前校验 PLAN/VERIFY/EXECUTE，批准仅允许匹配操作；不将模型自报 PASS 当授权。
4. 每会话隔离状态，切换/重启不继承批准；工具失败、参数变化、模型变化、取消和异常使批准失效；不阻断普通未启用会话。
5. 支持中文路径、解释器路径含空格、宿主停止、Python 缺失、无效 JSON、大小限制和超时。所有进程用 argv 启动，禁止 shell 拼接。
6. 文档给出安装、设置、命令、正常/失败案例、卸载及限制；Skill 格式符合桌面目录/manifest 协议。
7. pytest、JS 行为测试、Ruff、自带 Skill 同步、独立打包 smoke 通过。区分官方协议检查、模拟宿主、发行包检查和实际 GUI/模型验证。
8. 同一适配包覆盖最低基线与实现时的最新稳定版公开协议；实际验证版本单独记录。未知未来版本以能力/契约检测判定可用性，不冒充已验证版本。新增可选字段不导致失败；必需接口缺失或输出契约不兼容时返回明确诊断，不产生 PASS 或执行批准。

## 后续版本兼容策略

- **声明与实测分开**：manifest 使用合理的最低宿主版本要求，不限定为恰好 0.18.0；最低要求不代表所有后续版本已通过实测。
- **能力检测优先**：启动时检测公开 API 是否存在；调用时处理 `UNSUPPORTED` 等宿主错误，并校验输出形态。版本号仅用于已知协议差异与诊断，不能替代接口验证。
- **兼容边界集中**：桌面 API 的调用和返回值规范化集中在一个兼容层，不在领域规则中散布版本分支。不修改宿主安装包，不依赖压缩源码符号、内部数据库或私有 IPC；本机解包只作为协议核对证据。
- **可选功能降级**：状态 UI、用量等可选能力缺失时，保留核心结果并报告降级。必需的模型身份、独立审核或工具 Hook 缺失时，明确禁用对应功能；已启用门控不可自动退为放行模式。
- **未来扩展不自动改变策略**：后续宿主增加可见子会话、取消接口等能力时，仅通过已知且有测试的接口接入；不猜测签名，不自动授予权限，不改变风险人数、两轮上限或用户批准语义。
- **协议演进**：本地 JSON 桥接声明协议版本；不支持的协议版本必须拒绝。接受宿主非关键新增字段，但完整校验用于裁决的必需字段。
- **兼容测试矩阵**：包括 0.18.0 协议夹具、实现时最新稳定版夹具、增加字段的未来形态、可选能力缺失、必需能力缺失、返回值破坏性变化、取消和超时。另保留发行包外安装 smoke 与实际桌面验证入口，后续升级可重复执行。

## 宿主限制与策略

0.18.0 的插件 tool 有 110 秒总预算，模型 completion 有 90 秒预算和速率限制。适配器采用更短总预算，超时报告失败，不改写成 PASS。支持取消本地桥接并丢弃晚到模型结果；宿主 API 不提供的远端请求取消不声称可实现。

优先使用已核实的桌面接口。若 0.18.0 未向插件开放可见子会话派发，使用独立 tools-free completion，明确报告执行方式；不伪造 UI 子会话。

Extension Hook 是宿主协作式拦截：宿主忽略抛错/超时的结果并继续，不是操作系统安全隔离。适配器在自身异常时返回显式 block，但不能保证宿主故障时强制阻断。

## 实现计划与任务

1. 在 `goalanchor/pi_desktop.py` 增加可打包、受限 JSON 桥接；优先复用现有领域 API。
2. 在 `goalanchor/_pi_desktop/` 放可执行 JS 插件资源；构建时复制 Skill 和 Python 模块，避免两份规则源。
3. 添加 CLI 构建入口与文档；不为 desktop 伪造 Pi CLI 命令。
   兼容层提供能力报告，记录宿主版本、协议版本、可用/降级/不可用能力与原因。
4. 先写 Python 和 Node 失败测试，再实现打包、桥接、模型审核和 Extension Hook；做独立导入和边界验证。
5. 对最终差异做独立审查，修复后重跑必要验证，报告剩余宿主限制。

## 命令与边界

```text
python -m pytest -q
python -m ruff check goalanchor tests scripts
python scripts/sync_skill_bundle.py --check
node --test tests/js/test_pi_desktop.cjs
python -m goalanchor.pi_desktop build --destination <new-plugin-directory>
```

保持运行时零第三方 Python 依赖。测试必须覆盖拒绝与失败路径。禁止读取/复制凭据，禁止模型输出决定解释器或执行命令，禁止写宿主授权数据库、改发布标签或推送远端。日常开发不修改版本号。构建/安装产物不提交。
