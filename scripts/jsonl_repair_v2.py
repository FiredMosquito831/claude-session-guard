#!/usr/bin/env python3
"""
jsonl_repair_v2 -- incremental, byte-exact repair of Claude Code session transcripts (PR-09, F8).

Replaces the sweep of jsonl_repair.py. That file is not changed.

Commands
    --all              gated sweep of every transcript, subagents included
                       (at most one per 6 hours, at most 4 per rolling 24 hours)
    --all --force      the same sweep without the gate
    --all --dry-run    report only. Writes nothing: no schedule, no cache, no log, no lock
    <file or folder>   repair that file now, with no gate. Add --dry-run to report only

Compared with jsonl_repair.py:
  * An unparseable line is kept as its exact original bytes. v1 kept a stripped copy.
  * A repaired line is written as the repaired text. Its original bytes are archived first,
    with reason "original of repaired line".
  * A blank line is removed, and each one is archived first, with reason "blank line".
    v1 removed blank lines without archiving them.
  * Subagent and workflow transcripts are swept. v1 skipped them. Paths with "acompact" are skipped.
  * A file verified clean is remembered by (size, mtime_ns) and is not read again.
  * The sweep runs on 8 threads, one whole transcript per task. A failing file is counted and the rest go on.

Safety
  * Removed content goes to the permanent archive through gk.append_removed_records (one locked writer).
    The archive write comes before the replace. If it raises, the rewrite is abandoned.
  * Size and mtime are checked before the backup, before the archive write and before the replace.
    Any change aborts the rewrite.
  * Live files are skipped: modified in the last hour, on a running claude command line, or
    (sweep only) named by CLAUDE_SESSION_ID.
  * Writes are binary and atomic. Append offsets are invalidated after each rewrite. Backups are never pruned.
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
import api_repair_v2 as api  # noqa: E402  (running_session_ids)

PROJECTS_DIR = gk.PROJECTS_DIR
BACKUPS_DIR = gk.CLAUDE_DIR / "backups" / "sessions"
ACTIVE_SKIP_SECONDS = 3600
COOLDOWN_SECONDS = 6 * 3600
MAX_SWEEPS_PER_DAY = 4
SCHEDULE_FILE = gk.STATE_DIR / "jsonl_repair_schedule.json"
CACHE_NAME = "jsonl_repair_clean"
CONTROL_CHARS = "\x00\x07\x08\x0b\x0c\x0d\x1a"


def is_valid_json_line(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def attempt_repair_line(line: str):
    """The repair rules of jsonl_repair.attempt_repair_line. Returns the repaired text, or None."""
    line = line.strip()
    if not line:
        return None
    if is_valid_json_line(line):
        return line
    if line.startswith("{") and not line.endswith("}"):           # missing closing brace
        for suffix in ("}", "}}"):
            if is_valid_json_line(line + suffix):
                return line + suffix
    if line.startswith("[") and not line.endswith("]"):           # missing closing bracket
        if is_valid_json_line(line + "]"):
            return line + "]"
    fixed = re.sub(r",\s*}", "}", line)                           # trailing commas
    fixed = re.sub(r",\s*]", "]", fixed)
    if is_valid_json_line(fixed):
        return fixed
    fixed = re.sub(r"[\x00\x07\x08\x0b\x0c]", "", line)           # control characters
    if is_valid_json_line(fixed):
        return fixed
    return None


def analyse_bytes(raw: bytes):
    """Pure. Returns (stats, new_bytes | None, dropped).

    new_bytes is None when nothing would change. dropped is a list of (1-based line number, reason,
    text) for every removed line, in file order. Its text is the original line, decoded with
    surrogateescape, so archiving it and decoding it again gives back the same bytes.
    """
    lines = raw.split(b"\n")
    if raw.endswith(b"\n"):
        lines.pop()
    stats = {"original_lines": len(lines), "valid_lines": 0, "repaired_lines": 0, "removed_lines": 0,
             "blank_lines": 0, "preserved_unparseable": 0}
    seen, out, dropped = set(), [], []
    for i, raw_line in enumerate(lines, 1):
        text = raw_line.decode("utf-8", "surrogateescape")
        stripped = text.strip()
        if not stripped:
            stats["blank_lines"] += 1
            stats["removed_lines"] += 1
            dropped.append((i, "blank line", text))
            continue
        if all(c in CONTROL_CHARS for c in stripped):
            stats["removed_lines"] += 1
            dropped.append((i, "control chars only", text))
            continue
        if is_valid_json_line(stripped):
            if stripped in seen:
                stats["removed_lines"] += 1
                dropped.append((i, "duplicate", text))
                continue
            seen.add(stripped)
            stats["valid_lines"] += 1
            out.append(raw_line)                       # kept exactly as it was, not stripped
            continue
        fixed = attempt_repair_line(stripped)
        if fixed is None:
            stats["preserved_unparseable"] += 1
            out.append(raw_line)                       # unparseable: kept byte for byte, never deleted
            continue
        stats["repaired_lines"] += 1
        if fixed in seen:
            stats["removed_lines"] += 1
            dropped.append((i, "duplicate after repair", text))
            continue
        seen.add(fixed)
        dropped.append((i, "original of repaired line", text))
        out.append(fixed.encode("utf-8", "surrogateescape"))
    if not dropped:
        return stats, None, []
    return stats, b"".join(x + b"\n" for x in out), dropped


def _changed(path: Path, st) -> bool:
    """True when the file is gone, or its size or mtime differs from st, the stat taken at the start."""
    try:
        now = path.stat()
    except OSError:
        return True
    return (now.st_mtime_ns, now.st_size) != (st.st_mtime_ns, st.st_size)


def make_backup(path: Path) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = BACKUPS_DIR / f"{path.stem}.jrbackup.{stamp}_{os.getpid()}.jsonl"
    shutil.copy2(path, dest)
    return dest


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def repair_file(path: Path, dry_run: bool = False, live_ids=frozenset(), sweep: bool = False) -> dict:
    """Repair one transcript. Order of writes: backup, archive of the removed lines, atomic replace.

    The size and mtime are checked before the backup, before the archive write and before the replace.
    """
    try:
        st0 = path.stat()
        raw = path.read_bytes()
    except OSError as e:
        return {"file": str(path), "error": f"read failed: {e}"}
    if not dry_run:
        active = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if sweep and active and path.stem == active:
            return {"file": str(path), "skipped": "active session"}
        if path.stem in live_ids:
            return {"file": str(path), "skipped": "running session"}
        if time.time() - st0.st_mtime < ACTIVE_SKIP_SECONDS:
            return {"file": str(path), "skipped": "modified recently (possibly live)"}
    stats, new_bytes, dropped = analyse_bytes(raw)
    stats["file"] = str(path)
    if new_bytes is None:
        return stats
    if dry_run:
        stats["dry_run"] = True
        return stats
    if _changed(path, st0):
        stats["aborted"] = "file changed while being checked; left untouched"
        return stats
    try:
        stats["backup"] = str(make_backup(path))          # a copy is not a removal
    except OSError as e:
        stats["aborted"] = f"backup failed ({e}); left untouched"
        return stats
    if _changed(path, st0):
        stats["aborted"] = "file changed after backup; nothing archived"
        return stats
    if dropped:
        records = [{"source_file": str(path), "source_line": n, "reason": reason, "content": text}
                   for n, reason, text in dropped]
        try:
            stats["archived"], stats["archive_skipped"] = gk.append_removed_records(records)
        except Exception as e:
            stats["aborted"] = f"could not archive removed content ({e}); left untouched"
            return stats
    tmp = path.with_name(f"{path.name}.{os.getpid()}.jrepair-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(new_bytes)
            f.flush()
            os.fsync(f.fileno())
        if _changed(path, st0):
            _unlink_quiet(tmp)
            stats["aborted"] = "file changed before replace; temp removed"
            return stats
        os.replace(tmp, path)
    except OSError as e:
        _unlink_quiet(tmp)
        stats["aborted"] = f"atomic write failed ({e})"
        return stats
    stats["rewritten"] = True
    try:
        gk.invalidate_offsets(path.relative_to(PROJECTS_DIR).as_posix())
    except ValueError:
        pass
    return stats


def schedule_allows(now: float):
    data = gk.load_json(SCHEDULE_FILE, {})
    runs = [t for t in (data.get("runs", []) if isinstance(data, dict) else [])
            if isinstance(t, (int, float))]
    last = max(runs, default=0)
    if now - last < COOLDOWN_SECONDS:
        nxt = time.strftime("%H:%M", time.localtime(last + COOLDOWN_SECONDS))
        return False, f"cooldown: the next sweep is allowed at {nxt}"
    if sum(1 for t in runs if now - t < 86400) >= MAX_SWEEPS_PER_DAY:
        return False, f"{MAX_SWEEPS_PER_DAY} sweeps already ran in the last 24 hours"
    return True, ""


def schedule_record(now: float) -> None:
    data = gk.load_json(SCHEDULE_FILE, {})
    runs = [t for t in (data.get("runs", []) if isinstance(data, dict) else [])
            if isinstance(t, (int, float)) and now - t < 7 * 86400]
    gk.atomic_write_json(SCHEDULE_FILE, {"runs": (runs + [now])[-50:]})


def sweep_incremental(workers: int = gk.POOL_WORKERS, dry_run: bool = False) -> dict:
    cache = gk.CleanCache(CACHE_NAME)
    now = time.time()
    seen, todo = set(), []
    res = {"transcripts": 0, "clean": 0, "recent": 0, "checked": 0, "rewritten": 0, "would_rewrite": 0,
           "aborted": 0, "errors": 0, "live": 0, "removed": 0, "blank": 0, "repaired": 0, "preserved": 0}
    for rel, p, size, mtime_ns, mtime_s in gk.transcript_files(PROJECTS_DIR):
        res["transcripts"] += 1
        seen.add(rel)
        if cache.is_clean(rel, size, mtime_ns):
            res["clean"] += 1
        elif now - mtime_s < ACTIVE_SKIP_SECONDS:
            res["recent"] += 1
        else:
            todo.append((rel, p))
    live = set() if (dry_run or not todo) else api.running_session_ids()

    def work(item):
        rel, p = item
        st0 = p.stat()
        r = repair_file(p, dry_run=dry_run, live_ids=live, sweep=True)
        if r.get("error"):
            return ("error", rel, r)
        if r.get("aborted"):
            return ("aborted", rel, r)
        if r.get("skipped"):
            return ("live", rel, r)
        if r.get("rewritten"):
            return ("rewritten", rel, r)
        if r.get("dry_run"):
            return ("would_rewrite", rel, r)
        st1 = p.stat()
        if (st1.st_size, st1.st_mtime_ns) == (st0.st_size, st0.st_mtime_ns):
            return ("clean", rel, st1)
        return ("changed", rel, None)                # changed while checked: not marked clean

    try:
        for _item, out, err in gk.run_pool(work, todo, workers):
            res["checked"] += 1
            if err is not None:
                res["errors"] += 1
                continue
            kind, rel, payload = out
            if kind == "clean":
                if not dry_run:
                    cache.mark(rel, payload.st_size, payload.st_mtime_ns)
            elif kind in ("rewritten", "would_rewrite"):
                res["rewritten" if kind == "rewritten" else "would_rewrite"] += 1
                res["removed"] += payload.get("removed_lines", 0)
                res["blank"] += payload.get("blank_lines", 0)
                res["repaired"] += payload.get("repaired_lines", 0)
                res["preserved"] += payload.get("preserved_unparseable", 0)
            elif kind == "aborted":
                res["aborted"] += 1
            elif kind == "error":
                res["errors"] += 1
            elif kind == "live":
                res["live"] += 1
        if not dry_run:
            cache.prune(seen)
    finally:
        if not dry_run:
            cache.close()                             # saves what was verified, even if the sweep stops early
    return res


def _summary(r: dict, workers: int) -> str:
    return (f"{r['transcripts']} transcripts: {r['clean']} verified clean and skipped, "
            f"{r['recent']} recent and skipped, {r['checked']} checked on {workers} threads; "
            f"rewritten {r['rewritten']} ({r['removed']} lines removed, {r['blank']} blank, "
            f"{r['repaired']} repaired, {r['preserved']} unparseable kept), "
            f"would rewrite {r['would_rewrite']}, aborted {r['aborted']}, errors {r['errors']}, live {r['live']}")


def sweep(force: bool = False, dry_run: bool = False) -> int:
    if dry_run:
        r = sweep_incremental(dry_run=True)
        print("[jsonl-repair] dry run: " + _summary(r, gk.POOL_WORKERS))
        return 0
    now = time.time()
    lock = gk.FileLock("jsonl_repair-sweep")
    if not lock.acquire():
        print("[jsonl-repair] another sweep is running; this start skips it")
        return 0
    try:
        ok, why = (True, "") if force else schedule_allows(now)
        if not ok:
            print(f"[jsonl-repair] skipped: {why}")
            return 0
        schedule_record(now)                          # recorded before any work starts
        t0 = time.time()
        r = sweep_incremental()
        line = _summary(r, gk.POOL_WORKERS) + f"; {time.time() - t0:.1f}s"
        print("[jsonl-repair] " + line)
        gk.log_line("jsonl_repair", "sweep " + line)
        return 0
    finally:
        lock.release()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    rest = [a for a in args if not a.startswith("--")]
    if "--all" in args:
        return sweep(force="--force" in args, dry_run=dry)
    if not rest:
        print(__doc__)
        return 1
    target = Path(rest[0])
    if target.is_dir():
        files = sorted(target.glob("*.jsonl"))
    elif target.is_file() and target.suffix == ".jsonl":
        files = [target]
    else:
        print(f"no transcript found at: {target}")
        return 1
    live = set() if dry else api.running_session_ids()
    for f in files:
        print(json.dumps(repair_file(f, dry_run=dry, live_ids=live, sweep=False), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
