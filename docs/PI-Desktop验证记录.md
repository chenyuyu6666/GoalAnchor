# PI-Desktop 适配验证记录

日期：2026-10-13。开发分支：`feature/pi-desktop-compat`。基础版本：v0.6.0，新增适配属于未发布变更，未更新发布版本、合并或推送。

## 交付与兼容范围

交付内容：可构建的 `.piplug` 与目录插件、单一源 GoalAnchor Skill、随包 Python 规则内核、桌面公开 completion 审核、明确启用的工具门控、能力诊断及 Node/Python 回归。

最低公开协议基线为 0.18.0；不精确锁定该版本。后续版本按 API 能力检测、调用错误处理与输出校验决定是否可用；非关键新增字段被容忍，关键身份/接口不兼容时失败。未知未来版本没有被宣称实测。

| 验证层级 | 证据 | 结论与限制 |
|---|---|---|
| 本地领域与桥接 | pytest 218 项通过；其中新增桌面测试 12 项 | 含风险人数、两轮争议、上下文指纹、模型拒绝、协议版本、独立打包 |
| Node 行为 | 初始 11 项、真实测试修复后 13 项通过 | 0.18.0/0.19.1 协议模拟、未来字段、缺失 API、精确批准、撤销竞态、取消与无效输出，新增桌面核心名称回归 |
| 0.18.0 发行包 | 实际 `validateManifest` 和 `TrustedExtensionRunner` | 加载成功、无诊断错误；命令/Hook 注册，未批准拒绝、批准单次放行、运行时重建清零 |
| 0.19.1 公开契约与真实宿主 | SDK 核对、模拟回归及后续原生安装/审核/门控/Stop/卸载测试 | 所列范围已实际验证，不扩展为所有模式/未来版本保证 |
| wheel 分发 | wheel 构建、临时独立目标安装、仓库外构建插件与桥接预检 | 验证资源随包及与源码仓库隔离；没有第三方生产依赖 |
| GUI/真实模型 | 后续已执行，详见下文 | 原生文件选择器安装，自动化操作实际确认窗口、真实当前模型 LOW 审核；不把自动化确认称为生产人工签署 |

## 实际命令

```text
python -m pytest -q
node --test tests/js/test_pi_desktop.cjs
python -m ruff check goalanchor tests scripts
python -m mypy goalanchor/pi_desktop.py --follow-imports=silent
python scripts/sync_skill_bundle.py --check
git diff --check
python -m pip wheel . --no-deps --wheel-dir build/pi-desktop-wheel
python -m goalanchor.pi_desktop build --destination build/pi-desktop-compatible --archive build/goalanchor-pi-desktop.piplug
```

上述命令通过。新增 Python 模块的类型检查通过；不把这一结果表述为既有全项目 mypy 基线已清零。

发行验证额外在临时目录中用 `pip install --target` 安装 wheel，再通过 `python -I` 从仓库外导入安装包构建插件，执行打包 `runtime/bridge.py` 验证样例 preflight。Skill 源/包副本同步，目录与 `.piplug` 内部保存相对路径，zip 使用宿主支持的 stored 格式。

最初的 `pip wheel --no-build-isolation` 因本机旧 setuptools/缺少 bdist_wheel 失败；改用 pyproject 声明的隔离构建后成功。没有修改生产依赖以掩盖工具环境问题。

Git fetch 曾因既有 Codex checkpoint 引用损坏失败；检查时本地 HEAD 与已存在的 origin/master 引用一致。开发在独立分支进行，没有改写损坏引用或声称已与远端最新状态同步。

## 独立审查与修复

两个独立 Reviewer 分别审查运行时/授权和桥接/分发：

1. 确认竞态：等待用户确认期间已经撤销的批准，会被迟到确认恢复。先添加失败回归，再给撤销增加 generation，并在异步预检/确认后检查。复核 PASS。
2. 输出边界：合法 JSON `null` 导致 stdout 回调未捕获 TypeError。先添加真实 Python 输出的失败回归，再校验非 null、非数组对象；通过统一错误出口清理。复核 PASS。

独立 Meta Reviewer 同意初次离线交付“可交付、尚未发布”的主裁决，并对该次目录/归档逐字节一致性、实际 0.18.0 validator/Runner、撤销确认竞态及 Node 11 项结果复核 PASS。初次离线交付归档 SHA-256：`5eb92a117e5d67c33209dd2eb1d24bfd92ca3ab325dab4697fd371ce9cc4bebb`；它不是下述真实测试修复后的新包。

跨语言 Windows 审核测试另发现输入线程退出崩溃，改为非 daemon reader，并由宿主在收到终态结果后关闭 stdin，完整两轮审核通过。

## 宿主边界与安装后验收

0.18.0 的 plugin tool 总预算为 110 秒、completion 单次 90 秒、每插件 8 次/60 秒。桥接主动使用 100 秒总预算，超时/限流报告失败。没有公开的可见子会话派发或远端 completion 取消接口；停止只终止本地桥接并丢弃迟到结果。Extension Hook 是协作机制，宿主忽略异常/超时的行为不由插件保证强制阻断。

