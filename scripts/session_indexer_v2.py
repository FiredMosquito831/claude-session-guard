#!/usr/bin/env python3
"""
session_indexer_v2 -- append-only merge of the parallel prompt index into history.jsonl.

PR-08 replaces the rewrite-in-place merge in session_indexer.py (F9). That merge read the
whole history, built a full copy and replaced the file, so a prompt that Claude Code
appended in between was lost. This module never rewrites history.jsonl. It appends complete
lines with O_APPEND, under the lock `session-index`, and only after checking that the bytes
it has already indexed have not changed.

State, in guardkit.STATE_DIR:
  index_state.json   {"history_offset", "history_head_sha", "tail_sha", "keys_file"}
                     head_sha: sha256 of the first min(4096, offset) bytes
                     tail_sha: sha256 of the min(4096, offset) bytes that end at offset
  index_keys.bin     one 16-byte BLAKE2b digest per known key; only ever appended to
  index_last_merge   time of the last merge attempt that got the lock (the throttle)

A key is (sessionId, timestamp, display[:500]). history_offset is the length of the prefix
of history.jsonl that has been indexed; it always ends just after a newline. Lines after it,
whether written by Claude Code or by this module, are read and indexed on the next merge.

Stdlib only, plus guardkit for the lock, the state folder and the hook payload reader.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guardkit as gk  # noqa: E402

CLAUDE_DIR = gk.CLAUDE_DIR
OFFICIAL_HISTORY = CLAUDE_DIR / "history.jsonl"
PARALLEL_INDEX = CLAUDE_DIR / ".session_index.jsonl"
PROJECTS_DIR = gk.PROJECTS_DIR
STATE_DIR = gk.STATE_DIR
STATE_FILE = STATE_DIR / "index_state.json"
KEYS_NAME = "index_keys.bin"
KEYS_FILE = STATE_DIR / KEYS_NAME
THROTTLE_FILE = STATE_DIR / "index_last_merge"
LOCK_NAME = "session-index"
LOCK_TIMEOUT = 30.0
DEFAULT_MIN_INTERVAL = 300.0
HEAD_BYTES = 4096
DIGEST_SIZE = 16
NO_SESSION_MSG = "[session-index] register: no session in payload; nothing to do"


class MergeResult(NamedTuple):
    status: str   # ok | busy | refused | throttled | deferred | error | noop
    added: int    # lines this call appended to history.jsonl
    detail: str


# --- keys and lines -----------------------------------------------------------

def entry_key(entry: dict) -> tuple:
    """(sessionId, timestamp, display[:500]): the identity of one prompt."""
    display = entry.get("display", "") or ""
    if not isinstance(display, str):
        display = str(display)
    return (entry.get("sessionId", ""), entry.get("timestamp", 0), display[:500])


def key_digest(entry: dict) -> bytes:
    """16-byte BLAKE2b over sessionId, timestamp and display[:500], joined by NUL."""
    sid, ts, display = entry_key(entry)
    text = f"{sid}\0{ts}\0{display}"
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # a lone surrogate from a \uD8xx escape in the JSON
        raw = text.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(raw, digest_size=DIGEST_SIZE).digest()


def encode_line(entry: dict) -> bytes:
    """One JSON line with ensure_ascii=False, as bytes and ending in \n."""
    try:
        return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # lone surrogate: fall back to ASCII escapes
        return (json.dumps(entry, ensure_ascii=True) + "\n").encode("ascii")


def parse_line(raw: bytes):
    """Parse one non-blank line. Returns the JSON object, or None if it is not one."""
    try:
        entry = json.loads(raw.decode("utf-8", "surrogateescape"))
    except ValueError:  # JSONDecodeError is a ValueError
        return None
    return entry if isinstance(entry, dict) else None


# --- file helpers -------------------------------------------------------------

def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _head_sha(path: Path, offset: int) -> str:
    """sha256 of the first min(HEAD_BYTES, offset) bytes of the file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(min(HEAD_BYTES, offset)))
    except FileNotFoundError:
        pass
    return h.hexdigest()


def _tail_sha(path: Path, offset: int) -> str:
    """sha256 of the min(HEAD_BYTES, offset) bytes that end at `offset`."""
    n = min(HEAD_BYTES, offset)
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            f.seek(offset - n)
            h.update(f.read(n))
    except FileNotFoundError:
        pass
    return h.hexdigest()


def _read_complete_lines(path: Path, start: int):
    """Bytes from `start` through the last newline, and the offset just after them.

    A final partial line (a writer still in the middle of one) is left for the next merge.
    """
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return b"", start
    cut = data.rfind(b"\n") + 1
    return data[:cut], start + cut


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _append_bytes(path: Path, chunks: list) -> None:
    """Append each chunk with its own os.write call on an O_APPEND fd, then fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_BINARY: on Windows os.open is text mode by default, which turns every \n into \r\n.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        for chunk in chunks:
            _write_all(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_keys(path: Path):
    """Digests from the keys file as a set. None if the file is missing or torn."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(data) % DIGEST_SIZE:
        return None
    return {data[i:i + DIGEST_SIZE] for i in range(0, len(data), DIGEST_SIZE)}


