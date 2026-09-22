---
name: qa-export
description: 把会话里的问答原样导出成 Markdown。直接解析 Claude Code / Codex 落在本地的会话记录文件，不让模型复述。当用户说「把这几轮问答记进文档」「导出刚才的对话」「写进 QA 记录」「保存这段问答」时使用。
allowed-tools: Bash
---

# qa-export

问答全文本来就落在磁盘上，**不要自己复述重写**——既浪费上下文又会走样。
一律调用本目录下的 `scripts/qa_export.py`，路径相对于本 SKILL.md。

```bash
SCRIPT="<本 SKILL.md 所在目录>/scripts/qa_export.py"
```

## 怎么用

先列出轮次，让用户确认要哪几轮：

```bash
python3 "$SCRIPT" -l
```

然后导出（`-o` 是追加，文件不存在会自动建）：

```bash
python3 "$SCRIPT" -n 2 -o QA.md          # 最后 2 轮
python3 "$SCRIPT" -t 3,5-7 -o QA.md      # 第 3、5、6、7 轮
python3 "$SCRIPT" -t 4                   # 不给 -o 就打印到 stdout
```

用户没说存到哪里时，默认 `QA.md`；没说要哪几轮时，先 `-l` 再问，不要擅自猜。

## 其他选项

| 选项 | 作用 |
| --- | --- |
| `--sessions` | 列出当前目录下的所有会话（Claude Code 和 Codex 都扫） |
| `-s ID` | 指定会话：id 前缀，或 Claude Code 的会话名（会自动切到该会话的目录）；默认当前会话 |
| `-a claude\|codex` | 指定从哪个 agent 的记录里取，默认自动判断 |
| `-C DIR` | 指定项目目录，默认当前目录 |
| `--no-answer` | 只导出提问 |
| `--sidechain` | 连子 agent 的对话一起导出 |
| `--all-branches` | 保留被改写重发时废弃的旧提问 |

## 注意

- **时序**：某一轮的回答要等它生成完才落盘。所以「导出刚刚这一轮」必须在回答结束之后才跑，
  在本轮进行中跑 `-n 1` 拿到的是上一轮。`-l` 输出里带 `*` 的就是还没有回答的轮次。
- **最省 token 的用法**：让用户自己在输入框敲 `! python3 "$SCRIPT" -n 1 -o QA.md`
  （Claude Code）或直接在终端跑，完全不经过模型。可以主动告诉用户这一点。
- 脚本只读会话记录，唯一的写入是 `-o` 指定的那个文件，且是追加。
