#!/usr/bin/env python3
"""
API Repair — fixes transcripts that are valid JSON but that the API rejects,
so an old session can be resumed instead of dying on `claude --resume`.

This is a DIFFERENT class of damage from jsonl_repair. jsonl_repair fixes lines
that are malformed JSON. The lines this tool fixes parse perfectly; they are
semantically invalid to the Messages API, so the failure only appears when the
transcript is replayed:

    API Error: 400 messages.N.content.0.thinking:
    each thinking block must contain non-whitespace thinking

Cause: Claude Code writes one assistant API response as SEVERAL transcript
lines -- one content block per line -- all sharing the same `message.id`, and
merges them back into one message on replay. When a model emits a thinking
block whose text is empty or whitespace (common with third-party models routed
through a proxy, but it happens with Claude models too), the merged message
carries an empty thinking block and the API refuses the whole conversation.

Repair, in order of preference:
  1. The line has other content blocks -> drop only the empty thinking block.
  2. The empty block is the line's only block, but other lines share its
     message.id -> drop the line. Nothing is lost: the sibling lines carry the
     same `usage` object, and the message's other content blocks live on them.
  3. The empty block is the only block AND the only line of its message ->
     drop the line as well. Such a message has no usable content at all; it is
     archived first so its token usage stays recoverable.

Whenever a line is dropped, any line whose `parentUuid` pointed at it is
re-linked to the dropped line's own parent, so the conversation chain stays
connected.

SAFETY -- identical invariants to the rest of Session Guard:
  * Nothing is ever deleted without first being copied, in full, to the
    permanent append-only archive; the rewrite is abandoned if that fails.
  * A full backup of the file is always taken first.
  * Writes are atomic (temp + fsync + os.replace).
  * The file is re-stat'd immediately before the write; if it changed while
    being scanned, the rewrite is ABORTED rather than clobbering a live append.
  * The current session and recently-written transcripts are skipped by
    default, because rewriting a file Claude Code is appending to truncates it.
  * Read and write use errors='surrogateescape', so bytes round-trip losslessly.

Commands:
    scan                 report what is broken; never writes (default)
    from-hook            repair the transcript named on stdin by a SessionEnd
                         hook payload -- the safest moment, since the session
                         has just terminated
    fix --all            repair every eligible transcript
    fix <session-id>     repair one session by id (allowed even if recent,
                         since you are asking for it explicitly -- but never
                         the session that is running right now)
    fix <path.jsonl>     repair one file by path

No external deps -- stdlib only.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
BACKUPS_DIR = CLAUDE_DIR / "backups" / "sessions"
REMOVED_LINES_ARCHIVE = BACKUPS_DIR / "removed-lines-archive.jsonl"
REPAIR_LOG = CLAUDE_DIR / "session-tools" / "logs" / "api_repair.log"
ACTIVE_SKIP_SECONDS = 3600


def is_empty_thinking(block) -> bool:
    if not isinstance(block, dict) or block.get("type") != "thinking":
        return False
    t = block.get("thinking")
    return t is None or (isinstance(t, str) and not t.strip())


def read_lines(path: Path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return [ln.rstrip("\n") for ln in f]


def archive_dropped(path: Path, dropped):
    """Append every removed line/block, in full, to the permanent archive."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat()
    with open(REMOVED_LINES_ARCHIVE, "a", encoding="utf-8") as f:
        for lineno, reason, raw in dropped:
            f.write(json.dumps({
                "archived_at": stamp,
                "source_file": str(path),
                "source_line": lineno,
                "reason": reason,
                "content": raw,
            }, ensure_ascii=False) + "\n")


def make_backup(path: Path) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS_DIR / f"{path.stem}.apibackup.{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    shutil.copy2(path, dest)
    return dest


