"""Codex 后端。

记录在 ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl，纯线性追加，
没有任何父子指针，所以认不出 backtrack 改写和 fork 的来源。已知局限见
references/codex.md。
"""

import glob
import json
import os
import re

from .common import NOISE, finish, home, new_turn, read_jsonl

CODEX_ENVELOPE = re.compile(
    r"^<(environment_context|user_instructions|recommended_plugins|model_switch"
    r"|multi_agent\w*|collaboration_mode|turn_aborted|plan_mode)>"
    r"|^#\s*AGENTS\.md instructions\b",
    re.I,
)


class CodexBackend:
    name = "codex"
    label = "Codex"

    def __init__(self):
        self.root = os.path.join(home("CODEX_HOME", "~/.codex"), "sessions")

    def sessions(self, cwd):
        """Codex 的会话不按项目分目录，得读每个 rollout 的 session_meta 比对 cwd。"""
        want = os.path.abspath(cwd)
        files = glob.glob(os.path.join(self.root, "**", "rollout-*.jsonl"), recursive=True)
        out = []
        for p in sorted(files, key=os.path.getmtime, reverse=True):
            meta = self._meta(p)
            if not meta:
                continue
            if os.path.abspath(meta.get("cwd") or "") != want:
                continue
            sid = meta.get("session_id") or meta.get("id") or os.path.basename(p)
            out.append((p, os.path.getmtime(p), sid))
        return out

    @staticmethod
    def _meta(path, scan=5):
        """session_meta 实测总在首条，但不值得赌——往后多看几条，
        赌输的代价是整个会话找不到。"""
        for i, d in enumerate(read_jsonl(path)):
            if d.get("type") == "session_meta":
                return d.get("payload") or {}
            if i + 1 >= scan:
                break
        return None

    def find_by_id(self, want):
        """按会话 id 前缀在所有 rollout 里找，不限 cwd。"""
        for p in sorted(glob.glob(os.path.join(self.root, "**", "rollout-*.jsonl"),
                                  recursive=True),
                        key=os.path.getmtime, reverse=True):
            meta = self._meta(p)
            sid = (meta or {}).get("session_id") or (meta or {}).get("id") or ""
            if sid.startswith(want) or want in os.path.basename(p):
                return p, sid or os.path.basename(p), (meta or {}).get("cwd")
        return None

    def current_id(self):
        return os.environ.get("CODEX_SESSION_ID")

    @staticmethod
    def _text(content, kinds):
        return "\n".join(
            b.get("text", "")
            for b in content or []
            if isinstance(b, dict) and b.get("type") in kinds
        )

    def parse(self, path, all_branches=False):
        """Codex 没有分支树；重发/改写表现为「上一条没等到回答，且是本条的前缀」。"""
        turns = []
        for d in read_jsonl(path):
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            if p.get("type") != "message":
                continue
            role = p.get("role")
            if role == "user":
                txt = NOISE.sub("", self._text(p.get("content"), ("input_text", "text"))).strip()
                if not txt or CODEX_ENVELOPE.match(txt):
                    continue
                turn = new_turn(d.get("timestamp"), txt)
                superseded = (
                    not all_branches
                    and turns
                    and not turns[-1]["a"]
                    and txt.startswith(turns[-1]["q"])
                )
                if superseded:
                    turns[-1] = turn
                else:
                    turns.append(turn)
            elif role == "assistant":
                txt = self._text(p.get("content"), ("output_text", "text")).strip()
                if txt and turns:
                    turns[-1]["a"].append(txt)
        return finish(turns)
