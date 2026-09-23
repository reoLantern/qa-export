"""两个后端共用的解析原语。

这里只放「怎么读一行记录、怎么攒一轮问答」这种与 agent 无关的东西。
凡是某个 agent 特有的字段、过滤规则、分支结构，都归各自的后端模块。
"""

import datetime
import json
import os
import re

NOISE = re.compile(
    r"<system-reminder>.*?</system-reminder>|"
    r"<local-command-caveat>.*?</local-command-caveat>",
    re.S,
)
# 斜杠命令的回显和打断标记，不是提问


def home(var, default):
    return os.path.expanduser(os.environ.get(var) or default)


def fmt_ts(ts):
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def read_jsonl(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):  # 合法 JSON 也可能是数组或 null
                yield obj


def preview(text, width=70):
    """把提问压成一行摘要。取首行常常只拿到「我有个问题：」这种开场，
    所以压平换行再截断，让人和 agent 都能认出这是哪一轮。"""
    return re.sub(r"\s+", " ", text).strip()[:width]


def new_turn(ts, text, parent=None):
    return {"ts": ts or "", "q": text, "a": [], "parent": parent}


def finish(turns):
    for t in turns:
        t["a"] = "\n\n".join(t["a"]).strip()
    return turns


# ---------------------------------------------------------------- Claude Code
