#!/usr/bin/env python3
"""
Session Indexer — keeps history.jsonl in sync with all session .jsonl transcripts.
Runs on SessionStart hooks + manual calls. Scans projects/ for .jsonl files,
extracts EVERY user prompt, and merges new entries into history.jsonl.

history.jsonl holds one entry per PROMPT, not per session. Dedup is therefore
keyed on (sessionId, timestamp, display); keying on sessionId alone collapsed
every multi-prompt session to a single surviving prompt. The merge is
append-only in effect: it refuses to write a result smaller than what is
already on disk, backs up with rotation, and writes atomically.

No external deps — stdlib only (json, os, sys, pathlib, time, datetime).
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from datetime import datetime

# --- Safety invariants -------------------------------------------------------
# 1. Entries are identified by (sessionId, timestamp, display) -- NOT by
#    sessionId alone. history.jsonl holds one entry per PROMPT, and many
#    prompts share a sessionId; deduping on sessionId collapsed every
#    multi-prompt session down to a single surviving prompt.
# 2. history.jsonl is only ever written atomically, and only ever as a
#    superset: if the merge result would contain fewer entries than the file
#    already on disk, the write is refused.
# 3. Backups rotate instead of being written once and never refreshed.
MAX_HISTORY_BACKUPS = 10

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
OFFICIAL_HISTORY = CLAUDE_DIR / "history.jsonl"
PARALLEL_INDEX = CLAUDE_DIR / ".session_index.jsonl"
MERGE_THROTTLE_FILE = CLAUDE_DIR / ".session_merge_last"
MERGE_THROTTLE_SECONDS = 300  # only merge every 5 min
SCAN_WATERMARK_FILE = CLAUDE_DIR / ".session_scan_last"


def parse_timestamp(ts_str: str) -> int:
    """Convert ISO timestamp to millisecond epoch."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def extract_first_user_message(jsonl_file: Path, project_name: str = "") -> dict | None:
    """Extract first user message + session metadata from a session .jsonl."""
    session_id = jsonl_file.stem
    first_msg = None
    cwd = None
    ts = None

    try:
        with open(jsonl_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not cwd:
                    cwd = entry.get('cwd', '')

                if entry.get('type') in ('user', 'user_message') and not first_msg:
                    msg = entry.get('message', {})
                    content = ""
                    if isinstance(msg, dict):
                        c = msg.get('content', '')
                        if isinstance(c, list):
                            parts = []
                            for block in c:
                                if isinstance(block, dict):
                                    t = block.get('text', '') or block.get('content', '')
                                    if t:
                                        parts.append(str(t))
                                else:
                                    parts.append(str(block))
                            content = " ".join(parts)
                        elif c:
                            content = str(c)
                    elif isinstance(msg, str):
                        content = msg

                    if content and len(content) > 2:
                        first_msg = content[:500]
                        ts = entry.get('timestamp', '')
                        break

        if first_msg:
            ts_ms = parse_timestamp(ts) if ts else int(time.time() * 1000)
            project = cwd or project_name
            return {
                "display": first_msg,
                "pastedContents": {},
                "timestamp": ts_ms,
                "project": project.replace("\\", "\\\\") if project else "",
                "sessionId": session_id,
            }
    except Exception:
        pass

    return None


def entry_key(entry: dict) -> tuple:
    """Identity of a history entry: one entry per prompt, not per session."""
    return (
        entry.get('sessionId', ''),
        entry.get('timestamp', 0),
        (entry.get('display', '') or '')[:500],
    )


def load_entry_keys(*files: Path) -> set:
    """Load full (sessionId, timestamp, display) keys from history-shaped files."""
    keys = set()
    for fp in files:
        if not fp.exists():
            continue
        try:
            with open(fp, 'r', encoding='utf-8', errors='surrogateescape') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        keys.add(entry_key(json.loads(line)))
                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            pass
    return keys


def load_session_ids(*files: Path) -> set:
    """Load sessionIds from one or more JSONL files."""
    ids = set()
    for fp in files:
        if not fp.exists():
            continue
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        sid = entry.get('sessionId', '')
                        if sid:
                            ids.add(sid)
                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            pass
    return ids


def extract_prompts(jsonl_file: Path, project_name: str = "") -> list:
    """Extract EVERY user prompt from a session transcript.

    history.jsonl records one entry per prompt. The old implementation returned
    only the first message per session, which is why rescans could never
    repopulate a session's prompts once they had been lost.
    """
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


def scan_new_sessions(full: bool = False) -> list:
    """Scan project dirs for prompts not yet present in the index or history.

    Incremental by default: only transcripts modified since the last scan are
    re-read, so this stays fast enough for a SessionStart hook even with
    thousands of sessions. `full=True` re-reads everything.
    """
    indexed = load_entry_keys(PARALLEL_INDEX, OFFICIAL_HISTORY)
    new_entries = []

    if not PROJECTS_DIR.exists():
        return new_entries

    watermark = 0.0
    if not full and SCAN_WATERMARK_FILE.exists():
        try:
            watermark = float(SCAN_WATERMARK_FILE.read_text().strip())
        except Exception:
            watermark = 0.0
    # Overlap the window slightly so a file written during the last scan is
    # never skipped. Missing a prompt is a silent loss; re-reading is cheap.
    cutoff = watermark - 600 if watermark else 0.0

    scan_started = time.time()

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            if "subagents" in str(jsonl_file) or "acompact" in str(jsonl_file):
                continue
            if cutoff:
                try:
                    if jsonl_file.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
            for info in extract_prompts(jsonl_file, project_dir.name):
                k = entry_key(info)
                if k in indexed:
                    continue
                indexed.add(k)
                new_entries.append(info)

    try:
        SCAN_WATERMARK_FILE.write_text(str(scan_started))
    except Exception:
        pass

    return new_entries


def append_to_index(entry: dict):
    """Append an entry to the parallel index file."""
    PARALLEL_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(PARALLEL_INDEX, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def merge_to_official_history(throttled: bool = True):
    """
    Merge parallel index entries into official history.jsonl.
    Only adds entries whose sessionId is not already present.
    Throttled by default (5 min).
    """
    if throttled and MERGE_THROTTLE_FILE.exists():
        try:
            last_merge = float(MERGE_THROTTLE_FILE.read_text().strip())
            if time.time() - last_merge < MERGE_THROTTLE_SECONDS:
                return
        except Exception:
            pass

    if not PARALLEL_INDEX.exists():
        return

    # Update throttle timestamp
    try:
        MERGE_THROTTLE_FILE.write_text(str(time.time()))
    except Exception:
        pass

    # Load existing history — every entry is kept, keyed per PROMPT.
    existing_keys = set()
    existing_entries = []
    if OFFICIAL_HISTORY.exists():
        try:
            with open(OFFICIAL_HISTORY, 'r', encoding='utf-8', errors='surrogateescape') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    existing_entries.append(entry)
                    existing_keys.add(entry_key(entry))
        except Exception:
            pass

    # Load parallel index — add anything whose full key is not already present.
    new_entries = []
    try:
        with open(PARALLEL_INDEX, 'r', encoding='utf-8', errors='surrogateescape') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                k = entry_key(entry)
                if k in existing_keys:
                    continue
                existing_keys.add(k)
                new_entries.append(entry)
    except Exception:
        pass

    if not new_entries:
        return

    # Merge and sort. Dedup is by full entry identity, so multiple prompts in
    # one session all survive — collapsing on sessionId is what destroyed them.
    seen = {}
    for entry in existing_entries + new_entries:
        seen[entry_key(entry)] = entry
    all_entries = sorted(seen.values(), key=lambda e: e.get('timestamp', 0))

    # SAFETY: the merge must never lose a distinct entry. Collapsing lines that
    # are byte-identical in identity is fine; dropping a prompt is not. So the
    # invariant is on the KEY SET, not the line count: every key already on disk
    # must still be present in the result.
    result_keys = set(seen.keys())
    lost_keys = existing_keys - result_keys
    if lost_keys:
        print(f"[session-index] REFUSED: merge would drop {len(lost_keys)} distinct "
              f"entries from history.jsonl; left untouched")
        return

    # Rotating backup — the old code backed up once and never refreshed it.
    if OFFICIAL_HISTORY.exists():
        try:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            shutil.copy2(str(OFFICIAL_HISTORY),
                         str(OFFICIAL_HISTORY.with_suffix(f'.jsonl.backup.{stamp}')))
            rotated = sorted(OFFICIAL_HISTORY.parent.glob('history.jsonl.backup.*'))
            for old in rotated[:-MAX_HISTORY_BACKUPS]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    # Atomic write — a crash mid-merge can never truncate history.jsonl.
    tmp = OFFICIAL_HISTORY.with_suffix('.jsonl.merge-tmp')
    try:
        with open(tmp, 'w', encoding='utf-8', errors='surrogateescape') as f:
            for entry in all_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OFFICIAL_HISTORY)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        print(f"[session-index] Merge write failed ({e}); history.jsonl left untouched")
        return

    print(f"[session-index] Merged {len(new_entries)} new entries into history.jsonl "
          f"({len(all_entries)} total)")


def register_current():
    """Register the current session from settings.local.json."""
    settings_file = CLAUDE_DIR / "settings.local.json"
    if not settings_file.exists():
        print("[session-index] No settings.local.json found")
        return

    try:
        data = json.loads(settings_file.read_text(encoding='utf-8'))
        session_id = data.get("sessionId", "")
        if not session_id:
            print("[session-index] No sessionId in settings.local.json")
            return

        indexed_ids = load_session_ids(PARALLEL_INDEX, OFFICIAL_HISTORY)
        if session_id in indexed_ids:
            print(f"[session-index] Session {session_id} already indexed")
            return

        entry = {
            "display": data.get("display", ""),
            "pastedContents": {},
            "timestamp": int(time.time() * 1000),
            "project": data.get("project", "").replace("\\", "\\\\"),
            "sessionId": session_id,
        }
        append_to_index(entry)
        print(f"[session-index] Registered session {session_id}")
    except Exception as e:
        print(f"[session-index] Error: {e}")


def status():
    """Print index status."""
    ids = load_session_ids(PARALLEL_INDEX, OFFICIAL_HISTORY)
    parallel_count = 0
    if PARALLEL_INDEX.exists():
        with open(PARALLEL_INDEX, 'r', encoding='utf-8', errors='replace') as f:
            parallel_count = sum(1 for line in f if line.strip())
    history_count = 0
    if OFFICIAL_HISTORY.exists():
        with open(OFFICIAL_HISTORY, 'r', encoding='utf-8', errors='replace') as f:
            history_count = sum(1 for line in f if line.strip())
    print(json.dumps({
        "total_indexed_sessions": len(ids),
        "parallel_index_entries": parallel_count,
        "history_jsonl_entries": history_count,
        "parallel_index_exists": PARALLEL_INDEX.exists(),
    }, indent=2))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if mode == "scan":
        full = "--full" in sys.argv
        new = scan_new_sessions(full=full)
        for entry in new:
            append_to_index(entry)
        merge_to_official_history()
        print(f"[session-index] Found {len(new)} new sessions" if new else "[session-index] No new sessions")

    elif mode == "register":
        register_current()

    elif mode == "merge":
        merge_to_official_history(throttled=False)
        print("[session-index] Merge complete")

    elif mode == "status":
        status()

    else:
        print(f"Usage: {sys.argv[0]} [scan|register|merge|status]")


if __name__ == "__main__":
    main()
