#!/usr/bin/env python3
"""
API Repair -- removes empty thinking blocks that make the Messages API reject a
resumed transcript ("each thinking block must contain non-whitespace thinking").

Commands
    fix --all [--force]   gated sweep of every transcript (SessionStart, PreCompact)
    fix <session|path>    repair one transcript now
    from-hook             repair the transcript named in a SessionEnd payload (ungated)
    scan                  read-only report over every transcript

Gate: a sweep runs at most once per 6 hours and at most 4 times per rolling 24 hours.
The run is recorded before any work starts, so a sweep that is killed still counts.

Speed
  * Verified-clean transcripts are remembered by (size, mtime_ns) and skipped.
  * A regex finds only the lines that could hold an empty or whitespace thinking value;
    just those lines are parsed. The regex is a superset of the real condition, so no
    empty block is missed (it only ever lets through extra lines, which are then checked).
  * Transcripts are checked in parallel, one transcript per task, on a ThreadPoolExecutor
    of 8. Each task is isolated: an error in one file cannot stop the others.
  * The running-session process check runs only when there is something to repair.

Safety
  * Removed content is copied to the permanent archive before any write.
  * Writes are atomic; a backup is taken first.
  * A file that changes while it is being checked is left alone.
  * Recently written files and the running session are skipped.
  * Each repaired file's append offsets are invalidated, so the archive and usage
    syncs re-read it in full next time.
"""
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardkit as gk  # noqa: E402

PROJECTS_DIR = gk.PROJECTS_DIR
BACKUPS_DIR = gk.CLAUDE_DIR / "backups" / "sessions"
REMOVED_LINES_ARCHIVE = BACKUPS_DIR / "removed-lines-archive.jsonl"
ACTIVE_SKIP_SECONDS = 3600
COOLDOWN_SECONDS = 6 * 3600
MAX_SWEEPS_PER_DAY = 4
SCHEDULE_FILE = gk.STATE_DIR / "api_repair_schedule.json"

# Superset of every thinking value that can be empty or whitespace-only (or null):
#   "thinking": ""   "thinking": "\n\n"   "thinking": " "   "thinking": null
# Anything else (a value with an ASCII letter, digit, punctuation or an escaped quote)
# cannot be empty. Non-ASCII bytes are allowed through, which is the conservative side.
EMPTY_CAND = re.compile(
    rb'"thinking"\s*:\s*(?:null|"(?:[ \t\r\n\x0b\x0c]|\\[ntrfbv]|\\u[0-9a-fA-F]{4}|[^"\\\x00-\x7f])*")')


def is_empty_thinking(block) -> bool:
    if not isinstance(block, dict) or block.get("type") != "thinking":
        return False
    t = block.get("thinking")
    return t is None or (isinstance(t, str) and not t.strip())


def _line_bounds(raw: bytes, idx: int):
    s = raw.rfind(b"\n", 0, idx) + 1
    e = raw.find(b"\n", idx)
    return s, (len(raw) if e == -1 else e)


