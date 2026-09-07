#!/usr/bin/env python3
"""
Session Find — browse and search EVERY Claude Code session, across all folders.

Claude Code's own resume picker is scoped to the current directory's project,
and `--continue` is explicitly "the most recent conversation in the current
directory". So sessions you started in other folders are invisible unless you
cd back to exactly the right place. This lists all of them, from anywhere, and
prints the command to resume each one.

It reads three sources and merges them, so a session shows up even if its live
transcript has already been deleted by Claude Code's retention sweep:

  ~/.claude/history.jsonl              every prompt ever typed
  ~/.claude/projects/**/*.jsonl        live transcripts
  ~/.claude/session-archive/           the permanent mirror

Usage:
    session_find.py                       20 most recent sessions
    session_find.py -n 50                 50 most recent
    session_find.py <text>                sessions whose prompts match <text>
    session_find.py --project <substr>    restrict to a project path
    session_find.py --since 2026-08-01    on/after a date
    session_find.py --all                 no limit
    session_find.py --resumable           only ones with a live transcript

Add --full to print every matching prompt instead of just the first.
No external deps -- stdlib only.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Prompt text is arbitrary Unicode. The Windows console defaults to cp1252 and
# raises UnicodeEncodeError on the first box-drawing or emoji character, which
# kills the listing part-way through. Force UTF-8 and never fail on output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CLAUDE_DIR = Path.home() / ".claude"
HISTORY = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ARCHIVE_DIR = CLAUDE_DIR / "session-archive" / "transcripts"


def load_history():
    """sessionId -> {project, first_ts, last_ts, prompts[]}"""
    out = {}
    if not HISTORY.exists():
        return out
    with open(HISTORY, "r", encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            sid = e.get("sessionId") or ""
            if not sid:
                continue
            d = e.get("display") or ""
            ts = e.get("timestamp") or 0
            rec = out.setdefault(sid, {"project": e.get("project") or "",
                                       "first": ts, "last": ts, "prompts": []})
            if not rec["project"]:
                rec["project"] = e.get("project") or ""
            if ts:
                rec["first"] = min(rec["first"] or ts, ts)
                rec["last"] = max(rec["last"] or ts, ts)
            if d:
                rec["prompts"].append((ts, d))
    return out


def live_sessions():
    out = {}
    if PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.glob("*/*.jsonl"):
            if p.is_file():
                out[p.stem] = p
    return out


def archived_sessions():
    out = set()
    if ARCHIVE_DIR.exists():
        for p in ARCHIVE_DIR.glob("*/*.jsonl"):
            if p.is_file():
                out.add(p.stem)
    return out


def fmt_ts(ms):
    if not ms:
        return "?"
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def main() -> int:
    args = sys.argv[1:]
    limit = 20
    query = []
    project = None
    since = None
    show_all = "--all" in args
    full = "--full" in args
    resumable_only = "--resumable" in args

    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2; continue
        if a == "--project" and i + 1 < len(args):
            project = args[i + 1].lower(); i += 2; continue
        if a == "--since" and i + 1 < len(args):
            since = args[i + 1]; i += 2; continue
        if a in ("--all", "--full", "--resumable"):
            i += 1; continue
        if a in ("-h", "--help"):
            print(__doc__); return 0
        query.append(a); i += 1

    hist = load_history()
    live = live_sessions()
    arch = archived_sessions()
    q = " ".join(query).lower()

    rows = []
    for sid, rec in hist.items():
        if project and project not in (rec["project"] or "").lower():
            continue
        if since and fmt_ts(rec["last"])[:10] < since:
            continue
        matches = [d for _, d in rec["prompts"] if not q or q in d.lower()]
        if q and not matches:
            continue
        state = ("live" if sid in live else
                 "archived" if sid in arch else "gone")
        if resumable_only and state != "live":
            continue
        rows.append((rec["last"], sid, rec["project"], state,
                     len(rec["prompts"]), matches))

    rows.sort(key=lambda r: r[0] or 0, reverse=True)
    total = len(rows)
    if not show_all:
        rows = rows[:limit]

    if not rows:
        print("no sessions matched")
        return 0

    for last, sid, proj, state, nprompts, matches in rows:
        mark = {"live": "*", "archived": "~", "gone": "!"}[state]
        print(f"\n{mark} {fmt_ts(last)}  {sid}  [{state}, {nprompts} prompts]")
        print(f"    {proj or '(unknown project)'}")
        shown = matches if full else matches[:1]
        for d in shown:
            one = " ".join(d.split())
            print(f"    > {one[:150]}")
        if not full and len(matches) > 1:
            print(f"    ... {len(matches)-1} more matching prompts (--full)")
        if state == "live":
            print(f"    resume: claude --resume {sid}")
        elif state == "archived":
            print(f"    restore first: session_archive.py restore")

    print(f"\n{len(rows)} of {total} matching sessions"
          f"   ( * live   ~ archived only   ! not found )")
    if total > len(rows):
        print("use --all or -n N to see more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
