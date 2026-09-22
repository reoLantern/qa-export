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
    r"|multi_agent\w*|collaboration_mode|turn_aborted|plan_mode"
    r"|\w*_context|\w*_instructions)>"
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
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


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

    def resolve_name(self, want):
        """会话名记在 ~/.claude/sessions/<pid>.json 里，返回 (会话 id, cwd)。

        Codex 没有这个概念，所以只有这个后端实现它。
        """
        root = os.path.join(home("CLAUDE_CONFIG_DIR", "~/.claude"), "sessions")
        exact, prefix = [], []
        for p in glob.glob(os.path.join(root, "*.json")):
            try:
                with open(p, encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            name, sid = d.get("name"), d.get("sessionId")
            if not name or not sid:
                continue
            row = (d.get("startedAt") or 0, sid, d.get("cwd"))
            if name == want:
                exact.append(row)
            elif name.startswith(want):
                prefix.append(row)
        hits = exact or prefix
        if not hits:
            return None
        _, sid, cwd = max(hits, key=lambda r: r[0])
        return sid, cwd

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

    def parse(self, path, keep_sidechain=False, all_branches=False):
        """按 Esc 改写重发的提问，jsonl 里留下两条 parentUuid 相同、中间没有
        任何回答的记录，默认只保留后发的那条。"""
        turns = []
        for d in read_jsonl(path):
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                continue
            if d.get("isSidechain") and not keep_sidechain:
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
    def _meta(path):
        for d in read_jsonl(path):
            if d.get("type") == "session_meta":
                return d.get("payload") or {}
            return None
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

    def parse(self, path, keep_sidechain=False, all_branches=False):
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
        named = BACKENDS["claude"].resolve_name(want)
        if named:
            want, cwd = named[0], named[1] or cwd
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
                return be, hit[0][0], hit[0][2]
            continue
        if agent and agent != "auto":
            return be, found[0][0], found[0][2]
        cur = be.current_id()
        if cur:
            hit = [s for s in found if s[2] == cur]
            if hit:
                return be, hit[0][0], hit[0][2]
        if best is None or found[0][1] > best[0][1]:
            best = (found[0], be)
    if best:
        (path, _, sid), be = best
        return be, path, sid
    if want:
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


def render(turns, sel, sid, label, with_answer=True):
    chunks = []
    for i in sel:
        t = turns[i - 1]
        first = t["q"].splitlines()[0]
        body = [f"## {fmt_ts(t['ts'])} · {first[:60]}", "", "**Q**", "", t["q"]]
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
    ap.add_argument("--sidechain", action="store_true", help="包含子 agent 的对话")
    ap.add_argument("--all-branches", action="store_true", help="保留被改写重发废弃的旧提问")
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
                n = len(be.parse(path, args.sidechain, args.all_branches))
                stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  {str(sid)[:8]}  {stamp}  {n:3d} 轮  {os.path.basename(path)}")
        if not found:
            sys.exit(f"{args.cwd} 下没有会话记录")
        return

    be, path, sid = resolve(args.cwd, args.agent, args.session)
    turns = be.parse(path, args.sidechain, args.all_branches)
    if not turns:
        sys.exit(f"{path} 里没有解析出问答")

    if args.list or (args.last is None and not args.turns):
        n = len(turns)
        start = max(0, n - args.tail) if args.tail > 0 else 0
        print(f"# [{be.label}] {path}  （共 {n} 轮）", file=sys.stderr)
        if start:
            print(f"# 只显示最近 {args.tail} 轮，--tail 0 看全部", file=sys.stderr)
        print(file=sys.stderr)
        for i, t in enumerate(turns[start:], start + 1):
            first = t["q"].splitlines()[0] if t["q"] else ""
            mark = " " if t["a"] else "*"
            print(f"{i:3d}{mark} {fmt_ts(t['ts']):16s}  {first[:70]}")
        if any(not t["a"] for t in turns[start:]):
            print("\n* = 该轮还没有回答（多半是当前正在进行的这一轮）", file=sys.stderr)
        return

    n = len(turns)
    if args.turns:
        raw = parse_sel(args.turns)
        sel = [i for i in raw if 1 <= i <= n]
        bad = [i for i in raw if i not in sel]
        if bad:
            print(f"警告: 轮次 {bad} 超出范围（共 {n} 轮），已跳过", file=sys.stderr)
    else:
        # 正在进行的这一轮已经在记录里了，但还没有回答。默认从最后一个
        # 「已完成」的轮次往回数，否则「导出最后 2 轮」会把用户刚发出的
        # 那条「帮我导出」本身算进去。
        end = n
        if not args.include_pending:
            # agent 自己调用本脚本时，用户刚发出的那条请求（「帮我导出最近两轮」）
            # 已经在记录里了，但这一轮还没结束。两端都能从环境变量拿到自己的
            # 会话 id，所以「读的就是自己这个会话」时，最后一轮必然是进行中的。
            if be.current_id() and be.current_id() == sid and end > 0:
                end -= 1
                print(f"（已排除进行中的第 {n} 轮；要包含它加 --include-pending）",
                      file=sys.stderr)
            # 再往前跳过没有回答的轮次（被打断的那些，没东西可导）
            while end > 0 and not turns[end - 1]["a"]:
                end -= 1
            if end == 0:
                sys.exit("还没有已完成的轮次可以导出")
        sel = [i for i in range(end - args.last + 1, end + 1) if i >= 1]
    if not sel:
        sys.exit("没有选中任何轮次")

    def summary():
        return "\n".join(
            f"  {i:3d}  {fmt_ts(turns[i - 1]['ts'])}  {turns[i - 1]['q'].splitlines()[0][:60]}"
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
