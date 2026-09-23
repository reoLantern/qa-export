"""Claude Code 后端。

记录在 ~/.claude/projects/<cwd 转义>/<session-id>.jsonl，一行一条 JSON，
条目之间用 parentUuid 串成一棵树——rewind / fork 的废弃分支就是靠这棵树
认出来的。这是本工具验证最充分的一侧。
"""

import glob
import json
import os
import re

from .common import NOISE, finish, home, new_turn, read_jsonl

CLAUDE_SKIP_PREFIX = ("<command-name>", "<command-message>", "<local-command-stdout>",
                      "[Request interrupted by user")
# Codex 以 user 角色注入、但不是用户说的话：每轮的环境信封，和 AGENTS.md 正文


def _parent_of(entry, order):
    """上一跳。compact 边界的 parentUuid 是空的，改用 logicalParentUuid 跨过去；
    只认指向更早记录的桥，指向后面的桥会把父链绕成环。"""
    pu = entry.get("parentUuid")
    if pu:
        return pu
    lp = entry.get("logicalParentUuid")
    if lp and order.get(lp, 1 << 60) < order.get(entry.get("uuid"), -1):
        return lp
    return None


def dead_uuids(path):
    """扫一遍文件只取图结构（uuid / 父指针 / 类型），不留正文。

    整个文件读成对象列表会吃掉上百 MB——压缩过几十次的会话能到 500MB 以上，
    所以这一遍只保留判断分支所需的几个字段，正文留给第二遍流式处理。
    """
    nodes = []
    for d in read_jsonl(path):
        if d.get("type") == "last-prompt" and d.get("leafUuid"):
            nodes.append({"type": "last-prompt", "leafUuid": d["leafUuid"]})
            continue
        if not d.get("uuid"):
            continue
        nodes.append({
            "uuid": d["uuid"],
            "leafUuid": d.get("leafUuid"),
            "parentUuid": d.get("parentUuid"),
            "logicalParentUuid": d.get("logicalParentUuid"),
            "type": d.get("type"),
            "isSidechain": d.get("isSidechain"),
        })
    return abandoned_uuids(nodes)


def abandoned_uuids(entries):
    """找出被 rewind / fork 丢弃的分支。

    按 Esc 改写重发留下的是「同 parentUuid 且中间没有回答」的孪生记录，好认。
    但 rewind 丢弃的旧分支带着完整的回答，那个判据完全抓不到——所以改从结构入手：
    从活跃叶子沿父链回溯得到当前这条分支，凡是「不在链上、但往上能走到链上」的
    记录，就是从活跃分支分叉出去又被放弃的部分。

    走不回链上的记录一律保留。那通常意味着父链被截断（compact 桥的目标已经不在
    这个文件里），此时宁可多导出几轮，也不能让整段历史凭空消失。
    """
    order = {e["uuid"]: i for i, e in enumerate(entries) if e.get("uuid")}
    byu = {e["uuid"]: e for e in entries if e.get("uuid")}

    def walk(uuid):
        chain, cur = set(), byu.get(uuid)
        while cur is not None and cur.get("uuid") not in chain:
            chain.add(cur["uuid"])
            cur = byu.get(_parent_of(cur, order))
        return chain

    # 活跃叶子以 last-prompt 的 leafUuid 为准。不能直接取文件最后一条
    # user/assistant：偶尔会有很旧的游离记录被追加到末尾，拿它当叶子会
    # 把整条链压成几跳，然后把真正的对话全判成废弃。
    leaf = None
    for e in entries:
        if e.get("type") == "last-prompt" and e.get("leafUuid") in byu:
            leaf = e["leafUuid"]
    chain = walk(leaf) if leaf else set()

    # last-prompt 这个锚点的写入是滞后的（实测能差几十秒）。这期间用户刚发出的
    # 提问已经落盘、挂在锚点之后，若仍以锚点为头，它会被当成「不在链上但能走回
    # 链上」而误判为废弃分支剪掉。所以把头推进到锚点的最新后代。
    if chain:
        for e in reversed(entries[-200:]):
            if (not e.get("uuid") or e.get("isSidechain")
                    or e.get("type") not in ("user", "assistant")):
                continue
            if e["uuid"] in chain:
                break  # 最新的活跃记录就是锚点本身，无需推进
            c = walk(e["uuid"])
            if leaf in c:
                chain = c
                break

    if not chain:
        # 没有 leafUuid 可用时，在末尾若干候选里取链最长的那个，同样是为了
        # 躲开游离记录
        cands = [e["uuid"] for e in entries
                 if e.get("uuid") and e.get("type") in ("user", "assistant")
                 and not e.get("isSidechain")][-20:]
        for u in cands:
            c = walk(u)
            if len(c) > len(chain):
                chain = c
    if not chain:
        return set()

    status = {u: True for u in chain}

    def reaches(uuid):
        path, seen, u = [], set(), uuid
        while u is not None and u not in status and u not in seen:
            seen.add(u)
            path.append(u)
            e = byu.get(u)
            u = _parent_of(e, order) if e else None
        val = bool(status.get(u)) if u is not None else False
        for node in path:
            status[node] = val
        return val

    return {u for u in byu if u not in chain and reaches(u)}


