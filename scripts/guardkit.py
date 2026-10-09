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
  * append_removed_records   the one locked, binary writer for the removed-lines archive (PR-09)
"""
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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


# --- The removed-lines archive (PR-09, F19) ---------------------------------
# One writer for backups/sessions/removed-lines-archive.jsonl, shared by api_repair_v2 and
# jsonl_repair_v2. It takes the archive lock, writes each whole record in binary with O_APPEND,
# and keeps a digest index in STATE_DIR, so a record already archived is never archived again.

REMOVED_LINES_ARCHIVE = CLAUDE_DIR / "backups" / "sessions" / "removed-lines-archive.jsonl"
REMOVED_LOCK_NAME = "removed-lines-archive"
INDEX_READ_BLOCK = 8 * 1024 * 1024       # the archive is streamed in blocks this size (about 2 GB real)


def _removed_index_paths() -> tuple:
    """The digest index of the removed-lines archive, kept in the state folder."""
    return STATE_DIR / "removed_index.json", STATE_DIR / "removed_index.bin"


def _record_digest(source_file: str, content: str) -> bytes:
    """16-byte identity of one archived record: its source file and its exact content."""
    key = source_file.encode("utf-8", "surrogateescape") + b"\0" + content.encode("utf-8", "surrogateescape")
    return hashlib.blake2b(key, digest_size=16).digest()


def _refresh_removed_index(archive: Path, read_block: int) -> set:
    """Index the archive records written since the last refresh, and return every known digest.

    Call with the archive lock held. Only complete lines are read. The new digests are appended to
    removed_index.bin and synced before the offset is recorded in removed_index.json. If the archive
    is smaller than the recorded offset, it was truncated or replaced, so the index is rebuilt.
    """
    idx_json, idx_bin = _removed_index_paths()
    state = load_json(idx_json, None)
    offset = state.get("archive_offset") if isinstance(state, dict) else None
    try:
        archive_size = archive.stat().st_size
    except FileNotFoundError:
        archive_size = 0
    rebuild = not isinstance(offset, int) or offset < 0 or archive_size < offset
    if rebuild:
        if isinstance(offset, int) and offset > archive_size:
            msg = (f"removed-lines archive is smaller than its index ({archive_size} < {offset} bytes); "
                   "index rebuilt from the start")
            print("[guardkit] warning: " + msg, file=sys.stderr)
            log_line("removed_archive", "warning: " + msg)
        offset = 0
    new, bad, consumed = [], 0, offset      # consumed: archive offset through the last complete line
    try:
        f = open(archive, "rb")
    except FileNotFoundError:
        f = None
    if f is not None:
        with f:
            f.seek(offset)
            carry = b""
            while True:
                block = f.read(read_block)
                if not block:
                    break
                buf = carry + block
                cut = buf.rfind(b"\n") + 1
                if cut == 0:
                    carry = buf
                    continue
                for raw_line in buf[:cut].split(b"\n"):
                    if not raw_line.strip():
                        continue
                    try:
                        rec = json.loads(raw_line.decode("utf-8", "surrogateescape"))
                    except ValueError:
                        bad += 1
                        continue
                    if not isinstance(rec, dict) or not isinstance(rec.get("source_file"), str) \
                            or not isinstance(rec.get("content"), str):
                        bad += 1
                        continue
                    new.append(_record_digest(rec["source_file"], rec["content"]))
                consumed += cut
                carry = buf[cut:]
    if bad:
        log_line("removed_archive", f"removed-lines archive: {bad} unreadable record(s) skipped while indexing")
    idx_bin.parent.mkdir(parents=True, exist_ok=True)
    with open(idx_bin, "a+b") as f:
        size = f.seek(0, os.SEEK_END)
        keep = 0 if rebuild else size // 16 * 16  # drop a partial digest left by a killed write
        if keep != size:
            f.truncate(keep)
        if new:
            f.write(b"".join(new))
        f.flush()
        os.fsync(f.fileno())
        f.seek(0)
        data = f.read()
    atomic_write_json(idx_json, {"archive_offset": consumed})
    return {data[i:i + 16] for i in range(0, len(data) // 16 * 16, 16)}


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of data to fd. A whole record is one os.write call in the normal case."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("removed-lines archive: write made no progress")
        view = view[n:]


def append_removed_records(records: list, *, archive: Path | None = None,
                           read_block: int | None = None) -> tuple:
    """Archive each removed record once, under the archive lock. Returns (written, skipped).

    records: dicts with source_file (str), source_line (int), reason (str) and content (str). The
    content may hold surrogates from surrogateescape decoding; those bytes are written back exactly.
    A record is known when its digest (source file and content) is already in the archive, or was
    written earlier in this call. Known records are skipped and counted.

    The lock is held from the index refresh to the last write. Each record is one binary write with
    O_APPEND, so records from concurrent processes never share a line. Raises RuntimeError when the
    lock cannot be taken within 60 s, and OSError when a write fails. Either way the caller must
    leave the transcript untouched.

    archive and read_block default to REMOVED_LINES_ARCHIVE and INDEX_READ_BLOCK. api_repair_v2
    passes its own, so tests that set them on that module still apply.
    """
    archive = REMOVED_LINES_ARCHIVE if archive is None else Path(archive)
    read_block = INDEX_READ_BLOCK if read_block is None else read_block
    lock = lock_wait(REMOVED_LOCK_NAME, timeout=60)
    if lock is None:
        raise RuntimeError("the removed-lines archive is locked by another process")
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat()
        known = _refresh_removed_index(archive, read_block)
        written = skipped = 0
        new_digests, offset = [], None
        # O_BINARY stops the C runtime from turning each LF into CRLF on Windows.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(str(archive), flags, 0o644)
        try:
            for rec in records:
                digest = _record_digest(rec["source_file"], rec["content"])
                if digest in known:
                    skipped += 1
                    continue
                out = {"archived_at": stamp, "source_file": rec["source_file"],
                       "source_line": rec["source_line"], "reason": rec["reason"], "content": rec["content"]}
                _write_all(fd, json.dumps(out, ensure_ascii=False).encode("utf-8", "surrogateescape") + b"\n")
                offset = os.fstat(fd).st_size     # right after our own last write, never a later stat
                known.add(digest)
                new_digests.append(digest)
                written += 1
            os.fsync(fd)
        finally:
            os.close(fd)
        if new_digests:
            idx_json, idx_bin = _removed_index_paths()
            with open(idx_bin, "ab") as f:
                f.write(b"".join(new_digests))
                f.flush()
                os.fsync(f.fileno())
            atomic_write_json(idx_json, {"archive_offset": offset})
        return written, skipped
    finally:
        lock.release()