def analyse_bytes(path, raw: bytes):
    """Pure. Returns (stats, new_lines | None, dropped). new_lines is None when nothing changes."""
    stats = {"file": str(path), "blocks_removed": 0, "lines_removed": 0, "relinked": 0}
    starts = set()
    for m in EMPTY_CAND.finditer(raw):
        starts.add(raw.rfind(b"\n", 0, m.start()) + 1)
    if not starts:
        return stats, None, []

    changed = {}      # line start offset -> parsed object (content or parent link rewritten)
    drop = set()      # line start offsets to remove
    remap = {}        # uuid of a dropped line -> its parentUuid
    dropped = []      # (1-based line number, reason, original text): archived before any write
    prev, n = 0, 0
    for s in sorted(starts):
        n += raw.count(b"\n", prev, s)
        prev = s
        _, e = _line_bounds(raw, s)
        text = raw[s:e].decode("utf-8", "surrogateescape")
        try:
            o = json.loads(text.strip())
        except ValueError:
            continue
        m = o.get("message") if isinstance(o, dict) else None
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        empties = [b for b in m["content"] if is_empty_thinking(b)]
        if not empties:
            continue
        keep = [b for b in m["content"] if not is_empty_thinking(b)]
        if keep:
            m["content"] = keep
            changed[s] = o
            stats["blocks_removed"] += len(empties)
            for b in empties:
                dropped.append((n + 1, "empty thinking block removed from line",
                                json.dumps(b, ensure_ascii=False)))
        else:
            drop.add(s)
            stats["lines_removed"] += 1
            dropped.append((n + 1, "empty-thinking-only line", text))
            if o.get("uuid"):
                remap[o["uuid"]] = o.get("parentUuid")
    if not drop and not changed:
        return stats, None, []

    if remap:
        def resolve(uid):
            seen = set()
            while uid in remap and uid not in seen:
                seen.add(uid)
                uid = remap[uid]
            return uid

        pat = re.compile(b"|".join(re.escape(u.encode("utf-8")) for u in remap))
        visited = set()
        for m in pat.finditer(raw):
            s, e = _line_bounds(raw, m.start())
            if s in drop or s in visited:
                continue
            visited.add(s)
            o = changed.get(s)
            if o is None:
                try:
                    o = json.loads(raw[s:e].decode("utf-8", "surrogateescape").strip())
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
            parent = o.get("parentUuid")
            if parent in remap:
                o["parentUuid"] = resolve(parent)
                changed[s] = o
                stats["relinked"] += 1

    out = []
    pos, size = 0, len(raw)
    while pos < size:
        e = raw.find(b"\n", pos)
        if e == -1:
            e = size
        if pos in drop:
            pass
        elif pos in changed:
            out.append(json.dumps(changed[pos], ensure_ascii=False).encode("utf-8", "surrogateescape"))
        else:
            out.append(raw[pos:e])
        pos = e + 1
    return stats, out, dropped


def analyse(path: Path):
    return analyse_bytes(path, path.read_bytes())


def make_backup(path: Path) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = BACKUPS_DIR / f"{path.stem}.apibackup.{stamp}_{os.getpid()}.jsonl"
    shutil.copy2(path, dest)
    return dest


