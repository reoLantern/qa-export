"""后端注册表。加一个 agent 就是加一个模块 + 在这里登记。

后端只负责两件事：在磁盘上找到会话文件，以及把文件解析成 [{ts, q, a}]。
轮次怎么选、怎么渲染、怎么写出去，全在入口脚本里，与 agent 无关。
"""

from .claude import ClaudeBackend
from .codex import CodexBackend

BACKENDS = {b.name: b for b in (ClaudeBackend(), CodexBackend())}

__all__ = ["BACKENDS", "ClaudeBackend", "CodexBackend"]
