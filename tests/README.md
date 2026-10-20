# 测试目录说明

本目录包含 GoalAnchor 的 Python 单元/集成测试、固定回归夹具和 PI-Desktop 的 Node 测试。

## 布局

| 路径 | 内容 |
|---|---|
| `test_*.py` | 42 个测试文件，约 15,100 行，覆盖契约、门控、证据、审核、工作流、平台适配与宿主生命周期 |
| `fixtures/regression/cases.json` | 12 个固定回归场景；每项包含期望通过与期望 finding code |
| `js/test_pi_desktop.cjs` | PI-Desktop 桥接的 Node 行为测试 |
| `conftest.py` | 共享夹具与临时目录/进程辅助 |

当前共收集 846 个用例。测试以离线、确定性为原则，不依赖外部模型或网络；平台探测类测试使用受控 fixture。

## 运行

```powershell
cd D:\Project\GoalAnchor\Project\GoalAnchor
python -m pip install -e .
python -m pytest -q

# 固定回归夹具（需要 editable install，子进程会导入 goalanchor）
python scripts/evaluate_fixtures.py --fixtures tests/fixtures/regression --revision HEAD

# PI-Desktop Node 测试（Node.js 20+）
node --test tests/js/test_pi_desktop.cjs
```

CI 在三个操作系统和 Python 3.11–3.13 上运行同一套 pytest，并额外执行文档驱动测试与 Skill 同步检查。

## 编写约定

- 一个测试只验证一个可说明的行为；缺陷修复先补复现用例。
- 使用 `tmp_path` 隔离文件与 SQLite 数据库，不写入仓库目录。
- 不把 fixture 通过等同于原生宿主验收；宿主相关结论需在对应平台实测。
- 并发/跨进程用例应显式设置超时与清理，避免依赖机器负载。

## 已知问题

在 Windows 高负载、全量并发运行时，以下 SQLite 跨进程用例可能因锁竞争失败或长时间等待，单独复跑通过：

- `tests/test_contract_remaining.py::test_t24_t25_two_workflow_processes_one_authorization_one_dispatch`
- `tests/test_idempotent_recovery.py::test_t36_shared_sqlite_processes_elect_one_recovery_transport`
- `tests/test_workflow_store.py::test_two_processes_only_one_dispatch`（负载下曾出现 `attempt to write a readonly database`）

在修复前，遇到这两项失败应先单独复跑并记录负载条件，不要直接判定为产品回归。
