#!/usr/bin/env python3
"""
guardkit -- shared helpers for the session-guard tools. Stdlib only.

Used by api_repair, jsonl_repair, session_archive, usage_db and session_indexer.
This module decides nothing about what is repaired or archived. It provides:

  * FileLock / lock_wait     cross-process locks that survive a killed process
  * atomic_write_json        state files that are never half-written
  * write_bytes_atomic       binary-exact atomic write, and read_raw_lines to match
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


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` byte for byte: temp file, fsync, then os.replace.

    If anything fails, the temp file is removed and the old file is left as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def read_raw_lines(path: Path) -> list:
    """The lines of a file as bytes, without the trailing newline.

    Every other byte is kept, including a carriage return. A final newline does not add an
    empty last line. A missing file gives [].
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


TAKEOVER_STALE = 30.0   # seconds after which a takeover marker may be removed (see FileLock)


class FileLock:
    """Cross-process lock using O_EXCL. A lock older than `stale` seconds is treated as abandoned.

    Two processes can both see the same lock as stale. The takeover is settled under a second
    lock, the takeover marker `<name>.lock.takeover`, which only one process can create at a time.
    The marker holder checks the lock's age again before it removes the lock, so it never removes a
    fresh lock that someone else has just created. release() removes the lock only if its pid is ours.

    A PermissionError from the O_EXCL create (step 1) or from the takeover marker create (step 3) is
    treated as busy, not raised. This is a deliberate fail-safe. On Windows, a file that was just
    unlinked refuses a new create until its last handle closes, which is transient. A permanent
    permission fault on the lock folder also reads as busy: acquire returns False, lock_wait times
    out, and the sweep skips its work instead of crashing.

    Residual risk: a process that holds the takeover marker and is suspended for more than
    TAKEOVER_STALE seconds can have its marker removed by another process, which may then take the
    lock over. This is accepted for now.
    """

    def __init__(self, name: str, stale: float = 1800.0):
        self.path = STATE_DIR / "locks" / f"{name}.lock"
        self.takeover = STATE_DIR / "locks" / f"{name}.lock.takeover"
        self.stale = stale
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            # 1. Create the lock. O_EXCL lets exactly one process succeed.
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                # PermissionError: on Windows a file that was just unlinked refuses a new O_EXCL
                # create until the last handle to it closes. That is busy, not a crash.
                pass
            else:
                try:
                    os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
                    os.fsync(fd)
                except OSError:
                    os.close(fd)
                    _unlink_quiet(self.path)      # never leave an empty lock that blocks others
                    raise
                os.close(fd)
                self.held = True
                return True

            # 2. The lock exists. It is live while it is younger than `stale`.
            try:
                age = time.time() - os.stat(self.path).st_mtime
            except FileNotFoundError:
                continue                          # removed since the failed create
            except OSError:
                return False
            if age < self.stale:
                return False

            # 3. The lock is stale. Replace it only while holding the takeover marker.
            try:
                mfd = os.open(str(self.takeover), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):   # PermissionError: see step 1
                try:
                    marker_age = time.time() - os.stat(self.takeover).st_mtime
                except OSError:
                    marker_age = 0.0
                if marker_age <= TAKEOVER_STALE:
                    return False                  # another process is taking over
                _unlink_quiet(self.takeover)
                continue

            # 4. We hold the marker. Record our pid, then remove the lock if it is still stale.
            try:
                try:
                    os.write(mfd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(mfd)
                try:
                    age = time.time() - os.stat(self.path).st_mtime
                except FileNotFoundError:
                    age = None                    # already gone
                if age is not None and age >= self.stale:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass                              # removed between the stat and the unlink
            except OSError:
                return False                      # could not write or remove: not acquired
            finally:
                # 5. Drop the marker. The next attempt tries to create the lock.
                _unlink_quiet(self.takeover)
        return False

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            first = self.path.read_bytes().split(maxsplit=1)
        except OSError:
            return                                # already gone
        if not first or first[0] != str(os.getpid()).encode():
            return                                # someone took the lock over: leave it alone
        _unlink_quiet(self.path)


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
        self.last_save = time.time()

    def is_clean(self, rel: str, size: int, mtime_ns: int) -> bool:
        return self.data.get(rel) == [size, mtime_ns]

    def mark(self, rel: str, size: int, mtime_ns: int) -> None:
        self.data[rel] = [size, mtime_ns]
        self.pending += 1
        if self.pending >= 20 or time.time() - self.last_save >= 10:
            self.save()

    def prune(self, seen: set) -> None:
        for k in [k for k in self.data if k not in seen]:
            del self.data[k]

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
        self.pending = 0
        self.last_save = time.time()

    def close(self) -> None:
        # Always save, even with nothing pending: prune() changes the data without touching
        # pending, so a close() that skipped the save would lose the prune.
        self.save()


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
