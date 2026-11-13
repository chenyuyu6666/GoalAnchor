# v0.9.3 交付目录

本目录是 v0.9.3 的权威交付目录，包含最终发布包、校验和、发布 manifest 和发布说明。

发布提交：待 tag 定稿后回填（见 `release-manifest.json`）
GitHub Release：[v0.9.3](https://github.com/chenyuyu6666/GoalAnchor/releases/tag/v0.9.3)

| 文件 | 用途 |
|---|---|
| `goalanchor-0.9.3-py3-none-any.whl` | 正式 wheel |
| `goalanchor-0.9.3.tar.gz` | 正式 sdist |
| `SHA256SUMS.txt` | wheel/sdist 校验和 |
| `release-manifest.json` | 发布提交、tag、Release、镜像和资产哈希 |
| `release-notes.md` | 发布说明正文 |

资产哈希和 GitHub 状态以 `release-manifest.json` 为唯一来源。

当前资产：

```text
ee23ce3e340c37bdda7516971a93c3e77b1018351dbecd69c2421e17b7542fab  goalanchor-0.9.3-py3-none-any.whl
0641326c799e777563c5871e5414a6ea244e8b4ecc3c8be310b60eaf2a3804df  goalanchor-0.9.3.tar.gz
```

## 校验

在当前目录执行：

```powershell
Get-FileHash -Algorithm SHA256 .\goalanchor-0.9.3-py3-none-any.whl
Get-FileHash -Algorithm SHA256 .\goalanchor-0.9.3.tar.gz
```

## 版本边界

- 版本类型：PATCH，向后兼容；旧 `Workflow` 路径与序列化格式保持可用。
- 原生宿主仍为 `PARTIAL/BOUNDED`；58 项原生场景按证据记录 `NOT_RUN`，不因本版本发布升级为完成。
- Gitee 镜像本次未同步（远端分叉待处理），只发布 GitHub。