def analyse(path: Path):
    """Return (stats, new_lines, dropped) without touching disk."""
    stats = {"file": str(path), "blocks_removed": 0, "lines_removed": 0,
             "relinked": 0, "unfixable": 0}
    raw = read_lines(path)
    objs, order = [], []
    for ln in raw:
        s = ln.strip()
        if not s:
            objs.append(None); order.append(ln); continue
        try:
            objs.append(json.loads(s))
        except (json.JSONDecodeError, ValueError):
            objs.append(None)
        order.append(ln)

    # how many lines carry each message.id
    per_msg = {}
    for o in objs:
        if isinstance(o, dict) and isinstance(o.get("message"), dict):
            mid = o["message"].get("id")
            if mid:
                per_msg[mid] = per_msg.get(mid, 0) + 1

    dropped = []
    drop_idx = set()
    remap = {}          # uuid of dropped line -> its parentUuid
    changed = {}        # index -> new json text

    for i, o in enumerate(objs):
        if not isinstance(o, dict):
            continue
        m = o.get("message")
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if not isinstance(c, list):
            continue
        empties = [b for b in c if is_empty_thinking(b)]
        if not empties:
            continue
        keep = [b for b in c if not is_empty_thinking(b)]

        if keep:
            # Case 1: other blocks survive -> drop only the offending block.
            m["content"] = keep
            changed[i] = json.dumps(o, ensure_ascii=False)
            stats["blocks_removed"] += len(empties)
            for b in empties:
                dropped.append((i + 1, "empty thinking block removed from line",
                                json.dumps(b, ensure_ascii=False)))
        else:
            # Cases 2 and 3: the line carries nothing else -> drop the line.
            mid = m.get("id")
            siblings = per_msg.get(mid, 0) if mid else 0
            reason = ("empty-thinking-only line (message has other lines)"
                      if siblings > 1 else
                      "empty-thinking-only line (sole line of its message)")
            if siblings <= 1:
                stats["unfixable"] += 1   # counted, but still repaired
            drop_idx.add(i)
            dropped.append((i + 1, reason, order[i]))
            stats["lines_removed"] += 1
            u, p = o.get("uuid"), o.get("parentUuid")
            if u:
                remap[u] = p

    if not changed and not drop_idx:
        return stats, None, []

    # Re-link: a child of a dropped line adopts that line's parent. Follow
    # chains, so consecutive dropped lines collapse correctly.
    def resolve(uid):
        seen = set()
        while uid in remap and uid not in seen:
            seen.add(uid)
            uid = remap[uid]
        return uid

    new_lines = []
    for i, o in enumerate(objs):
        if i in drop_idx:
            continue
        if isinstance(o, dict) and o.get("parentUuid") in remap:
            o["parentUuid"] = resolve(o["parentUuid"])
            changed[i] = json.dumps(o, ensure_ascii=False)
            stats["relinked"] += 1
        new_lines.append(changed.get(i, order[i]))

    return stats, new_lines, dropped


def repair_file(path: Path, dry_run: bool = True, force: bool = False) -> dict:
    try:
        st_before = path.stat()
    except OSError as e:
        return {"file": str(path), "error": f"stat failed: {e}"}

    # Liveness guards exist to prevent a WRITE from truncating a file Claude
    # Code is appending to. A scan never writes, so applying them there just
    # blinds the report -- notably right after a repair, when every file it
    # touched looks "recent".
    if dry_run:
        pass
    elif force == "session_end":
        # SessionEnd fired for this exact transcript: the session has
        # terminated, so "recently written" is expected and is NOT evidence of
        # liveness. The TOCTOU check below still protects us -- if anything
        # appends between the read and the write, the rewrite is abandoned.
        pass
    elif not force:
        active = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if active and path.stem == active:
            return {"file": str(path), "skipped": "active session"}
        if time.time() - st_before.st_mtime < ACTIVE_SKIP_SECONDS:
            return {"file": str(path), "skipped": "modified recently (possibly live)"}
    else:
        # Even with an explicit target we never rewrite a session that is
        # running right now. CLAUDE_SESSION_ID is not always set in the
        # environment, so it cannot be the only check: fall back to recency,
        # since a live session appends constantly.
        active = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if active and path.stem == active:
            return {"file": str(path), "skipped": "refusing: this is the running session"}
        if time.time() - st_before.st_mtime < 120:
            return {"file": str(path),
                    "skipped": "refusing: written to in the last 2 minutes "
                               "(looks live) -- close that session first"}

    try:
        stats, new_lines, dropped = analyse(path)
    except Exception as e:
        return {"file": str(path), "error": f"analyse failed: {e}"}

    if new_lines is None:
        return stats                      # nothing to do
    if dry_run:
        stats["dry_run"] = True
        return stats

    # Archive before removing anything; abandon the rewrite if archiving fails.
    if dropped:
        try:
            archive_dropped(path, dropped)
        except Exception as e:
            stats["aborted"] = f"could not archive removed content ({e})"
            return stats
        if not REMOVED_LINES_ARCHIVE.exists():
            stats["aborted"] = "archive missing after write"
            return stats

    stats["backup"] = str(make_backup(path))

    # TOCTOU: refuse if anything appended while we were scanning.
    try:
        st_now = path.stat()
    except OSError as e:
        stats["aborted"] = f"file vanished ({e})"
        return stats
    if (st_now.st_mtime_ns, st_now.st_size) != (st_before.st_mtime_ns, st_before.st_size):
        stats["aborted"] = "file changed while being scanned (concurrent append)"
        return stats

    tmp = path.with_suffix(path.suffix + ".apirepair-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
            for ln in new_lines:
                f.write(ln + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        stats["aborted"] = f"atomic write failed ({e})"
    return stats


def running_session_ids() -> set:
    """Session ids of Claude Code processes running right now.

    A far better liveness signal than a timestamp, when it is available. Not
    every process names its session on the command line (`claude -r` does not),
    so this narrows the guess rather than replacing the mtime fallback.
    """
    ids = set()
    try:
        import subprocess
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" "
                 "| ForEach-Object { $_.CommandLine }"],
                capture_output=True, text=True, timeout=20).stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"],
                                 capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ids
    import re as _re
    for m in _re.finditer(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12}", out or ""):
        ids.add(m.group(0))
    return ids


