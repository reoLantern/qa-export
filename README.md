# qa-export

把编码 agent 的问答**原样**导出成 Markdown。

归档范围是**人与 agent 的主线问答**——子 agent 之间的协作过程不导出。

问答全文本来就落在本地磁盘上，没必要让模型再复述一遍——那样既费 token，内容还会走样。
这个 skill 直接解析会话记录文件，逐字取出提问和回答。

同时支持 **Claude Code** 和 **Codex**。

## 安装

```bash
npx skills add reoLantern/qa-export
```

装到全局、两个 agent 都装、不问确认：

```bash
npx skills add reoLantern/qa-export -g -a claude-code -a codex -y
```

装好之后，直接对 agent 说「把刚才那两轮问答记进 QA.md」就行。

## 直接当命令行用

脚本本身不依赖 agent，也不依赖任何第三方库（Python 3.8+ 即可）：

```bash
python3 skills/qa-export/scripts/qa_export.py -l          # 列出当前目录最近会话的轮次
python3 skills/qa-export/scripts/qa_export.py -n 2 -o QA.md
```

这是**零 token** 的用法：在 Claude Code 里用 `!` 前缀直接跑，模型完全不参与。

## 用法

```
qa_export.py [-l] [--sessions] [-n N] [-t SPEC] [-o FILE] [-s ID]
             [-C DIR] [-a {auto,claude,codex}] [--all-branches]
             [--from TEXT] [--to TEXT] [-g TEXT] [--match-answers]
             [--dry-run] [--include-pending] [--tail N] [--no-answer]
```

| 选项 | 作用 |
| --- | --- |
| `-l, --list` | 列出轮次：序号、时间、提问首行 |
| `--sessions` | 列出当前目录下的所有会话（两个 agent 都扫） |
| `-n, --last N` | 导出最后 N 轮 |
| `-t, --turns SPEC` | 导出指定轮次，如 `3,5-7` |
| `--from TEXT` | 从提到 TEXT 的那一轮开始，直到最后 |
| `--to TEXT` | 到提到 TEXT 的那一轮为止，配合 `--from` |
| `-g, --grep TEXT` | 只列出提到 TEXT 的轮次，用来定位序号 |
| `--match-answers` | `--from`/`--to`/`-g` 也在回答里找，默认只找提问 |
| `-o, --out FILE` | 追加写入该文件；不给则打印到 stdout |
| `-s, --session ID` | 指定会话：id 前缀，或 Claude Code 的会话名；默认当前会话 |
| `-a, --agent` | `claude` / `codex` / `auto`（默认） |
| `-C, --cwd DIR` | 指定项目目录，默认当前目录 |
| `--dry-run` | 只报告会导出哪几轮，不写文件 |
| `--include-pending` | 不排除当前进行中的那一轮 |
| `--tail N` | `-l` 只显示最近 N 轮，0 为全部（默认 20） |
| `--no-answer` | 只导出提问 |
| `--all-branches` | 保留被 rewind / 改写重发废弃的旧分支 |

输出形如：

```markdown
## 2026-09-22 14:03 · 重试的退避间隔是怎么定的？

**Q**

重试的退避间隔是怎么定的？

**A**

指数退避，基数 500ms、上限 30s，见 `client/retry.go:88`。
第 n 次重试等 `min(500ms * 2^n, 30s)`，再叠加一个 0–250ms 的随机抖动避免惊群。

<!-- Claude Code session a1b2c3d4 turn 7 -->
```

### 按内容划范围（命令行用）

自己敲命令时，不必先数第几轮：

```bash
qa_export.py -g "退避"                      # 哪几轮提到它
qa_export.py --from "退避" -o QA.md         # 从那轮到最后
qa_export.py --from "退避" --to "限流" -o QA.md
```

子串匹配、大小写不敏感，默认只在提问里找。命中多轮时不会替你猜，
而是把候选列出来并以退出码 2 结束。

这几个开关是给**人**用的便利。让 agent 导出时不要走这条路：
「从我们开始讨论 xxx 起」是个理解问题，而用户的说法多半不会逐字出现在任何一条提问里。
agent 的上下文里本来就有整段对话，正确做法是 `-l --tail 20` 看序号、自己对上、再用 `-t`。
脚本只负责按序号取原文，理解交给 agent——这样「关于 yyy 的那几轮」
「我提架构问题的那些」这类说法都不需要再为每种加一个开关。

