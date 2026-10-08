#!/usr/bin/env python3
"""
guardkit -- shared helpers for the session-guard tools. Stdlib only.

Used by api_repair, jsonl_repair, session_archive, usage_db and session_indexer.
This module decides nothing about what is repaired or archived. It provides:

  * FileLock / lock_wait     cross-process locks that survive a killed process
  * atomic_write_json        state files that are never half-written
  * transcript_files         one scandir pass over projects/ (no rglob, no re-walk)
  * run_pool                 a thread pool that keeps going when one item fails
  * CleanCache               "verified clean at this size and mtime" memory
  * read_hook_payload        the JSON Claude Code writes to a hook's stdin
  * session_transcripts      a session's transcript plus its subagent transcripts
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("SESSION_GUARD_HOME") or (Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSION_TOOLS = CLAUDE_DIR / "session-tools"
STATE_DIR = SESSION_TOOLS / "state"
LOG_DIR = SESSION_TOOLS / "logs"
POOL_WORKERS = 8
ARCHIVE_STATE = CLAUDE_DIR / "session-archive" / "state"       # per transcript: bytes already archived
USAGE_STATE = CLAUDE_DIR / "session-archive" / "usage-state"   # per transcript: bytes already ingested
                                                               # (L/ = live copy, A/ = archive copy)


def invalidate_offsets(rel: str) -> None:
    """Forget the append offsets kept for one transcript, so the next sync reads it in full again.

    Called after any tool rewrites a live transcript. A rewrite can change bytes before the
    offset without changing its length, so an offset must never survive a rewrite.
    """
    for side in (ARCHIVE_STATE / f"{rel}.json",
                 USAGE_STATE / "L" / f"{rel}.json",
                 USAGE_STATE / "A" / f"{rel}.json"):
        try:
            side.unlink()
        except OSError:
            pass


def log_line(name: str, text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{name}.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + text + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class FileLock:
    """Cross-process lock using O_EXCL. A lock older than `stale` seconds is treated as abandoned."""

    def __init__(self, name: str, stale: float = 1800.0):
        self.path = STATE_DIR / "locks" / f"{name}.lock"
        self.stale = stale
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < self.stale:
                        return False
                    self.path.unlink()
                except OSError:
                    return False
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
            return True
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def lock_wait(name: str, timeout: float = 30.0):
    """Acquire a FileLock, waiting up to `timeout` seconds. Returns the lock, or None."""
    lock = FileLock(name)
    end = time.time() + timeout
    while True:
        if lock.acquire():
            return lock
        if time.time() >= end:
            return None
        time.sleep(0.2)


def transcript_files(root: Path = PROJECTS_DIR, top_only: bool = False):
    """Yield (rel_posix, path, size, mtime_ns, mtime_s) for each transcript under root, in one pass.

    top_only=True yields only <project>/<session>.jsonl and skips subagent and workflow transcripts.
    """
    stack = [(str(root), 0)]
    while stack:
        d, depth = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    if not (top_only and depth >= 1):
                        stack.append((e.path, depth + 1))
                    continue
                if not e.name.endswith(".jsonl") or "acompact" in e.path:
                    continue
                if top_only and depth != 1:
                    continue
                st = e.stat()
                p = Path(e.path)
                yield p.relative_to(root).as_posix(), p, st.st_size, st.st_mtime_ns, st.st_mtime


def run_pool(fn, items, workers: int = POOL_WORKERS):
    """Run fn(item) on a thread pool. Yields (item, result, error) as each one finishes."""
    items = list(items)
    if not items:
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="guard") as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                yield it, fut.result(), None
            except Exception as exc:          # one bad file must not stop the sweep
                yield it, None, exc


class CleanCache:
    """Files verified clean at an exact (size, mtime_ns). Any change to a file invalidates its entry."""

    def __init__(self, name: str):
        self.path = STATE_DIR / f"{name}.json"
        data = load_json(self.path, {})
        self.data = data if isinstance(data, dict) else {}
        self.pending = 0

    def is_clean(self, rel: str, size: int, mtime_ns: int) -> bool:
        return self.data.get(rel) == [size, mtime_ns]

    def mark(self, rel: str, size: int, mtime_ns: int) -> None:
        self.data[rel] = [size, mtime_ns]
        self.pending += 1
        if self.pending >= 500:
            self.save()

    def prune(self, seen: set) -> None:
        for k in [k for k in self.data if k not in seen]:
            del self.data[k]

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
        self.pending = 0


def read_hook_payload() -> dict:
    """The JSON Claude Code writes to a hook's stdin (session_id, transcript_path, ...). {} when run by hand."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def session_transcripts(transcript: Path) -> list:
    """A session's own transcript plus its subagent and workflow transcripts, when present."""
    files = [transcript]
    sub = transcript.with_suffix("") / "subagents"
    if sub.is_dir():
        files.extend(p for p in sub.rglob("*.jsonl") if p.is_file() and "acompact" not in str(p))
    return files
