#!/usr/bin/env python3
"""
Session Archive v2 -- append-only mirror of Claude Code transcripts. Stdlib only.

A new file beside session_archive.py. The live tool is unchanged. This version
does not rewrite archive files (see Invariants), reads only the bytes a sync needs,
and writes in binary. Usage rows are not handled here (PR-06).

Under SESSION_GUARD_HOME/session-archive/ (default ~/.claude/session-archive/):

  transcripts/<rel>                        the archive of one transcript. It only grows.
  state/<rel>.json                         live_offset, head_sha, tail_sha, archive_size.
  state/excused.json, state/excused.bin     verify's index of the removed-lines archive (PR-07)
  quarantine/<rel>.<stamp>.partial         archive bytes beyond the recorded size (crash path).
  quarantine/<rel>.<stamp>.before-restore  a live file's bytes before restore --merge-live.

Invariants:
  1. The archive never loses a line. A line cut by the crash path is in quarantine.
  2. The archive file is never rewritten. It only grows, by appends. The one exception
     is the crash path below.
  3. Every write is binary, and every append ends on a complete line.
  4. If the state does not match what was recorded, sync falls back to a line-identity
     full merge. That merge also only appends, except for the crash-path cut.

Crash path: archive bytes beyond the recorded archive_size are copied to quarantine,
fsynced, and only then cut. This never removes recorded content; it removes only bytes
that were never recorded as archived, after copying them to quarantine.

Restore: every write is binary and goes through a temporary file, fsync and os.replace.
  - An archive-only session is created with its archive bytes, byte for byte. The target
    must not exist.
  - A live file that is missing archived lines is reported as "shrunken, not touched"
    unless --merge-live is given.
  - With --merge-live, a file is skipped if it was modified in the last hour, if its
    session id is on a running claude command line, or if the process list cannot be
    read. Otherwise its current bytes are copied to quarantine (before-restore), and the
    file is replaced by the archive bytes followed by the live lines that are not in the
    archive, raw.

The state says how many live bytes are safely archived (live_offset), the
checksums of the first and last 4 KB of that prefix, and the archive size right
after our last append.

  Fast path (the state matches): read the new live bytes and at most 8 KB of
  checksums, append the complete new lines, update the state. The archive is
  not read.

  Full merge (no state, or the state does not match): read the whole live file
  and archive, append every line whose identity (uuid, else raw bytes) is not in
  the archive, recompute the state. This is the fallback, so it may parse lines.

Commands:
    sync --all                 every transcript under projects/ (the default)
    sync --transcript <path>   one session's transcript and its subagent transcripts
    verify, restore, status    as in session_archive.py; status renames the ledger field
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import guardkit

CLAUDE_DIR = Path(os.environ.get("SESSION_GUARD_HOME") or (Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
ARCHIVE_DIR = CLAUDE_DIR / "session-archive"
TRANSCRIPTS_DIR = ARCHIVE_DIR / "transcripts"
STATE_DIR = ARCHIVE_DIR / "state"
QUARANTINE_DIR = ARCHIVE_DIR / "quarantine"
USAGE_LEDGER = ARCHIVE_DIR / "usage-ledger.jsonl"
HASH_WINDOW = 4096      # bytes hashed at each end of the archived prefix
LOCK_TIMEOUT = 60.0     # seconds to wait for one transcript's lock before skipping it


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_at(f, offset: int, n: int) -> bytes:
    f.seek(offset)
    return f.read(n)


def line_key(raw: bytes):
    """Identity of one line (its bytes without the newline): the uuid when the line is a
    JSON object with one, else the raw bytes with a trailing carriage return removed."""
    text = raw[:-1] if raw.endswith(b"\r") else raw
    try:
        entry = json.loads(raw)
    except ValueError:              # JSONDecodeError and UnicodeDecodeError are both ValueError
        return ("raw", text)
    uid = entry.get("uuid") if isinstance(entry, dict) else None
    if isinstance(uid, str) and uid:
        return ("uuid", uid)
    return ("raw", text)


def _load_state(path: Path):
    """The state dict, or None when the file is missing or malformed (the full merge then runs)."""
    obj = guardkit.load_json(path, None)
    if not isinstance(obj, dict):
        return None
    lo, size = obj.get("live_offset"), obj.get("archive_size")
    if (type(lo) is int and lo >= 0 and type(size) is int and size >= 0
            and isinstance(obj.get("head_sha"), str) and isinstance(obj.get("tail_sha"), str)):
        return obj
    return None


def _save_state(path: Path, live_offset: int, head: bytes, tail: bytes, archive_size: int) -> None:
    guardkit.atomic_write_json(path, {
        "live_offset": live_offset,
        "head_sha": _sha(head),
        "tail_sha": _sha(tail),
        "archive_size": archive_size,
    })


def _quarantine(rel: str, data: bytes, kind: str = "partial") -> Path:
    """Copy bytes into quarantine under <rel>.<stamp>.<kind>, and fsync. Never overwrites."""
    rp = Path(rel)
    qdir = QUARANTINE_DIR / rp.parent
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    qpath = qdir / f"{rp.name}.{stamp}.{kind}"
    with open(qpath, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return qpath


def _truncate(path: Path, size: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())


def _fast_path(live: Path, arch: Path, state_path: Path, state: dict, size: int):
    """Append the new complete lines. Returns (lines, bytes), or None when the fast path does not apply."""
    off, a_size = state["live_offset"], state["archive_size"]
    if size < off:
        return None                                 # the live file shrank
    try:
        if os.stat(arch).st_size != a_size:
            return None                             # the archive changed since our last append
    except FileNotFoundError:
        return None
    k = min(HASH_WINDOW, off)
    with open(live, "rb") as f:
        head = _read_at(f, 0, k)
        tail = _read_at(f, off - k, k)
        if (len(head) != k or len(tail) != k or _sha(head) != state["head_sha"]
                or _sha(tail) != state["tail_sha"]):
            return None                             # the prefix we archived has changed
        new = _read_at(f, off, size - off)
    new = new[:new.rfind(b"\n") + 1]                # complete lines only
    if not new:
        return 0, 0
    with open(arch, "ab") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
        new_size = f.tell()                         # append mode: this is the end of the file
    new_off = off + len(new)
    k2 = min(HASH_WINDOW, new_off)
    # New checksums come from bytes already in memory, not from a second read of the live
    # file, so they describe exactly what was appended.
    _save_state(state_path, new_off, (head + new)[:k2],
                (tail + new)[len(tail) + len(new) - k2:], new_size)
    return new.count(b"\n"), len(new)


def _full_merge(live: Path, arch: Path, rel: str, state_path: Path, state):
    """Append every line whose identity is not archived yet, then recompute the state. Returns (lines, bytes)."""
    data = live.read_bytes()                        # FileNotFoundError propagates to the caller
    lo = data.rfind(b"\n") + 1
    complete = data[:lo]
    live_lines = complete[:-1].split(b"\n") if lo else []
    try:
        arch_data = arch.read_bytes()
    except FileNotFoundError:
        arch_data = b""
    # Keep the archive up to its recorded size (or its last newline). Quarantine the rest first.
    limit = len(arch_data)
    if state is not None and state["archive_size"] < limit:
        limit = state["archive_size"]
    keep = arch_data.rfind(b"\n", 0, limit) + 1
    if keep < len(arch_data):
        _quarantine(rel, arch_data[keep:])
        _truncate(arch, keep)
    have = {line_key(ln) for ln in arch_data[:keep - 1].split(b"\n")} if keep else set()
    adds = [ln for ln in live_lines if line_key(ln) not in have]
    new_size = keep
    if adds:
        arch.parent.mkdir(parents=True, exist_ok=True)
        payload = b"".join(ln + b"\n" for ln in adds)
        with open(arch, "ab") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            new_size = f.tell()
    k = min(HASH_WINDOW, lo)
    _save_state(state_path, lo, complete[:k], complete[lo - k:], new_size)
    return len(adds), sum(len(ln) + 1 for ln in adds)


def _sync_locked(live: Path, rel: str):
    arch = TRANSCRIPTS_DIR / rel
    state_path = STATE_DIR / f"{rel}.json"
    state = _load_state(state_path)
    done = None
    if state is not None:
        done = _fast_path(live, arch, state_path, state, os.stat(live).st_size)
    if done is None:
        done = _full_merge(live, arch, rel, state_path, state)
    return done


def sync_transcript(live: Path, rel: str):
    """Sync one live transcript. rel is its path under projects/, with forward slashes.
    Returns (status, lines, bytes); status is "ok", "skipped" (lock busy) or "missing"."""
    lock = guardkit.lock_wait("session-archive-" + rel.replace("/", "__"), timeout=LOCK_TIMEOUT)
    if lock is None:
        return "skipped", 0, 0
    try:
        lines, nbytes = _sync_locked(live, rel)
        return "ok", lines, nbytes
    except FileNotFoundError:
        return "missing", 0, 0
    finally:
        lock.release()


def _sync_paths(pairs) -> int:
    """Sync (rel, path) pairs one at a time, then print the summary line."""
    scanned = archived = added_lines = errors = 0
    for rel, path in pairs:
        if "acompact" in str(path):
            continue
        scanned += 1
        try:
            status, lines, _ = sync_transcript(path, rel)
        except Exception as exc:                    # one bad file must not stop the sweep
            errors += 1
            print(f"[session-archive] error on {rel}: {exc!r}")
            continue
        if status == "skipped":
            print(f"[session-archive] skipped {rel}: lock busy, retried on the next sync")
        elif status == "ok" and lines:
            archived += 1
            added_lines += lines
    print(f"[session-archive] scanned {scanned} live transcripts; "
          f"archived {archived} ({added_lines} new lines); +0 usage rows")
    return 1 if errors else 0


def cmd_sync_all() -> int:
    return _sync_paths((rel, p) for rel, p, *_ in guardkit.transcript_files(PROJECTS_DIR))


def cmd_sync_transcript(path: str) -> int:
    live = Path(os.path.abspath(path))
    try:
        live.relative_to(PROJECTS_DIR)
    except ValueError:
        print(f"[session-archive] not a transcript under projects/: {path}")
        return 2
    pairs = [(p.relative_to(PROJECTS_DIR).as_posix(), p) for p in guardkit.session_transcripts(live)]
    return _sync_paths(pairs)


# ---- verify, restore and status: ported from session_archive.py (text-mode helpers, unchanged logic) ----

def _text_key(text: str):
    """Line identity for verify and restore: uuid when present, else the exact text."""
    try:
        entry = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ("raw", text)
    uid = entry.get("uuid")
    return ("uuid", uid) if uid else ("raw", text)


def _read_text_lines(path: Path) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        return []


def iter_live_transcripts():
    """(relative_path, absolute_path) for every transcript under projects/, subagents included."""
    if not PROJECTS_DIR.exists():
        return
    for jsonl_file in PROJECTS_DIR.rglob("*.jsonl"):
        if jsonl_file.is_file() and "acompact" not in str(jsonl_file):
            yield jsonl_file.relative_to(PROJECTS_DIR), jsonl_file


def deliberately_removed_keys() -> set:
    """Line identities that a repair tool removed on purpose, from the permanent removed-lines archive."""
    keys = set()
    archive = CLAUDE_DIR / "backups" / "sessions" / "removed-lines-archive.jsonl"
    if not archive.exists():
        return keys
    try:
        with open(archive, "r", encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                content = rec.get("content")
                if not isinstance(content, str):
                    continue
                # Raw and stripped forms are both keyed: the archiver stores the stripped line.
                keys.add(_text_key(content))
                stripped = content.strip()
                if stripped != content:
                    keys.add(_text_key(stripped))
    except Exception:
        pass
    return keys


# ---- verify: excused-line index and the three-class canary (PR-07; F11, F20, F21) ----------
# Each line that the archive has and the live file lacks is classed exactly once:
#   excused             a readable removal record matches it: by uuid, or (no uuid) by source file and exact text
#   excused_unreadable  no readable record matches, but a removal record that does not parse contains the
#                       line's uuid as a token. Counted apart: not a loss, but the audit record is damaged.
#   unexplained         neither of the above. This is the only class that drives the canary.
# The index of the removed-lines archive is kept in ARCHIVE_DIR/state. It is refreshed from its recorded
# offset, so a verify reads only the bytes appended since the previous one.

ENTRY_SIZE = 17                                 # one index entry: a kind byte, then a 16-byte digest
KIND_RECORD, KIND_UUID, KIND_UNREADABLE = 0, 1, 2
EXCUSED_READ_BLOCK = 8 * 1024 * 1024            # the removed-lines archive is streamed in blocks this size
UUID_TOKEN_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _excused_paths():
    """(excused.json, excused.bin) in the archive state folder, resolved at call time."""
    state = ARCHIVE_DIR / "state"
    return state / "excused.json", state / "excused.bin"


def _removed_archive_path() -> Path:
    return CLAUDE_DIR / "backups" / "sessions" / "removed-lines-archive.jsonl"


def _digest(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()


def _uuid_digest(uid: str) -> bytes:
    return _digest(uid.encode("utf-8", "surrogateescape"))


def _index_lines(lines, out: list) -> int:
    """Append one index entry per key found on these raw lines of the removed-lines archive.
    Returns how many of the lines do not parse."""
    unreadable = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "surrogateescape"))
        except ValueError:
            unreadable += 1
            for m in UUID_TOKEN_RE.finditer(raw):      # a single pass over the raw bytes
                out.append(bytes([KIND_UNREADABLE]) + _digest(m.group(0)))
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("content"), str):
            continue
        content = rec["content"]
        if isinstance(rec.get("source_file"), str):
            # The key of guardkit's removed-lines index (PR-09). The raw and the stripped form are both
            # keyed, because the archiver stores the stripped line.
            out.append(bytes([KIND_RECORD]) + guardkit._record_digest(rec["source_file"], content))
            stripped = content.strip()
            if stripped != content:
                out.append(bytes([KIND_RECORD]) + guardkit._record_digest(rec["source_file"], stripped))
        try:
            inner = json.loads(content)
        except ValueError:
            continue
        uid = inner.get("uuid") if isinstance(inner, dict) else None
        if isinstance(uid, str) and uid:
            out.append(bytes([KIND_UUID]) + _uuid_digest(uid))
    return unreadable


def _split_entries(blob: bytes) -> dict:
    sets = {KIND_RECORD: set(), KIND_UUID: set(), KIND_UNREADABLE: set()}
    for i in range(0, len(blob) - ENTRY_SIZE + 1, ENTRY_SIZE):
        s = sets.get(blob[i])
        if s is not None:
            s.add(blob[i + 1:i + ENTRY_SIZE])
    return {"record": sets[KIND_RECORD], "uuid": sets[KIND_UUID], "unreadable_uuid": sets[KIND_UNREADABLE]}


def refresh_excused_index() -> dict:
    """Bring the index up to date with the removed-lines archive and return its key sets.

    Reads only the bytes after the recorded archive offset, and only complete lines. The index is rebuilt
    from the start when the archive is shorter than that offset, or when the recorded state and excused.bin
    disagree. excused.bin is appended and fsynced first, and excused.json is replaced last, so a killed run
    leaves the recorded state at the last complete refresh. Returns the three digest sets under "record",
    "uuid" and "unreadable_uuid", and the total number of unreadable removal records under "unreadable_records".
    """
    archive = _removed_archive_path()
    idx_json, idx_bin = _excused_paths()
    state = guardkit.load_json(idx_json, None)
    if not isinstance(state, dict):
        state = {}
    off, recorded = state.get("archive_offset"), state.get("bin_bytes")
    unreadable_total = state.get("unreadable_records")
    try:
        size = archive.stat().st_size
    except FileNotFoundError:
        size = 0
    try:
        bin_size = idx_bin.stat().st_size
    except FileNotFoundError:
        bin_size = 0
    usable = (type(off) is int and type(recorded) is int and type(unreadable_total) is int
              and 0 <= off <= size and 0 <= recorded <= bin_size and recorded % ENTRY_SIZE == 0)
    if usable:
        base = b""
        if recorded:
            with open(idx_bin, "rb") as f:
                base = f.read(recorded)
    else:
        off, recorded, unreadable_total, base = 0, 0, 0, b""
    new, unreadable_new, consumed = [], 0, off
    if size > off:
        with open(archive, "rb") as f:
            f.seek(off)
            carry = b""
            while True:
                block = f.read(EXCUSED_READ_BLOCK)
                if not block:
                    break
                buf = carry + block
                cut = buf.rfind(b"\n") + 1              # complete lines only
                if cut == 0:
                    carry = buf
                    continue
                unreadable_new += _index_lines(buf[:cut].split(b"\n"), new)
                consumed += cut
                carry = buf[cut:]
    blob = b"".join(new)
    if not usable or blob or consumed != off or bin_size != recorded:
        idx_bin.parent.mkdir(parents=True, exist_ok=True)
        with open(idx_bin, "ab") as f:
            f.truncate(recorded)                        # drop entries that a killed run left past the recorded state
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        guardkit.atomic_write_json(idx_json, {
            "archive_offset": consumed,
            "bin_bytes": recorded + len(blob),
            "unreadable_records": unreadable_total + unreadable_new,
        })
    return {**_split_entries(base + blob), "unreadable_records": unreadable_total + unreadable_new}


def _verify_key(text: str):
    """Line identity for verify: the uuid when the line is a JSON object with one, else the exact text.
    Unlike _text_key, a line that is valid JSON but not an object is a raw line, not an error."""
    try:
        entry = json.loads(text)
    except ValueError:
        return ("raw", text)
    uid = entry.get("uuid") if isinstance(entry, dict) else None
    return ("uuid", uid) if uid else ("raw", text)


def _classify_missing(key, text: str, live: Path, idx: dict) -> str:
    """excused, excused_unreadable or unexplained, for one archived line that the live file lacks."""
    if key[0] == "uuid":
        uid = key[1]
        if isinstance(uid, str):
            d = _uuid_digest(uid)
            if d in idx["uuid"]:
                return "excused"
            if d in idx["unreadable_uuid"]:
                return "excused_unreadable"
        return "unexplained"
    # A line without a uuid cannot be searched for in an unreadable record (class 2 does not apply).
    # Class 1 needs a readable record for this source file with the exact text.
    if guardkit._record_digest(str(live), text) in idx["record"]:
        return "excused"
    return "unexplained"


def cmd_verify() -> int:
    """The canary. Exits 0 only when every archived line that is missing from its live copy has an explanation."""
    live_index = {str(r): p for r, p in iter_live_transcripts()}
    archived = [q for q in TRANSCRIPTS_DIR.rglob("*.jsonl")] if TRANSCRIPTS_DIR.exists() else []
    idx = refresh_excused_index()

    only_archived = []
    shrunk = []
    classes = {"excused": 0, "excused_unreadable": 0, "unexplained": 0}
    for arch in archived:
        rel = arch.relative_to(TRANSCRIPTS_DIR)
        live = live_index.get(str(rel))
        if live is None:
            only_archived.append(arch)
            continue
        l_keys = {_verify_key(ln) for ln in _read_text_lines(live)}
        missing = {}                                    # key -> the archived text of that line
        for ln in _read_text_lines(arch):
            key = _verify_key(ln)
            if key not in l_keys and key not in missing:
                missing[key] = ln
        unexplained = 0
        for key, text in missing.items():
            cls = _classify_missing(key, text, live, idx)
            classes[cls] += 1
            if cls == "unexplained":
                unexplained += 1
        if unexplained:
            shrunk.append((str(rel), unexplained))

    print(f"[session-archive] archived sessions: {len(archived)}")
    print(f"[session-archive] live sessions:     {len(live_index)}")
    print(f"[session-archive] present ONLY in archive (recoverable): {len(only_archived)}")
    for p in only_archived[:20]:
        print(f"    {p.relative_to(TRANSCRIPTS_DIR)}")
    if len(only_archived) > 20:
        print(f"    ... and {len(only_archived) - 20} more")
    print(f"[session-archive] live files missing archived lines: {len(shrunk)}"
          "   <-- the canary: anything above 0 is unexplained data loss")
    for name, n in shrunk[:20]:
        print(f"    {name}: {n} unexplained lines, only in archive")
    if len(shrunk) > 20:
        print(f"    ... and {len(shrunk) - 20} more")
    if classes["excused"]:
        print(f"[session-archive] ({classes['excused']} further missing lines were removed "
              "deliberately by a repair tool and are archived, so not counted above)")
    print(f"[session-archive] missing lines excused by an unreadable removal record: {classes['excused_unreadable']}"
          " (the record does not parse; its uuid is still in it)")
    print(f"[session-archive] missing lines unexplained: {classes['unexplained']}")
    print(f"[session-archive] unreadable removal records found: {idx['unreadable_records']}")
    return 0 if classes["unexplained"] == 0 else 1


RESTORE_QUIET_SECONDS = 3600    # restore leaves a live file alone if it changed more recently than this
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _running_ids_from_text(text: str) -> set:
    """Lower-case session ids found on the lines of a process list that mention claude."""
    ids = set()
    for line in text.splitlines():
        if "claude" in line.lower():
            ids.update(m.group(0).lower() for m in UUID_RE.finditer(line))
    return ids


def running_session_ids():
    """Session ids on the command line of a running claude process. None when the process
    list cannot be read, and restore then touches nothing."""
    if os.name == "nt":
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"]
    else:
        cmd = ["ps", "-eww", "-o", "args="]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _running_ids_from_text(out.decode("utf-8", "replace"))


class _Lazy:
    """Calls fn at most once, the first time its value is needed."""

    def __init__(self, fn):
        self._fn, self._done, self._value = fn, False, None

    def __call__(self):
        if not self._done:
            self._value, self._done = self._fn(), True
        return self._value


def _session_of(rel: str) -> str:
    """Owning session id (lower case) of a transcript, by its path under projects/."""
    parts = rel.split("/")
    if len(parts) >= 3 and "subagents" in parts:
        return parts[1].lower()
    return Path(parts[-1]).stem.lower()


def _complete_lines(data: bytes) -> list:
    """The complete lines of data, without their newlines. A trailing partial line is left out."""
    end = data.rfind(b"\n") + 1
    return data[:end - 1].split(b"\n") if end else []


def _merge_one(rel: str, arch_bytes: bytes, live: Path, merge_live: bool, running: _Lazy) -> str:
    live_bytes = live.read_bytes()
    st = os.stat(live)
    arch_lines = _complete_lines(arch_bytes)
    live_lo = live_bytes.rfind(b"\n") + 1
    live_lines = _complete_lines(live_bytes[:live_lo])
    live_tail = live_bytes[live_lo:]
    live_keys = {line_key(ln) for ln in live_lines}
    arch_keys = {line_key(ln) for ln in arch_lines}
    missing = [ln for ln in arch_lines if line_key(ln) not in live_keys]
    if not missing:
        return "none"
    if arch_bytes[-1:] != b"\n":
        print(f"[session-archive] archive ends in an incomplete line, not restored: {rel}")
        return "skipped"
    if not merge_live:
        print(f"[session-archive] shrunken, not touched: {rel} ({len(missing)} lines only in archive)")
        return "untouched"
    if time.time() - st.st_mtime < RESTORE_QUIET_SECONDS:
        print(f"[session-archive] modified in the last hour, not touched: {rel}")
        return "skipped"
    ids = running()
    if ids is None:
        print(f"[session-archive] could not read the process list, not touched: {rel}")
        return "skipped"
    if _session_of(rel) in ids:
        print(f"[session-archive] session is running, not touched: {rel}")
        return "skipped"
    extra = [ln for ln in live_lines if line_key(ln) not in arch_keys]
    merged = arch_bytes + b"".join(ln + b"\n" for ln in extra) + live_tail
    now = os.stat(live)
    if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        print(f"[session-archive] changed while restoring, not touched: {rel}")
        return "skipped"
    _quarantine(rel, live_bytes, "before-restore")
    guardkit.write_bytes_atomic(live, merged)
    print(f"[session-archive] merged {len(missing)} lines back into {rel}; copy kept in quarantine")
    return "merged"


def _restore_one(rel: str, merge_live: bool, running: _Lazy) -> str:
    """Restore one archived transcript. Returns restored, merged, untouched, skipped or none."""
    arch = TRANSCRIPTS_DIR / rel
    live = PROJECTS_DIR / rel
    lock = guardkit.lock_wait("session-archive-" + rel.replace("/", "__"), timeout=LOCK_TIMEOUT)
    if lock is None:
        print(f"[session-archive] skipped {rel}: lock busy, retried on the next restore")
        return "skipped"
    try:
        arch_bytes = arch.read_bytes()
        if not arch_bytes:
            return "none"
        if not os.path.lexists(live):
            guardkit.write_bytes_atomic(live, arch_bytes)   # byte for byte; the target did not exist
            return "restored"
        return _merge_one(rel, arch_bytes, live, merge_live, running)
    finally:
        lock.release()


def cmd_restore(merge_live: bool = False, running_ids=running_session_ids) -> int:
    """Create archive-only sessions. With merge_live, also merge missing lines into shrunken live files."""
    running = _Lazy(running_ids)
    counts = {"restored": 0, "merged": 0, "untouched": 0, "skipped": 0, "none": 0}
    errors = 0
    if TRANSCRIPTS_DIR.exists():
        for arch in sorted(TRANSCRIPTS_DIR.rglob("*.jsonl")):
            rel = arch.relative_to(TRANSCRIPTS_DIR).as_posix()
            try:
                counts[_restore_one(rel, merge_live, running)] += 1
            except Exception as exc:                # one bad file must not stop the restore
                errors += 1
                print(f"[session-archive] error on {rel}: {exc!r}")
    print(f"[session-archive] restored {counts['restored']} missing sessions, "
          f"merged lines back into {counts['merged']} shrunken transcripts")
    if counts["untouched"]:
        print(f"[session-archive] {counts['untouched']} shrunken transcripts not touched "
              "(run restore --merge-live to merge them)")
    return 1 if errors else 0


def cmd_status() -> int:
    archived = [q for q in TRANSCRIPTS_DIR.rglob("*.jsonl")] if TRANSCRIPTS_DIR.exists() else []
    total_bytes = sum(p.stat().st_size for p in archived) if archived else 0
    ledger_rows = 0
    ledger_raw_tokens = 0
    if USAGE_LEDGER.exists():
        with open(USAGE_LEDGER, "r", encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ledger_rows += 1
                try:
                    u = json.loads(line).get("usage", {})
                except (json.JSONDecodeError, ValueError):
                    continue
                ledger_raw_tokens += sum(v for v in u.values() if isinstance(v, int))
    live = sum(1 for _ in iter_live_transcripts())
    print(json.dumps({
        "archived_sessions": len(archived),
        "live_sessions": live,
        "archive_bytes": total_bytes,
        "archive_mb": round(total_bytes / 1048576, 1),
        "usage_ledger_rows": ledger_rows,
        "usage_ledger_raw_per_line_tokens": ledger_raw_tokens,
        "usage_ledger_note": ("raw per-line sum: a message repeats its usage on every split line, "
                              "so this is not a usage total"),
        "archive_dir": str(ARCHIVE_DIR),
    }, indent=2))
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "sync"
    if mode == "sync" and args[1:] in ([], ["--all"]):
        return cmd_sync_all()
    if mode == "sync" and len(args) == 3 and args[1] == "--transcript":
        return cmd_sync_transcript(args[2])
    if mode == "verify":
        return cmd_verify()
    if mode == "restore" and args[1:] in ([], ["--merge-live"]):
        return cmd_restore(merge_live=args[1:] == ["--merge-live"])
    if mode == "status":
        return cmd_status()
    print(f"Usage: {sys.argv[0]} [sync [--all | --transcript <path>]|verify|restore [--merge-live]|status]")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
