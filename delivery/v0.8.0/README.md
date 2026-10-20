# v0.8.0 交付目录

本目录是 v0.8.0 的唯一权威交付目录，位于 Git 仓库内：

```text
Project\GoalAnchor\delivery\v0.8.0\
```

| 文件 | 用途 |
|---|---|
| `goalanchor-0.8.0-py3-none-any.whl` | 正式 wheel |
| `goalanchor-0.8.0.tar.gz` | 正式 sdist |
| `SHA256SUMS.txt` | wheel/sdist 校验和 |
| `release-manifest.json` | 发布元数据：提交、tag、Release、镜像与资产哈希；同时保存发布说明摘要 |
| `release-notes.md` | 发布说明正文 |
| `README.md` | 本文件：目录说明与校验方式 |

`release-manifest.json` 中的 `release_notes_file` 保留发布当时的路径
`D:\Project\GoalAnchor\Delivery\v0.8.0-release-1af37a3\release-notes.md`；
该文件现已迁入本目录，实际位置为 `delivery\v0.8.0\release-notes.md`。

## 校验

在当前目录执行：

```powershell
Get-FileHash -Algorithm SHA256 .\goalanchor-0.8.0-py3-none-any.whl
Get-FileHash -Algorithm SHA256 .\goalanchor-0.8.0.tar.gz
```

应分别为：

```text
cb74dadedeb6dd29a3101be05603ec73227142acb0c32995429cff05aa1dd29e  wheel
e98e26fb0b16ed3bb9d061d77f7d3096a0304b73141959811d85ecca70134c0d  sdist
```

`release-manifest.json` 与 `SHA256SUMS.txt` 是同一组资产的两种记录：前者含完整发布上下文，后者用于快速校验。

## 历史候选

原 `Delivery\v0.8.0\` 是发布前候选，wheel/sdist 哈希与正式 Release 不同，已移至工作区本地状态目录：

```text
D:\Project\GoalAnchor\.workbuddy\archive\delivery-v0.8.0-candidate-2026-10-16\
```

它不参与安装、校验或发布；需要追溯候选构建时再从本地归档读取。

## 维护规则

1. 每个版本一个目录，目录内含本说明、包、校验和、manifest 与发布说明；不再在仓库外维护第二套 `Delivery`。
2. 包哈希不同的产物不得互相覆盖、重命名合并或混合校验。
3. 删除任何交付物前，先确认 manifest 引用、远端 Release 可回读，且没有其他文档依赖其路径。
4. 交付报告、验收映射等文字证据的权威位置是 `Doc\03_验证与测试\`；本目录只保留发布所需的包、校验和与发布说明。
5. `release-manifest.json` 是发布元数据的单一来源；其他位置需要引用时使用链接或路径，不再复制一份。
