# Skill 来源与同步约定

## 目录分层

`skills/.system/` 中的 `imagegen`、`openai-docs`、`plugin-creator`、`review-agent`、`skill-creator` 和
`skill-installer` 是 Codex 系统 Skill 的本地快照；`deeply`、`graphify` 和 `goalanchor` 是本项目直接维护的 Skill。

系统快照默认按完整目录同步，不应在本地无记录地零散修改。需要定制时，优先放入非 `.system` 目录；如果安全性或兼容性修复必须落在快照内，应在本文件和提交说明中记录本地补丁，后续上游同步时重新核对。

当前已记录的本地补丁：

- `skill-installer/scripts` 增加 GitHub 请求和 Git 子进程超时，并把网络/JSON 异常转换为用户可读错误，避免安装流程无限等待或输出 traceback。
- `skill-creator/scripts` 统一使用 UTF-8 读写，避免中文 Windows 默认编码导致 Skill 校验或生成失败。

## 版本检查

- Graphify Skill 版本记录在 `skills/graphify/.graphify_version`。
- 运行时安装包版本必须与该文件兼容；如果命令提示版本不一致，应先升级或固定运行时，再生成新的图谱。
- 每次上游同步应执行 `python -m pytest -q`，并检查 frontmatter、`agents/openai.yaml`、脚本帮助命令和 Git 差异。

## Git 记录

版本演进使用中文提交信息和标签记录。不要删除历史提交来“整理”版本；若多个版本最终内容相同，应通过 `CHANGELOG.md` 或标签明确说明每个版本的目的和验证结果。
