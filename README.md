# qa-export

把编码 agent 的问答**原样**导出成 Markdown。

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
             [-a claude|codex] [-C DIR] [--no-answer] [--sidechain] [--all-branches]
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
| `--sidechain` | 包含子 agent 的对话 |
| `--all-branches` | 保留被改写重发时废弃的旧提问 |

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

### 按内容划范围

不必数第几轮，可以直接说「从讨论某件事开始」：

```bash
qa_export.py -g "退避"                      # 哪几轮提到它
qa_export.py --from "退避" -o QA.md         # 从那轮到最后
qa_export.py --from "退避" --to "限流" -o QA.md
```

子串匹配、大小写不敏感，默认只在提问里找。命中多轮时不会替你猜，
而是把候选列出来并以退出码 2 结束。

## 记录在哪

| Agent | 路径 |
| --- | --- |
| Claude Code | `~/.claude/projects/<cwd 把非字母数字换成 ->/<session-id>.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl` |

两边都是一行一条 JSON。Claude Code 按项目分目录；Codex 不分，所以要读每个 rollout 首行的
`session_meta.cwd` 来筛出属于当前项目的会话。

`CLAUDE_CONFIG_DIR` 和 `CODEX_HOME` 环境变量都会被尊重。

Claude Code 的会话名记在 `~/.claude/sessions/<pid>.json`，所以 `-s` 可以直接写会话名：

```bash
qa_export.py -s review-bot -l      # 自动切到那个会话的项目目录，不用先 cd
```

## 处理掉的坑

- **过滤注入内容**。`<system-reminder>`、斜杠命令回显、工具调用与结果、Codex 每轮注入的
  `<environment_context>` 信封和 AGENTS.md 正文，都不是用户说的话，一律剔除。
- **改写重发留下的双份提问**。按 Esc 改掉提问再发，旧的那条仍留在记录里。
  Claude Code 侧的判据是「两条提问 `parentUuid` 相同、中间没有任何回答」；Codex 没有分支树，
  判据是「上一条没等到回答，且是本条的前缀」。`--all-branches` 可以全留。
  （注意 `parentUuid` 的树结构本身不能用来判断：正常对话里多个提问也会挂在同一个 `system`
  节点下，按树剪枝会误删大量正常轮次。）
- **中途打断的轮次**。用户 Esc 打断后接着补充，那一轮确实没有回答，`-l` 会用 `*` 标出来。

## 时序：当前这一轮

某一轮的回答要等生成完才落盘，所以「刚刚这一轮」只有在它结束之后才导得出来。

更要紧的是：当你对 agent 说「把最近两轮记进 QA.md」，**你这句话本身已经是记录里的
一轮了**，只是还没有回答。如果不处理，`-n 2` 就会把这条请求本身算进去，错位一轮。

两边的 agent 都会把自己的会话 id 写进环境变量（`CLAUDE_CODE_SESSION_ID` /
`CODEX_SESSION_ID`），所以脚本能判断「我读的就是我正在跑的这个会话」，
此时 `-n` 自动跳过最后那一轮，并在 stderr 上说明。要包含它就加 `--include-pending`。

## 只读

脚本只读会话记录，唯一的写入是 `-o` 指定的文件，且是追加。

## License

MIT
