# GoalAnchor 代码自动测试任务

本文件测试 Python 实现、固定回归夹具和 CLI，不测试自然语言对话体验。对话体验测试请使用
[GoalAnchor-对话体验测试任务.md](GoalAnchor-对话体验测试任务.md)，并在实际 GoalAnchor 对话上下文中执行。

## 任务目的

读取本文件中的机器可读测试定义，自动验证 GoalAnchor 的核心契约、阶段闸门、证据与实验校验、交付闭环、工具集成、兼容性和固定回归场景。

运行器只执行下方定义的受控检查，不把任意 Markdown 文本当作 shell 命令，也不把单项测试通过直接解释为产品闭环完成。

## 执行命令

在仓库根目录执行：

```powershell
python scripts/run_test_task.py --task docs/GoalAnchor-自动测试任务.md
```

默认报告写入 `test-results/goalanchor-baseline.json`。该目录是本地生成物，不纳入 Git。

如果要指定报告路径：

```powershell
python scripts/run_test_task.py `
  --task docs/GoalAnchor-自动测试任务.md `
  --report test-results/goalanchor-baseline.json
```

退出码约定：

- `0`：所有检查通过，且固定回归夹具全部与预期对齐；
- `1`：至少一项检查失败；
- `2`：任务文档格式错误、检查类型不支持或运行器无法准备任务。

## 检查范围

| 检查 | 验证内容 |
| --- | --- |
| `pytest` | 执行仓库的完整自动化测试 |
| `fixtures` | 执行固定回归夹具，确认预期通过/失败和 finding code 对齐 |
| `cli` | 运行受控的 `python -m goalanchor` 命令并校验 JSON 字段 |
| `file` | 确认关键 Skill、脚本和夹具文件存在 |

## 机器可读测试定义

<!-- GOALANCHOR_TEST_TASK_START -->

```json
{
  "schema_version": 1,
  "task_id": "goalanchor-baseline",
  "description": "GoalAnchor 当前版本的核心行为和回归验收",
  "checks": [
    {
      "id": "full-test-suite",
      "type": "pytest",
      "args": ["-q"],
      "timeout_seconds": 600
    },
    {
      "id": "fixed-regression-fixtures",
      "type": "fixtures",
      "fixture_dir": "tests/fixtures/regression",
      "require_all_aligned": true,
      "timeout_seconds": 120
    },
    {
      "id": "compatibility-cli-dry-run",
      "type": "cli",
      "args": [
        "install",
        "--target",
        "codex",
        "--scope",
        "project",
        "--project-root",
        ".",
        "--source",
        "skills/goalanchor",
        "--dry-run",
        "--json"
      ],
      "expected_exit": 0,
      "json_assertions": {
        "target": "codex",
        "scope": "project",
        "dry_run": true,
        "source.skill_id": "goalanchor"
      },
      "timeout_seconds": 60
    },
    {
      "id": "required-project-assets",
      "type": "file",
      "paths": [
        "README.md",
        "pyproject.toml",
        "skills/goalanchor/SKILL.md",
        "goalanchor/core.py",
        "goalanchor/gates.py",
        "goalanchor/evidence.py",
        "goalanchor/closure.py",
        "goalanchor/integration.py",
        "goalanchor/regression.py",
        "tests/fixtures/regression/cases.json"
      ]
    }
  ]
}
```

<!-- GOALANCHOR_TEST_TASK_END -->

## 扩展规则

新增检查时，只使用运行器支持的类型，并为每项提供唯一的 `id`。命令参数以数组保存，运行器不会经过 shell 展开；路径必须位于项目根目录内。

- `pytest` 可通过 `args` 指定测试路径或 pytest 参数；
- `fixtures` 必须提供 `fixture_dir`，可用 `require_all_aligned` 要求所有场景对齐；
- `cli` 必须提供参数数组，可用 `expected_exit` 和 `json_assertions` 验证结果；
- `file` 可提供 `paths`，所有路径都按项目根目录解析。

修改测试任务文档后，应重新运行运行器，并把报告中的版本、检查结果和失败原因作为验收证据保存或提交。