def archive_dropped(path: Path, dropped) -> None:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat()
    lock = gk.lock_wait("removed-lines-archive", timeout=60)
    if lock is None:
        raise RuntimeError("the removed-lines archive is locked by another process")
    try:
        with open(REMOVED_LINES_ARCHIVE, "a", encoding="utf-8") as f:
            for lineno, reason, raw in dropped:
                f.write(json.dumps({"archived_at": stamp, "source_file": str(path),
                                    "source_line": lineno, "reason": reason,
                                    "content": raw}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        lock.release()


def repair_file(path: Path, dry_run: bool = True, force=False) -> dict:
    try:
        st_before = path.stat()
    except OSError as e:
        return {"file": str(path), "error": f"stat failed: {e}"}
    active = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if dry_run or force == "session_end":
        pass
    elif not force:
        if active and path.stem == active:
            return {"file": str(path), "skipped": "active session"}
        if time.time() - st_before.st_mtime < ACTIVE_SKIP_SECONDS:
            return {"file": str(path), "skipped": "modified recently (possibly live)"}
    else:
        if active and path.stem == active:
            return {"file": str(path), "skipped": "refusing: this is the running session"}
        if time.time() - st_before.st_mtime < 120:
            return {"file": str(path), "skipped": "refusing: written in the last 2 minutes (looks live)"}
    try:
        stats, new_lines, dropped = analyse_bytes(path, path.read_bytes())
    except OSError as e:
        return {"file": str(path), "error": f"read failed: {e}"}
    if new_lines is None:
        return stats
    if dry_run:
        stats["dry_run"] = True
        return stats
    if dropped:
        try:
            archive_dropped(path, dropped)
        except Exception as e:
            stats["aborted"] = f"could not archive removed content ({e})"
            return stats
    stats["backup"] = str(make_backup(path))
    try:
        st_now = path.stat()
    except OSError as e:
        stats["aborted"] = f"file vanished ({e})"
        return stats
    if (st_now.st_mtime_ns, st_now.st_size) != (st_before.st_mtime_ns, st_before.st_size):
        stats["aborted"] = "file changed while being checked (concurrent append); left untouched"
        return stats
    tmp = path.with_name(f"{path.name}.{os.getpid()}.apirepair-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(b"".join(ln + b"\n" for ln in new_lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        stats["aborted"] = f"atomic write failed ({e})"
        return stats
    try:
        gk.invalidate_offsets(path.relative_to(PROJECTS_DIR).as_posix())
    except ValueError:
        pass
    return stats


def running_session_ids() -> set:
    """Session ids named on the command line of running Claude Code processes."""
    import subprocess
    ids = set()
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" "
                 "| ForEach-Object { $_.CommandLine }"],
                capture_output=True, text=True, timeout=20).stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ids
    for m in re.finditer(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out or ""):
        ids.add(m.group(0))
    return ids


def schedule_allows(now: float):
    runs = gk.load_json(SCHEDULE_FILE, {}).get("runs", [])
    last = max(runs, default=0)
    if now - last < COOLDOWN_SECONDS:
        nxt = time.strftime("%H:%M", time.localtime(last + COOLDOWN_SECONDS))
        return False, f"cooldown: the next sweep is allowed at {nxt}"
    if sum(1 for t in runs if now - t < 86400) >= MAX_SWEEPS_PER_DAY:
        return False, f"{MAX_SWEEPS_PER_DAY} sweeps already ran in the last 24 hours"
    return True, ""


def schedule_record(now: float) -> None:
    runs = [t for t in gk.load_json(SCHEDULE_FILE, {}).get("runs", []) if now - t < 7 * 86400]
    gk.atomic_write_json(SCHEDULE_FILE, {"runs": (runs + [now])[-50:]})


def sweep_incremental(workers: int = gk.POOL_WORKERS) -> dict:
    cache = gk.CleanCache("api_repair_clean")
    now = time.time()
    seen, todo = set(), []
    res = {"transcripts": 0, "clean": 0, "recent": 0, "checked": 0, "repaired": 0,
           "aborted": 0, "errors": 0, "live": 0, "lines": 0, "blocks": 0, "relinked": 0}
    for rel, p, size, mtime_ns, mtime_s in gk.transcript_files(PROJECTS_DIR):
        res["transcripts"] += 1
        seen.add(rel)
        if cache.is_clean(rel, size, mtime_ns):
            res["clean"] += 1
        elif now - mtime_s < ACTIVE_SKIP_SECONDS:
            res["recent"] += 1
        else:
            todo.append((rel, p))
    live = running_session_ids() if todo else set()

    def work(item):
        rel, p = item
        if p.stem in live:
            return ("live", rel, None)
        st0 = p.stat()
        r = repair_file(p, dry_run=False, force=False)
        if r.get("error"):
            return ("error", rel, r)
        if r.get("aborted"):
            return ("aborted", rel, r)
        if r.get("skipped"):
            return ("live", rel, r)
        if r.get("backup") or r.get("blocks_removed") or r.get("lines_removed"):
            return ("repaired", rel, r)
        st1 = p.stat()
        if (st1.st_size, st1.st_mtime_ns) == (st0.st_size, st0.st_mtime_ns):
            return ("clean", rel, st1)
        return ("changed", rel, None)

    for _item, out, err in gk.run_pool(work, todo, workers):
        res["checked"] += 1
        if err is not None:
            res["errors"] += 1
            continue
        kind, rel, payload = out
        if kind == "clean":
            cache.mark(rel, payload.st_size, payload.st_mtime_ns)
        elif kind == "repaired":
            res["repaired"] += 1
            res["lines"] += payload.get("lines_removed", 0)
            res["blocks"] += payload.get("blocks_removed", 0)
            res["relinked"] += payload.get("relinked", 0)
        elif kind == "aborted":
            res["aborted"] += 1
        elif kind == "error":
            res["errors"] += 1
        elif kind == "live":
            res["live"] += 1
    cache.prune(seen)
    cache.save()
    return res


def sweep(force: bool = False) -> int:
    now = time.time()
    lock = gk.FileLock("api_repair-sweep")
    if not lock.acquire():
        print("[api-repair] another sweep is running; this start skips it")
        return 0
    try:
        ok, why = (True, "") if force else schedule_allows(now)
        if not ok:
            print(f"[api-repair] skipped: {why}")
            return 0
        schedule_record(now)
        t0 = time.time()
        r = sweep_incremental()
        line = (f"{r['transcripts']} transcripts: {r['clean']} verified clean and skipped, "
                f"{r['recent']} recent and skipped, {r['checked']} checked on {gk.POOL_WORKERS} threads; "
                f"repaired {r['repaired']} ({r['lines']} lines, {r['blocks']} blocks, "
                f"{r['relinked']} links), aborted {r['aborted']}, errors {r['errors']}, "
                f"live {r['live']}; {time.time() - t0:.1f}s")
        print("[api-repair] " + line)
        gk.log_line("api_repair", "sweep " + line)
        return 0
    finally:
        lock.release()


def scan_all() -> int:
    files = list(gk.transcript_files(PROJECTS_DIR))

    def one(item):
        rel, p = item[0], item[1]
        stats, new_lines, _ = analyse(p)
        return rel, stats, new_lines is not None

    touched = lines = blocks = 0
    for _item, out, err in gk.run_pool(one, files):
        if err is not None:
            continue
        rel, stats, changed = out
        if changed:
            touched += 1
            lines += stats["lines_removed"]
            blocks += stats["blocks_removed"]
            print(f"  {rel}  lines={stats['lines_removed']} blocks={stats['blocks_removed']}")
    print(f"[api-repair scan] {len(files)} transcripts; {touched} need repair; "
          f"{lines} empty-only lines and {blocks} empty blocks would be removed (read-only)")
    return 0


def cmd_from_hook() -> int:
    data = gk.read_hook_payload()
    reason = str(data.get("end_reason", "")).lower() or "-"
    tp = str(data.get("transcript_path") or "")
    path = Path(tp) if tp else None
    if path is None or not path.is_file():
        sid = str(data.get("session_id") or "")
        hits = [p for _rel, p, *_ in gk.transcript_files(PROJECTS_DIR) if sid and p.stem == sid]
        path = hits[0] if hits else None
    if reason == "resume":
        gk.log_line("api_repair", f"from-hook end_reason=resume file={path.name if path else '-'} "
                                  "skipped: this transcript is about to be reopened")
        return 0
    if path is None:
        gk.log_line("api_repair", f"from-hook end_reason={reason} no transcript found")
        return 0
    r = repair_file(path, dry_run=False, force="session_end")
    gk.log_line("api_repair",
                f"from-hook end_reason={reason} file={path.name} blocks={r.get('blocks_removed', 0)} "
                f"lines={r.get('lines_removed', 0)} relinked={r.get('relinked', 0)} "
                f"aborted={r.get('aborted', '-')} skipped={r.get('skipped', '-')} error={r.get('error', '-')}")
    if r.get("blocks_removed") or r.get("lines_removed"):
        print(f"[api-repair] {path.name}: removed {r.get('lines_removed', 0)} empty-thinking lines, "
              f"{r.get('blocks_removed', 0)} blocks")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "scan"
    rest = [a for a in args[1:] if not a.startswith("-")]
    if mode == "from-hook":
        return cmd_from_hook()
    if mode == "scan":
        return scan_all()
    if mode == "fix":
        if "--all" in args:
            return sweep(force="--force" in args)
        if not rest:
            print("refusing: `fix` needs --all, a session id or a path")
            return 1
        targets = []
        for tok in rest:
            p = Path(tok)
            if p.is_file():
                targets.append(p)
            else:
                targets += [q for _rel, q, *_ in gk.transcript_files(PROJECTS_DIR) if q.stem == tok]
        if not targets:
            print("no transcript found for: " + " ".join(rest))
            return 1
        for t in targets:
            r = repair_file(t, dry_run=False, force=True)
            print(json.dumps(r, ensure_ascii=False))
            gk.log_line("api_repair", f"fix {t.name} {json.dumps(r, ensure_ascii=False)}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
