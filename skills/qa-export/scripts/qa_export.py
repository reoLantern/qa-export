#!/usr/bin/env python3
"""qa-export — 把编码 agent 的问答，从本地会话记录里原样导出成 Markdown。

问答全文本来就落在磁盘上，不需要让模型复述一遍：

    Claude Code   ~/.claude/projects/<cwd 转义>/<session-id>.jsonl
    Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl

两种格式都由本脚本解析，默认自动判断当前用的是哪个 agent。
"""


import argparse
import datetime
import os
import re
import sys

from backends import BACKENDS
from backends.common import fmt_ts, preview


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
        # 轮次标题用一级：回答正文里大量使用 ## 小标题，同级会让轮次边界
        # 在大纲里淹没掉。轮次之间再插一条 --- 分隔线。
        body = [f"# {fmt_ts(t['ts'])} · {preview(t['q'], 60)}", "",
                "**Q**", "", t["q"]]
        if with_answer and t["a"]:
            body += ["", "**A**", "", t["a"]]
        body += ["", f"<!-- {label} session {str(sid)[:8]} turn {i} -->"]
        chunks.append("\n".join(body))
    return "\n\n---\n\n".join(chunks) + "\n"


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
            if not fresh:
                fh.write("\n---\n\n")  # 接着已有内容写时也要隔开
            fh.write(text)
        print(f"已把 {len(sel)} 轮追加到 {args.out}：", file=sys.stderr)
        print(summary(), file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
