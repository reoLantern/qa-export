#!/usr/bin/env python3
"""qa-export — 把编码 agent 的问答，从本地会话记录里原样导出成 Markdown。

问答全文本来就落在磁盘上，不需要让模型复述一遍：

    Claude Code   ~/.claude/projects/<cwd 转义>/<session-id>.jsonl
    Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl

两种格式都由本脚本解析，默认自动判断当前用的是哪个 agent。
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

# ---------------------------------------------------------------- 通用工具

# 注入进上下文、但不是用户真正说的话
NOISE = re.compile(
    r"<system-reminder>.*?</system-reminder>|"
    r"<local-command-caveat>.*?</local-command-caveat>",
    re.S,
)
# 斜杠命令的回显和打断标记，不是提问
CLAUDE_SKIP_PREFIX = ("<command-name>", "<command-message>", "<local-command-stdout>",
                      "[Request interrupted by user")
# Codex 以 user 角色注入、但不是用户说的话：每轮的环境信封，和 AGENTS.md 正文
CODEX_ENVELOPE = re.compile(
    r"^<(environment_context|user_instructions|recommended_plugins|model_switch"
    r"|multi_agent\w*|collaboration_mode|turn_aborted|plan_mode)>"
    r"|^#\s*AGENTS\.md instructions\b",
    re.I,
)


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


def new_turn(ts, text, parent=None):
    return {"ts": ts or "", "q": text, "a": [], "parent": parent}


def finish(turns):
    for t in turns:
        t["a"] = "\n\n".join(t["a"]).strip()
    return turns


# ---------------------------------------------------------------- Claude Code


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


BACKENDS = {b.name: b for b in (ClaudeBackend(), CodexBackend())}


# ---------------------------------------------------------------- 选择会话


def detect_agent():
    """先看环境变量，再看谁在这个目录下有更新的会话。"""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    return None


def resolve(cwd, agent, want):
    """定位到一个具体会话，返回 (backend, path, session_id)。

    `want` 可以是会话 id 前缀，也可以是 Claude Code 的会话名；给的是会话名时，
    项目目录也跟着切到那个会话的 cwd，所以不必先 cd 过去。
    """
    if want:
        be = BACKENDS["claude"]
        named = be.resolve_name(want)
        if named:
            # fork 之后同名会话可能有好几个。自己就在其中之一时用自己的，
            # 否则不猜——列出候选让人用会话 id 指定。
            if len(named) > 1:
                # fork 出来的几个同名会话里，有些只是同一条血脉的前一段。
                # 各自跟到终点再按 id 去重，剩下的才是真正不同的对话。
                merged, seen_sid = [], set()
                for mtime, sid, scwd in named:
                    path = os.path.join(be.project_dir(scwd or ""), sid + ".jsonl")
                    if os.path.exists(path):
                        _, sid = be.follow(path, sid)
                    if sid not in seen_sid:
                        seen_sid.add(sid)
                        merged.append((mtime, sid, scwd))
                named = merged
            cur = be.current_id()
            pick = next((r for r in named if r[1] == cur), None)
            if pick is None and len(named) > 1:
                print(f"有 {len(named)} 个会话叫「{want}」，请改用会话 id 指定：",
                      file=sys.stderr)
                for mtime, sid, scwd in named:
                    stamp = datetime.datetime.fromtimestamp(
                        mtime).strftime("%Y-%m-%d %H:%M")
                    print(f"  {sid[:8]}  最后活动 {stamp}  {scwd}", file=sys.stderr)
                sys.exit(2)
            pick = pick or named[0]
            want, cwd = pick[1], pick[2] or cwd
    def done(be, path, sid):
        follow = getattr(be, "follow", None)
        if follow:
            newpath, newsid = follow(path, sid)
            if newpath != path:
                print(f"（会话 {sid[:8]} 中途换过文件，已跟到 {newsid[:8]}）",
                      file=sys.stderr)
            return be, newpath, newsid
        return be, path, sid

    order = [agent] if agent and agent != "auto" else [detect_agent() or "", "claude", "codex"]
    tried, best = [], None
    for name in order:
        be = BACKENDS.get(name)
        if not be or be in tried:
            continue
        tried.append(be)
        found = be.sessions(cwd)
        if not found:
            continue
        if want:
            hit = [s for s in found if want in s[2] or want in os.path.basename(s[0])]
            if hit:
                return done(be, hit[0][0], hit[0][2])
            continue
        # 自己这个会话的记录优先，别被同目录下更活跃的别的会话抢走
        cur = be.current_id()
        if cur:
            hit = [s for s in found if s[2] == cur]
            if hit:
                return done(be, hit[0][0], hit[0][2])
        if agent and agent != "auto":
            return done(be, found[0][0], found[0][2])
        if best is None or found[0][1] > best[0][1]:
            best = (found[0], be)
    if best:
        (path, _, sid), be = best
        return done(be, path, sid)
    if want:
        # 当前目录下没有，可能是别的项目的会话：按 id 前缀全盘兜底
        for be in tried or [BACKENDS["claude"], BACKENDS["codex"]]:
            hit = be.find_by_id(want)
            if hit:
                return done(be, hit[0], hit[1])
        sys.exit(f"没有找到匹配 {want!r} 的会话")
    sys.exit(f"{cwd} 下没有找到任何 Claude Code / Codex 会话记录")


# ---------------------------------------------------------------- 输出


def parse_sel(spec):
    idx = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if re.match(r"^\d+\s*-\s*\d+$", part):
            a, b = re.split(r"\s*-\s*", part)
            idx.extend(range(int(a), int(b) + 1))
        else:
            idx.append(int(part))
    return idx


def find(turns, text, in_answers=False):
    """返回所有提到 text 的轮次序号（1 起）。大小写不敏感的子串匹配，不是正则。"""
    needle = text.lower()
    hits = []
    for i, t in enumerate(turns, 1):
        hay = t["q"] + ("\n" + t["a"] if in_answers else "")
        if needle in hay.lower():
            hits.append(i)
    return hits


def pick_one(turns, text, in_answers, which, label):
    """定位唯一的一轮；命中多轮时不猜，把候选列出来让人挑。"""
    hits = find(turns, text, in_answers)
    if not hits:
        extra = "" if in_answers else "（回答里可能有，试试 --match-answers）"
        sys.exit(f"{label} 没有匹配「{text}」的轮次{extra}")
    if len(hits) > 1:
        print(f"{label}「{text}」命中 {len(hits)} 轮，请换更精确的说法或改用 -t：",
              file=sys.stderr)
        for i in hits:
            print(f"  {i:3d}  {fmt_ts(turns[i - 1]['ts'])}  "
                  f"{preview(turns[i - 1]['q'], 60)}", file=sys.stderr)
        sys.exit(2)
    return hits[which]


def render(turns, sel, sid, label, with_answer=True):
    chunks = []
    for i in sel:
        t = turns[i - 1]
        body = [f"## {fmt_ts(t['ts'])} · {preview(t['q'], 60)}", "",
                "**Q**", "", t["q"]]
        if with_answer and t["a"]:
            body += ["", "**A**", "", t["a"]]
        body += ["", f"<!-- {label} session {str(sid)[:8]} turn {i} -->"]
        chunks.append("\n".join(body))
    return "\n\n".join(chunks) + "\n"


def main():
    ap = argparse.ArgumentParser(
        prog="qa-export",
        description="把 Claude Code / Codex 的问答从本地会话记录原样导出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  qa-export -l                    列出当前目录最近会话的所有轮次
  qa-export -n 2 -o QA.md         把最后 2 轮追加进 QA.md
  qa-export -t 3,5-7 -o QA.md     导出第 3、5、6、7 轮
  qa-export --sessions            列出本目录下所有会话（两个 agent 都看）
  qa-export -s a1b2c3d4 -l        指定会话（id 前缀）
""",
    )
    ap.add_argument("-l", "--list", action="store_true", help="列出轮次")
    ap.add_argument("--sessions", action="store_true", help="列出本目录下的会话")
    ap.add_argument("-n", "--last", type=int, metavar="N", help="导出最后 N 轮")
    ap.add_argument("-t", "--turns", metavar="SPEC", help="导出指定轮次，如 3,5-7")
    ap.add_argument("-o", "--out", metavar="FILE", help="追加写入的文件；不给则打印到 stdout")
    ap.add_argument("-s", "--session", metavar="ID", help="会话 id 前缀或 Claude Code 会话名，默认当前会话")
    ap.add_argument("-C", "--cwd", default=os.getcwd(), metavar="DIR", help="项目目录，默认当前目录")
    ap.add_argument("-a", "--agent", choices=["auto", "claude", "codex"], default="auto",
                    help="从哪个 agent 的记录里取，默认自动判断")
    ap.add_argument("--all-branches", action="store_true", help="保留被改写重发废弃的旧提问")
    ap.add_argument("--from", dest="from_", metavar="TEXT",
                    help="从提到 TEXT 的那一轮开始，一直到最后（或 --to 指定的那轮）")
    ap.add_argument("--to", dest="to_", metavar="TEXT",
                    help="到提到 TEXT 的那一轮为止，需配合 --from")
    ap.add_argument("-g", "--grep", metavar="TEXT",
                    help="只列出提到 TEXT 的轮次，用来定位序号")
    ap.add_argument("--match-answers", action="store_true",
                    help="--from/--to/--grep 也在回答里找，默认只在提问里找")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告会导出哪几轮，不写文件（确认用，输出很短）")
    ap.add_argument("--include-pending", action="store_true",
                    help="不排除当前正在进行的那一轮")
    ap.add_argument("--tail", type=int, default=20, metavar="N",
                    help="-l 只显示最近 N 轮，0 表示全部（默认 20）")
    ap.add_argument("--no-answer", action="store_true", help="只导出提问")
    args = ap.parse_args()

    if args.sessions:
        names = [args.agent] if args.agent != "auto" else ["claude", "codex"]
        found = False
        for name in names:
            be = BACKENDS[name]
            rows = be.sessions(args.cwd)
            if not rows:
                continue
            found = True
            print(f"\n== {be.label} ==")
            for path, mtime, sid in rows[:20]:
                n = len(be.parse(path, args.all_branches))
                stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  {str(sid)[:8]}  {stamp}  {n:3d} 轮  {os.path.basename(path)}")
        if not found:
            sys.exit(f"{args.cwd} 下没有会话记录")
        return

    be, path, sid = resolve(args.cwd, args.agent, args.session)
    turns = be.parse(path, args.all_branches)
    if not turns:
        sys.exit(f"{path} 里没有解析出问答")

    if args.list or (args.last is None and not args.turns and not args.from_
                     and not args.grep):
        n = len(turns)
        start = max(0, n - args.tail) if args.tail > 0 else 0
        print(f"# [{be.label}] {path}  （共 {n} 轮）", file=sys.stderr)
        if start:
            print(f"# 只显示最近 {args.tail} 轮，--tail 0 看全部", file=sys.stderr)
        print(file=sys.stderr)
        for i, t in enumerate(turns[start:], start + 1):
            mark = " " if t["a"] else "*"
            print(f"{i:3d}{mark} {fmt_ts(t['ts']):16s}  {preview(t['q'])}")
        if any(not t["a"] for t in turns[start:]):
            print("\n* = 该轮还没有回答（多半是当前正在进行的这一轮）", file=sys.stderr)
        return

    n = len(turns)

    if args.grep:
        hits = find(turns, args.grep, args.match_answers)
        if not hits:
            sys.exit(f"没有匹配「{args.grep}」的轮次")
        print(f"共 {n} 轮，其中 {len(hits)} 轮提到「{args.grep}」：", file=sys.stderr)
        for i in hits:
            mark = " " if turns[i - 1]["a"] else "*"
            print(f"{i:3d}{mark} {fmt_ts(turns[i - 1]['ts'])}  "
                  f"{preview(turns[i - 1]['q'], 60)}")
        return

    # 末尾要排除的轮次：进行中的这一轮，以及被打断、没有回答的那些
    end_default, pending_note = n, None
    if not args.include_pending:
        if be.current_id() and be.current_id() == sid and end_default > 0:
            end_default -= 1
            pending_note = (f"（已排除进行中的第 {n} 轮；"
                            f"要包含它加 --include-pending）")
        while end_default > 0 and not turns[end_default - 1]["a"]:
            end_default -= 1

    if args.from_:
        start = pick_one(turns, args.from_, args.match_answers, 0, "--from")
        stop = (pick_one(turns, args.to_, args.match_answers, -1, "--to")
                if args.to_ else end_default)
        if stop < start:
            sys.exit(f"--to 命中的第 {stop} 轮在 --from 的第 {start} 轮之前")
        sel = list(range(start, stop + 1))
    elif args.turns:
        raw = parse_sel(args.turns)
        sel = [i for i in raw if 1 <= i <= n]
        bad = [i for i in raw if i not in sel]
        if bad:
            print(f"警告: 轮次 {bad} 超出范围（共 {n} 轮），已跳过", file=sys.stderr)
    else:
        if end_default == 0:
            sys.exit("还没有已完成的轮次可以导出")
        sel = [i for i in range(end_default - args.last + 1, end_default + 1) if i >= 1]
    if not sel:
        sys.exit("没有选中任何轮次")
    if pending_note and sel[-1] == end_default:
        print(pending_note, file=sys.stderr)

    def summary():
        return "\n".join(
            f"  {i:3d}  {fmt_ts(turns[i - 1]['ts'])}  {preview(turns[i - 1]['q'], 60)}"
            for i in sel
        )

    if args.dry_run:
        print(f"将导出 {len(sel)} 轮（共 {n} 轮）到 {args.out or 'stdout'}：", file=sys.stderr)
        print(summary())
        return

    text = render(turns, sel, sid, be.label, not args.no_answer)
    if args.out:
        fresh = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
        with open(args.out, "a", encoding="utf-8") as fh:
            if fresh:
                fh.write("# QA 记录\n\n")
            fh.write(text)
        print(f"已把 {len(sel)} 轮追加到 {args.out}：", file=sys.stderr)
        print(summary(), file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
