# 在 Codex 上使用 qa-export

用法和 Claude Code 一致（见 SKILL.md 的两条路径），只是记录格式不同，
而且有几处已知局限——**导出长会话前请先读完本文的「已知局限」**。

## 记录在哪

```
~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl
```

Codex 不按项目分目录，所以工具要读每个 rollout 首行的 `session_meta.cwd`
才能筛出属于当前项目的会话。`CODEX_HOME` 环境变量会被尊重。

当前会话靠 `CODEX_SESSION_ID` 环境变量定位，所以「导出我自己这个会话」
以及「排除进行中的那一轮」都能正常工作。

## 已过滤的注入内容

这些以 user 身份出现、但不是用户说的话，已经剔除：

- 每轮注入的 `<environment_context>` 等信封
- `# AGENTS.md instructions` 开头的项目说明正文
- `developer` 角色的全部消息

## 已知局限

以下三项在 Claude Code 侧做了、Codex 侧没做。原因是 Codex 的记录是**纯线性
追加，没有任何父子指针**（全量扫描本机 531 个 rollout，与分支相关的字段只有
一个 `payload.forked_from_id`），所以 Claude Code 那套「沿父链回溯出当前分支」
的办法在这里无从下手。

| 局限 | 后果 | 规避 |
| --- | --- | --- |
| **backtrack 改写认不出** | 改写前的旧提问和回答会一起导出 | 用 `-l` 核对轮次，用 `-t` 挑 |
| **compact 摘要会被当成提问** | compact 之后 Codex 把摘要以 user 身份重新注入，这一轮会原样进导出 | 导出长会话后检查有没有超长的、不像你说的话的「提问」 |
| **fork 不接父会话历史** | `forked_from_id` 指向的父会话内容不会被带进来 | 父会话单独导出一次 |

实测参考：本机 531 个 rollout 全部解析无异常，共 2007 轮；其中带 `compacted`
记录的会话里，约 44 轮是摘要被当成了提问。

**所以：在 Codex 上「导出最近几轮」是可靠的；「导出一整个长会话」或
「导出 backtrack 过的会话」需要人工核一下 `-l` 的结果。**

## 报告方式

导出完只说写了哪几轮、写进了哪个文件，不要把问答内容复述一遍。
如果这个会话 compact 过或 backtrack 过，提醒用户按上表核对一下。