def cmd_from_hook() -> int:
    """Repair exactly the transcript named by a SessionEnd hook payload.

    This is the safe moment: the session has ended, so the file is no longer
    being appended to, and we do not have to guess from a timestamp. It closes
    the window in which a just-closed session is still broken and a resume
    would fail.
    """
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    # end_reason "resume" means this transcript is about to be reopened by the
    # resuming process -- exactly the case where rewriting is unsafe.
    if str(data.get("end_reason", "")).lower() == "resume":
        return 0

    tp = data.get("transcript_path") or ""
    path = Path(tp) if tp else None
    if path is None or not path.is_file():
        sid = data.get("session_id") or ""
        hits = resolve_target(sid) if sid else []
        if not hits:
            return 0
        path = hits[0]

    r = repair_file(path, dry_run=False, force="session_end")
    if r.get("blocks_removed") or r.get("lines_removed"):
        print(f"[api-repair] {path.name}: removed "
              f"{r.get('lines_removed',0)} empty-thinking lines, "
              f"{r.get('blocks_removed',0)} blocks; relinked {r.get('relinked',0)}")
        log([r], dry_run=False)
    elif r.get("aborted"):
        print(f"[api-repair] {path.name}: ABORTED ({r['aborted']})")
        log([r], dry_run=False)
    return 0


def iter_transcripts():
    if not PROJECTS_DIR.exists():
        return
    for p in PROJECTS_DIR.rglob("*.jsonl"):
        if p.is_file() and "acompact" not in str(p):
            yield p


def resolve_target(token: str):
    p = Path(token)
    if p.is_file():
        return [p]
    hits = [f for f in iter_transcripts() if f.stem == token]
    return hits


def log(results, dry_run):
    REPAIR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(REPAIR_LOG, "a", encoding="utf-8") as f:
        label = "Scan (no files modified)" if dry_run else "Repair run"
        f.write(f"\n{'='*60}\n{label}: {datetime.now().isoformat()}\n{'='*60}\n")
        for s in results:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def summarise(results, dry_run):
    touched = [s for s in results
               if s.get("blocks_removed") or s.get("lines_removed")]
    skipped = [s for s in results if s.get("skipped")]
    aborted = [s for s in results if s.get("aborted")]
    errors = [s for s in results if s.get("error")]
    verb = "would remove" if dry_run else "removed"
    print(f"\nfiles needing repair : {len(touched)}")
    print(f"{verb} empty blocks    : {sum(s.get('blocks_removed',0) for s in touched)}")
    print(f"{verb} empty-only lines: {sum(s.get('lines_removed',0) for s in touched)}")
    if not dry_run:
        print(f"parent links repaired: {sum(s.get('relinked',0) for s in touched)}")
    sole = sum(s.get("unfixable", 0) for s in touched)
    if sole:
        print(f"  (of which {sole} were the sole line of their message)")
    if skipped:
        print(f"skipped as live/recent: {len(skipped)}")
    if aborted:
        print(f"ABORTED by a guard    : {len(aborted)}")
        for s in aborted[:5]:
            print(f"    {Path(s['file']).name}: {s['aborted']}")
    if errors:
        print(f"errors                : {len(errors)}")
    if dry_run:
        print("(SCAN ONLY -- nothing was modified; use `fix` to apply)")
    else:
        print(f"every removed item archived to: {REMOVED_LINES_ARCHIVE}")


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "scan"
    rest = [a for a in args[1:] if not a.startswith("-")]
    dry_run = mode != "fix"

    if mode == "from-hook":
        return cmd_from_hook()

    if mode not in ("scan", "fix"):
        print(__doc__)
        return 1

    if mode == "fix" and not rest and "--all" not in args:
        print("refusing: `fix` needs --all, a session id, or a path "
              "(use `scan` to see what would change)")
        return 1

    if rest:
        targets = []
        for tok in rest:
            hits = resolve_target(tok)
            if not hits:
                print(f"no transcript found for: {tok}")
                return 1
            targets.extend(hits)
        force = True          # explicit target = explicit consent
    else:
        # Sweep: additionally exclude any session a running Claude Code process
        # names on its command line, so we do not depend on mtime alone.
        live_ids = running_session_ids() if not dry_run else set()
        targets = [t for t in iter_transcripts() if t.stem not in live_ids]
        force = False

    results = []
    for t in targets:
        r = repair_file(t, dry_run=dry_run, force=force)
        if (r.get("blocks_removed") or r.get("lines_removed")
                or r.get("skipped") or r.get("aborted") or r.get("error")):
            results.append(r)
            if rest:
                print(json.dumps(r, ensure_ascii=False, indent=2))

    summarise(results, dry_run)
    log(results, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