def _load_state():
    st = gk.load_json(STATE_FILE, None)
    if not isinstance(st, dict):
        return None
    offset, head, tail = st.get("history_offset"), st.get("history_head_sha"), st.get("tail_sha")
    if not isinstance(offset, int) or offset < 0 or not isinstance(head, str) or not isinstance(tail, str):
        return None
    return st


def _save_state(offset: int) -> None:
    gk.atomic_write_json(STATE_FILE, {
        "history_offset": offset,
        "history_head_sha": _head_sha(OFFICIAL_HISTORY, offset),
        "tail_sha": _tail_sha(OFFICIAL_HISTORY, offset),
        "keys_file": KEYS_NAME,
    })


def _recently_merged(min_interval: float) -> bool:
    try:
        last = float(THROTTLE_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return 0 <= time.time() - last < min_interval


def _stamp_merge_attempt() -> None:
    try:
        gk.write_bytes_atomic(THROTTLE_FILE, str(time.time()).encode("ascii"))
    except OSError:
        pass


# --- merge --------------------------------------------------------------------

def _catch_up(keys: set, offset: int):
    """Index the complete lines of history.jsonl after `offset`.

    Returns (new_offset, fresh, unparseable). Each digest not already in `keys` is added to
    `keys` and listed in `fresh`, in file order, so the caller can persist it.
    """
    blob, new_offset = _read_complete_lines(OFFICIAL_HISTORY, offset)
    fresh, bad = [], 0
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        entry = parse_line(raw)
        if entry is None:
            bad += 1
            continue
        d = key_digest(entry)
        if d not in keys:
            keys.add(d)
            fresh.append(d)
    return new_offset, fresh, bad


def _pending_lines(keys: set):
    """Parallel-index entries whose key is not in `keys`, as lines. Returns (lines, unparseable)."""
    try:
        blob = PARALLEL_INDEX.read_bytes()
    except FileNotFoundError:
        return [], 0
    lines, seen, bad = [], set(), 0
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        entry = parse_line(raw)
        if entry is None:
            bad += 1
            continue
        d = key_digest(entry)
        if d in keys or d in seen:
            continue
        seen.add(d)
        lines.append(encode_line(entry))
    return lines, bad


def _merge_locked() -> MergeResult:
    size = _file_size(OFFICIAL_HISTORY)
    state = _load_state()
    keys = _load_keys(KEYS_FILE) if state is not None else None
    notes = []
    bad = 0

    if state is None or keys is None or state["history_offset"] > size:
        # The one full read of history.jsonl: no usable state, or the state is ahead of the file.
        keys = set()
        offset, fresh, bad = _catch_up(keys, 0)
        gk.write_bytes_atomic(KEYS_FILE, b"".join(fresh))
        notes.append("state rebuilt from history.jsonl")
    else:
        offset = state["history_offset"]
        # Invariant 5: the indexed prefix must still hold its first and its last bytes.
        if (_head_sha(OFFICIAL_HISTORY, offset) != state["history_head_sha"]
                or _tail_sha(OFFICIAL_HISTORY, offset) != state["tail_sha"]):
            return MergeResult("refused", 0,
                               "history.jsonl changed outside the indexer; nothing written")
        offset, fresh, bad = _catch_up(keys, offset)
        if fresh:
            _append_bytes(KEYS_FILE, [b"".join(fresh)])
    _save_state(offset)

    lines, bad_parallel = _pending_lines(keys)
    bad += bad_parallel
    added = 0
    if lines:
        if _file_size(OFFICIAL_HISTORY) > offset:
            return MergeResult("deferred", 0,
                               "history.jsonl ends with a partial line; retry on the next merge")
        _append_bytes(OFFICIAL_HISTORY, lines)
        added = len(lines)
        # Index what we just wrote, and anything another writer appended meanwhile. Reading the
        # bytes back is what keeps the offset exact; a size taken after the write is not.
        offset, fresh, bad_tail = _catch_up(keys, offset)
        bad += bad_tail
        if fresh:
            _append_bytes(KEYS_FILE, [b"".join(fresh)])
        _save_state(offset)

    if bad:
        notes.append(f"{bad} unparseable line(s) skipped")
    notes.append(f"{len(keys)} known keys")
    return MergeResult("ok", added, "; ".join(notes))


def merge_history(min_interval_seconds: float = DEFAULT_MIN_INTERVAL,
                  lock_timeout: float = LOCK_TIMEOUT) -> MergeResult:
    """Append the parallel-index prompts that history.jsonl lacks. Never rewrites history.jsonl."""
    if min_interval_seconds > 0 and _recently_merged(min_interval_seconds):
        return MergeResult("throttled", 0,
                           f"last merge attempt was under {min_interval_seconds:g}s ago")
    lock = gk.lock_wait(LOCK_NAME, timeout=lock_timeout)
    if lock is None:
        return MergeResult("busy", 0,
                           f"lock '{LOCK_NAME}' is held by another process; nothing written")
    try:
        return _merge_locked()
    except OSError as e:
        return MergeResult("error", 0, f"{e}; history.jsonl was not rewritten")
    finally:
        _stamp_merge_attempt()
        lock.release()


# --- register -----------------------------------------------------------------
# A copy of session_indexer.py's parse_timestamp and extract_prompts, so that this module
# depends only on the standard library and guardkit. tests/test_indexer_pr08.py checks that
# the two copies return the same prompts.

def parse_timestamp(ts_str: str) -> int:
    """Convert ISO timestamp to millisecond epoch."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def extract_prompts(jsonl_file: Path, project_name: str = "") -> list:
    """Extract EVERY user prompt from a session transcript."""
    session_id = jsonl_file.stem
    cwd = ""
    out = []

    try:
        with open(jsonl_file, 'r', encoding='utf-8', errors='surrogateescape') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not cwd:
                    cwd = entry.get('cwd', '') or ""

                if entry.get('type') not in ('user', 'user_message'):
                    continue
                # Meta/compact records and tool results are not user prompts.
                if entry.get('isMeta') or entry.get('isCompactSummary'):
                    continue

                msg = entry.get('message', {})
                content = ""
                if isinstance(msg, str):
                    content = msg
                elif isinstance(msg, dict):
                    c = msg.get('content', '')
                    if isinstance(c, str):
                        content = c
                    elif isinstance(c, list):
                        parts = []
                        is_tool_result = False
                        for block in c:
                            if not isinstance(block, dict):
                                parts.append(str(block))
                                continue
                            if block.get('type') == 'tool_result':
                                is_tool_result = True
                                break
                            t = block.get('text', '')
                            if t:
                                parts.append(str(t))
                        if is_tool_result:
                            continue
                        content = " ".join(parts)

                content = (content or "").strip()
                if len(content) <= 2:
                    continue

                out.append({
                    "display": content[:500],
                    "pastedContents": {},
                    "timestamp": parse_timestamp(entry.get('timestamp', '')) or int(time.time() * 1000),
                    "project": (cwd or project_name or ""),
                    "sessionId": session_id,
                })
    except Exception:
        pass

    return out


def _queue_prompts(prompts: list) -> int:
    """Append prompts whose key is not yet in the parallel index. Returns how many were appended."""
    try:
        blob = PARALLEL_INDEX.read_bytes()
    except FileNotFoundError:
        blob = b""
    known = set()
    for raw in blob.split(b"\n"):
        if raw.strip():
            entry = parse_line(raw)
            if entry is not None:
                known.add(key_digest(entry))
    lines, seen = [], set()
    for prompt in prompts:
        d = key_digest(prompt)
        if d in known or d in seen:
            continue
        seen.add(d)
        lines.append(encode_line(prompt))
    if lines:
        _append_bytes(PARALLEL_INDEX, lines)
    return len(lines)


def register_from_payload(payload: dict,
                          min_interval_seconds: float = DEFAULT_MIN_INTERVAL) -> MergeResult:
    """SessionStart: queue every prompt of the transcript named in the hook payload, then merge.

    The payload carries session_id and transcript_path (guardkit.read_hook_payload). With no
    transcript there is nothing to do, and that is not an error.
    """
    transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not transcript:
        print(NO_SESSION_MSG)
        return MergeResult("noop", 0, "no transcript_path in payload")
    path = Path(transcript)
    if not path.is_file():
        print("[session-index] register: transcript not found; nothing to do")
        return MergeResult("noop", 0, "transcript_path does not exist")
    session_id = payload.get("session_id") or path.stem
    prompts = extract_prompts(path, path.parent.name)
    queued = _queue_prompts(prompts)
    result = merge_history(min_interval_seconds)
    print(f"[session-index] register {session_id}: {len(prompts)} prompts, {queued} queued; "
          f"merge {result.status} ({result.added} added)")
    return result


# --- command line -------------------------------------------------------------

def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    mode = args[0] if args else "merge"
    if mode == "register":
        register_from_payload(gk.read_hook_payload())
        return 0
    if mode == "merge":
        result = merge_history(min_interval_seconds=0)
        print(f"[session-index] merge {result.status}: {result.added} added. {result.detail}")
        return 0
    print(f"Usage: {Path(sys.argv[0]).name} [register|merge]")
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
