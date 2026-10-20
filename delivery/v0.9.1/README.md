# v0.9.1 交付目录

本目录是 v0.9.1 的唯一权威交付目录，位于 Git 仓库内：

```text
Project\GoalAnchor\delivery\v0.9.1\
```

| 文件 | 用途 |
|---|---|
| `goalanchor-0.9.1-py3-none-any.whl` | 正式 wheel |
| `goalanchor-0.9.1.tar.gz` | 正式 sdist |
| `SHA256SUMS.txt` | wheel/sdist 校验和 |
| `release-manifest.json` | 发布元数据：提交、tag、Release、镜像与资产哈希 |
| `release-notes.md` | 发布说明正文 |
| `README.md` | 本文件：目录说明与校验方式 |

## 校验

在当前目录执行：

```powershell
Get-FileHash -Algorithm SHA256 .\goalanchor-0.9.1-py3-none-any.whl
Get-FileHash -Algorithm SHA256 .\goalanchor-0.9.1.tar.gz
```

应分别为：

```text
c6032c89c1a868d7f4cf2e70467dfe934661cbaa61df3c72e68170bd4c4a143c  wheel
e10e3a29b5db0116bc37497df5865d7cb138ecd392add6e33e2d7508acffc394  sdist
```

## 版本边界

- 版本类型：MINOR，向后兼容。
- 新增 `StrictWorkflow` 增量 API；旧 `Workflow` 路径保持可用。
- Strict 审核、问答、授权、执行、reconcile 与关闭仍返回 `UNAVAILABLE`；P2b–P7 未完成。
- 核心场景 58/58 PASS；原生宿主 58/58 `NOT_RUN`。

## 维护规则

1. 每个版本一个目录，目录内含本说明、包、校验和、manifest 与发布说明。
2. 包哈希不同的产物不得互相覆盖、重命名合并或混合校验。
3. `release-manifest.json` 是发布元数据的单一来源；其他位置引用路径，不再复制。
4. 旧版本 `delivery/v0.8.0` 保留为历史交付，不参与 v0.9.1 校验。