安装后通过宿主原生界面完成：

- 导入包、审核权限、启用插件，确认 Skill 和命令可见。
- 查看 Compatibility report；在 Agent 模式用真实当前模型执行一次 LOW 审核，检查角色/模型审计。
- 在临时工作区使用样例批准流程，分别检查取消确认、修改参数、匹配操作首次放行及再次拒绝。
- 用 Stop/禁用插件终止操作，确认无迟到批准与本地桥接残留；再次加载运行时应为未启用、零批准。

初次交付时这些 GUI/模型检查尚待执行；后续已按用户要求执行下述验收，原始离线记录不替代实际证据。

## 后续真实桌面验收

通过 Python Playwright 连接实际 Electron 窗口，并通过 Windows 原生文件选择器安装本地包。只操作新建的 GoalAnchor 验收临时会话和其 scratch 目录；没有读取 API key 或修改原有工程文件。模型由宿主使用既有配置调用，实际消耗额度。

测试开始时安装版为 0.18.0，宿主已有自动更新安装程序随后更新为 0.19.1。因此实际覆盖两个版本，范围如下：

| 项目 | 实际结果 |
|---|---|
| 0.18.0 安装、Skill、命令 | 原生 `.piplug` 导入成功并启用；插件 Skill 与五条扩展命令可见，状态命令成功 |
| 0.18.0 LOW 审核 | PASS；reviewer-1、arbiter、meta-reviewer 均为当前 gpt-6-luna，三个独立 completion 共记录 2,274 tokens |
| 0.19.1 安装与能力报告 | 成功；能力报告返回实际 0.19.1 及审核可用性 |
| 0.19.1 LOW 审核 | PASS；三个角色模型一致，共记录 2,379 tokens |
| 未批准写入 | 核心 Write 被 Hook 拦截，目标文件未生成 |
| 确认窗口选择否 | 确认取消，批准数保持 0 |
| 确认窗口选择是 | 实际确认后匹配 Write 成功写入 26 字节，读回内容一致；相同调用第二次被拦截 |
| 参数变化 | 批准后改变 content，被拦截且新目标文件不存在 |
| 只读通路 | 修复并完整重启后，门控开启时 Read 和 ToolSearch 成功 |
| Stop | 观察到实际审核 Python 进程后通过原生“停止生成”按钮触发取消；工具记录为 error / Session turn was aborted，本地桥接进程消失，批准为 0；迟到结果没有变成 PASS |
| 卸载 | 插件行及插件工具/Skill 删除；当前会话扩展命令在下一回合刷新后清空，未误删既有的同名手动导入 Skill |
| 重新安装 | 重新安装修复后的包并恢复启用；最终初始化/状态见证据目录 |

这里的模型审核只覆盖 LOW、max_rounds=1。HIGH/CRITICAL 和两轮争议仍采用本地回归证据，不声称完成这些组合的真实模型测试。算术案例只验证审核派发和裁决链，不证明复杂工程效果。

实际测试发现并修复了两个接口遗漏：ToolSearch 原本被当成未知操作阻断；桌面核心名称为 Read/Glob/Grep/Write，与初始 Pi CLI 小写白名单/样例不一致。新增失败回归后修复为四个精确只读名称 Read/Glob/Grep/ToolSearch，Write/Edit/Bash 与未知名称继续要求批准；独立窄复核 PASS，Node 13 项通过，全量 pytest 218 项及 Ruff 通过。

同版本重装后，磁盘资源已更新但依赖模块可能保持旧缓存。已确认完整退出、等待旧 PID 消失再启动新进程后修复生效；仅关闭窗口或在旧进程退出前启动第二实例不足以证明重启。指南已写明此要求。卸载的扩展目录/命令刷新同样遵循下一回合边界。

Stop 首次尝试因使用了过窄的按钮名“停止”未触发，审核自然结束；该轮不计为取消通过。后续识别实际按钮“停止生成”，观察到 bridge 进程后触发并取得取消证据。

证据位于忽略的 `build/pi-desktop-live-evidence/`：原始审核工具结果、按版本审核记录、单次写入/变化参数、确认窗口截图、Read/ToolSearch 修复结果、Stop 错误、卸载清理和最终安装截图。它们仅包含受控验收会话数据，不提交用户宿主数据或凭据。

最终交付包：`build/goalanchor-pi-desktop-tested-delivery.piplug`，SHA-256：`720cc0cfa7dce4a45e880ee3292057e5c52515b96b70fa773ba1fcc3b7b2e7bb`。运行时代码与最终安装实测版本相同，交付包额外同步了重启/卸载刷新和精确 Skill id 的说明。

独立 Meta Reviewer 复核实际审核、写入、变化参数、读取、取消、卸载及重装证据，支持在上述范围报告 PASS。调试进程退出清理曾超时，随后只终止已核实本任务启动的 PI-Desktop 主 PID；已确认调试端口关闭，审核 Python 桥接残留为 0。插件仍安装并启用，门控默认关闭、批准为 0。没有提交、合并、发布或推送此次变更。