class ClaudeBackend:
    name = "claude"
    label = "Claude Code"

    def __init__(self):
        self.root = os.path.join(home("CLAUDE_CONFIG_DIR", "~/.claude"), "projects")

    def project_dir(self, cwd):
        return os.path.join(self.root, re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(cwd)))

    def sessions(self, cwd):
        """返回 [(路径, mtime, 会话 id)]，按时间倒序。"""
        d = self.project_dir(cwd)
        out = []
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            out.append((p, os.path.getmtime(p), os.path.basename(p)[:-6]))
        return sorted(out, key=lambda x: -x[1])

    def current_id(self):
        return os.environ.get("CLAUDE_CODE_SESSION_ID")

    @staticmethod
    def _head_info(path, cap=5000):
        """扫文件开头，取出会话名和 cwd。两者都在前几十行里，命中即停。"""
        title = cwd = None
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if title is None and '"custom-title"' in line:
                    try:
                        title = json.loads(line).get("customTitle")
                    except json.JSONDecodeError:
                        pass
                if cwd is None and '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        pass
                if (title and cwd) or i >= cap:
                    break
        return title, cwd

    @staticmethod
    def _continued_in(path):
        """会话中途换文件时，旧文件末尾留一条 continued-in 指向后继。"""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if '"continued-in"' in line:
                try:
                    return json.loads(line).get("continuedInSessionId")
                except json.JSONDecodeError:
                    return None
        return None

    def follow(self, path, sid, depth=8):
        """跟着 continued-in 走到最后一个文件——但只在无损时才跟。

        一次会话中途换文件（换模型等）时，后继带着完整历史，不跟的话
        `-s <旧 id>` 拿到的是截断的视图。但 continued-in 也可能指向
        「压缩成摘要后新开的会话」，那种后继只有摘要和之后的几轮。

        判据就用最直接的那个：后继必须真的包含旧文件的最后一轮、且总轮数不减。
        不能只在后继里搜一段原文——compact 摘要会把旧对话抄进去，搜得到但
        轮次并不在。
        """
        seen = {sid}
        turns = self.parse(path)
        for _ in range(depth):
            nxt = self._continued_in(path)
            if not nxt or nxt in seen:
                break
            cand = os.path.join(os.path.dirname(path), nxt + ".jsonl")
            if not os.path.exists(cand):
                break
            try:
                cand_turns = self.parse(cand)
            except OSError:
                break
            if turns and (len(cand_turns) < len(turns)
                          or turns[-1]["q"] not in {t["q"] for t in cand_turns}):
                break  # 后继没带着这段历史，留在原文件
            seen.add(nxt)
            path, sid, turns = cand, nxt, cand_turns
        return path, sid

    def find_by_id(self, want):
        """按会话 id 前缀在所有项目目录里找，返回 (路径, 会话 id, cwd)。"""
        for p in sorted(glob.glob(os.path.join(self.root, "*", "*.jsonl")),
                        key=os.path.getmtime, reverse=True):
            sid = os.path.basename(p)[:-6]
            if sid.startswith(want):
                return p, sid, self._head_info(p)[1]
        return None

    def resolve_name(self, want):
        """按会话名找会话，返回 [(mtime, 会话 id, cwd)]，新的在前。

        名字有两处记录：`~/.claude/sessions/<pid>.json` 只在会话进程活着时存在，
        而 jsonl 里的 `custom-title` 条目是持久的，所以以后者为准。
        fork 之后会出现好几个同名会话，全部返回，由调用方决定取哪个。
        Codex 没有会话名这个概念，所以只有这个后端实现它。
        """
        exact, prefix = [], []
        for p in glob.glob(os.path.join(self.root, "*", "*.jsonl")):
            title, cwd = self._head_info(p)
            if not title:
                continue
            row = (os.path.getmtime(p), os.path.basename(p)[:-6], cwd)
            if title == want:
                exact.append(row)
            elif title.startswith(want):
                prefix.append(row)
        return sorted(exact or prefix, key=lambda r: -r[0])

    @staticmethod
    def _text(content):
        if isinstance(content, str):
            return content
        return "\n".join(
            b.get("text", "")
            for b in content or []
            if isinstance(b, dict) and b.get("type") == "text"
        )

    @staticmethod
    def _is_tool_result(content):
        return isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )

    def parse(self, path, all_branches=False):
        """默认只保留当前这条分支：rewind / fork 丢弃的旧分支整段剔除
        （见 abandoned_uuids），按 Esc 改写重发留下的孪生提问只保留后发的那条。
        `--all-branches` 可以全部保留。

        子 agent（sidechain）的对话一律不导出：本工具归档的是人与 agent 的
        问答，不是 agent 内部的协作过程。"""
        dead = set() if all_branches else dead_uuids(path)
        turns = []
        for d in read_jsonl(path):
            if d.get("uuid") in dead:
                continue
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                continue
            if d.get("isSidechain"):
                continue
            if d.get("isCompactSummary"):
                continue  # /compact 之后自动注入的摘要，不是用户说的话
            content = (d.get("message") or {}).get("content")
            if d.get("type") == "user":
                if self._is_tool_result(content):
                    continue
                txt = NOISE.sub("", self._text(content)).strip()
                if not txt or txt.startswith(CLAUDE_SKIP_PREFIX):
                    continue
                turn = new_turn(d.get("timestamp"), txt, d.get("parentUuid"))
                superseded = (
                    not all_branches
                    and turns
                    and not turns[-1]["a"]
                    and turns[-1]["parent"] is not None
                    and turns[-1]["parent"] == turn["parent"]
                )
                if superseded:
                    turns[-1] = turn
                else:
                    turns.append(turn)
            else:
                txt = self._text(content).strip()
                if txt and turns:
                    turns[-1]["a"].append(txt)
        return finish(turns)


# ---------------------------------------------------------------- Codex