## 记录在哪

| Agent | 路径 |
| --- | --- |
| Claude Code | `~/.claude/projects/<cwd 把非字母数字换成 ->/<session-id>.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl` |

两边都是一行一条 JSON。Claude Code 按项目分目录；Codex 不分，所以要读每个 rollout 首行的
`session_meta.cwd` 来筛出属于当前项目的会话。

`CLAUDE_CONFIG_DIR` 和 `CODEX_HOME` 环境变量都会被尊重。

Claude Code 的会话名以 jsonl 里的 `custom-title` 条目为准（`~/.claude/sessions/<pid>.json`
只在会话进程活着时存在），所以 `-s` 可以直接写会话名，会话结束后也查得到：

```bash
qa_export.py -s review-bot -l      # 自动切到那个会话的项目目录，不用先 cd
```

fork 之后会出现多个同名会话。此时：自己就是其中之一就用自己的，否则不猜，
列出候选让你用会话 id 指定。`-s <id 前缀>` 也会跨项目目录查找。

## 处理掉的坑

- **过滤注入内容**。`<system-reminder>`、斜杠命令回显、工具调用与结果、Codex 每轮注入的
  `<environment_context>` 信封和 AGENTS.md 正文，都不是用户说的话，一律剔除。
- **rewind / fork 留下的废弃分支**。这是最要紧的一类。rewind 回去重问，旧分支连同
  它完整的回答都留在文件里，导出时会和当前对话混在一起。
  判据是结构性的：从活跃叶子沿 `parentUuid` 回溯得到当前分支，凡是「不在链上、
  但往上能走到链上」的记录都是分叉出去又被放弃的部分，整段剔除。
  走不回链上的一律保留——那通常是父链被截断，宁可多导出也不能让历史凭空消失。
- **改写重发留下的双份提问**。按 Esc 改掉提问再发，旧的那条仍留在记录里，而且它
  没有回答，所以上面的结构判据抓不到。补充判据：Claude Code 侧是「两条提问
  `parentUuid` 相同、中间没有任何回答」，Codex 没有分支树，判据是「上一条没等到
  回答，且是本条的前缀」。
- 以上都可以用 `--all-branches` 关掉，导出全部分支。
- **中途打断的轮次**。用户 Esc 打断后接着补充，那一轮确实没有回答，`-l` 会用 `*` 标出来。

## 会话中途换文件

一次会话可能中途换到新的 jsonl（换模型等），旧文件末尾留一条 `continued-in`
指向后继。环境变量里的会话 id 会跟着更新，所以「导出我自己这个会话」不受影响；
但 `-s <旧 id>` 不处理的话拿到的是截断的视图，所以工具会自动跟到后继。

只在**无损**时才跟：`continued-in` 也可能指向「压缩成摘要后新开的会话」，
那种后继只有摘要和之后的几轮。判据是后继必须真的包含旧文件的最后一轮、
且总轮数不减——不能只在后继里搜一段原文，compact 摘要会把旧对话抄进去，
搜得到但轮次并不在。

## 时序：当前这一轮

会话记录是**实时写盘**的，不是退出时才回写：实测会话进行中文件每隔一两秒就在增长，
最后一条 assistant 记录落盘时间距当下只有几秒。

不过某一轮的**回答**要等生成完才落盘，所以「刚刚这一轮」只有在它结束之后才导得全。

更要紧的是：当你对 agent 说「把最近两轮记进 QA.md」，**你这句话本身已经是记录里的
一轮了**，只是还没有回答。如果不处理，`-n 2` 就会把这条请求本身算进去，错位一轮。

两边的 agent 都会把自己的会话 id 写进环境变量（`CLAUDE_CODE_SESSION_ID` /
`CODEX_SESSION_ID`），所以脚本能判断「我读的就是我正在跑的这个会话」，
此时 `-n` 自动跳过最后那一轮，并在 stderr 上说明。要包含它就加 `--include-pending`。

## 只读

脚本只读会话记录，唯一的写入是 `-o` 指定的文件，且是追加。

## License

MIT
