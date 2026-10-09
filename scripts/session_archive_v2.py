#!/usr/bin/env python3
"""
Session Archive v2 -- append-only mirror of Claude Code transcripts. Stdlib only.

A new file beside session_archive.py. The live tool is unchanged. This version
never rewrites an archive file, reads only the bytes a sync needs, and writes
in binary. Usage rows are not handled here (PR-06).

Under SESSION_GUARD_HOME/session-archive/ (default ~/.claude/session-archive/):

  transcripts/<rel>                 the archive of one transcript. It only grows.
  state/<rel>.json                  live_offset, head_sha, tail_sha, archive_size.
  quarantine/<rel>.<stamp>.partial  archive bytes a crash left beyond the recorded
                                    size. Copied here and fsynced before the cut.

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
import sys
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


def _quarantine(rel: str, tail: bytes) -> Path:
    """Copy bytes that are about to leave the archive into quarantine, and fsync. Never overwrites."""
    rp = Path(rel)
    qdir = QUARANTINE_DIR / rp.parent
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    qpath = qdir / f"{rp.name}.{stamp}.partial"
    with open(qpath, "xb") as f:
        f.write(tail)
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


def _write_text_lines(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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


def cmd_verify() -> int:
    live_index = {str(r): p for r, p in iter_live_transcripts()}
    archived = [q for q in TRANSCRIPTS_DIR.rglob("*.jsonl")] if TRANSCRIPTS_DIR.exists() else []
    excused = deliberately_removed_keys()

    only_archived = []
    shrunk = []
    excused_total = 0
    for arch in archived:
        rel = arch.relative_to(TRANSCRIPTS_DIR)
        live = live_index.get(str(rel))
        if live is None:
            only_archived.append(arch)
            continue
        a_keys = {_text_key(ln) for ln in _read_text_lines(arch)}
        l_keys = {_text_key(ln) for ln in _read_text_lines(live)}
        missing = a_keys - l_keys
        if missing and excused:
            before = len(missing)
            missing = missing - excused
            excused_total += before - len(missing)
        if missing:
            shrunk.append((str(rel), len(missing)))

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
        print(f"    {name}: {n} lines only in archive")
    if len(shrunk) > 20:
        print(f"    ... and {len(shrunk) - 20} more")
    if excused_total:
        print(f"[session-archive] ({excused_total} further missing lines were removed "
              "deliberately by a repair tool and are archived, so not counted above)")
    return 0


def cmd_restore() -> int:
    live_index = {str(r): p for r, p in iter_live_transcripts()}
    archived = [q for q in TRANSCRIPTS_DIR.rglob("*.jsonl")] if TRANSCRIPTS_DIR.exists() else []
    restored = merged = 0

    for arch in archived:
        rel = arch.relative_to(TRANSCRIPTS_DIR)
        live = live_index.get(str(rel))
        arch_lines = _read_text_lines(arch)
        if not arch_lines:
            continue
        if live is None:
            target = PROJECTS_DIR / rel
            if target.exists():
                continue
            _write_text_lines(target, arch_lines)
            restored += 1
            continue
        live_lines = _read_text_lines(live)
        have = {_text_key(ln) for ln in live_lines}
        missing = [ln for ln in arch_lines if _text_key(ln) not in have]
        if missing:
            _write_text_lines(live, arch_lines + [ln for ln in live_lines
                                                  if _text_key(ln) not in
                                                  {_text_key(x) for x in arch_lines}])
            merged += 1

    print(f"[session-archive] restored {restored} missing sessions, "
          f"merged lines back into {merged} shrunken transcripts")
    return 0


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
    if mode == "restore":
        return cmd_restore()
    if mode == "status":
        return cmd_status()
    print(f"Usage: {sys.argv[0]} [sync [--all | --transcript <path>]|verify|restore|status]")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
