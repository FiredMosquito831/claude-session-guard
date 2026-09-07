#!/usr/bin/env python3
"""
Session Archive — lifetime retention for Claude Code session data.

Claude Code deletes transcripts in ~/.claude/projects/ once they age past
`cleanupPeriodDays`, and tools that rewrite transcripts can shrink them. This
script keeps an independent, append-only mirror that nothing else writes to, so
session and token-usage data survives regardless of what happens upstream.

Two artefacts, both under ~/.claude/session-archive/:

  transcripts/<project>/<sessionId>.jsonl
      A full mirror of every transcript ever seen. Updated by MERGING on line
      identity (uuid, else exact text), so the archive only ever grows: if a
      live file is truncated, rewritten, or deleted, the archive still holds
      every line it ever had.

  usage-ledger.jsonl
      One append-only row per assistant message that carries token usage
      (sessionId, uuid, timestamp, model, usage). Small, flat, and sufficient
      to reconstruct lifetime token accounting even if every transcript were
      lost. Deduped on (sessionId, uuid).

Commands:
    sync [--full]   mirror new/changed transcripts (incremental by mtime)
    verify          report archive vs live: sessions only in the archive, and
                    any live file that has fewer lines than its archived copy
    restore         copy archived-only sessions back into projects/
    status          counts and sizes

Safe to run concurrently with Claude Code: it only ever READS projects/ and
only ever writes inside session-archive/. No external deps — stdlib only.
"""

import json
import os
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ARCHIVE_DIR = CLAUDE_DIR / "session-archive"
TRANSCRIPTS_DIR = ARCHIVE_DIR / "transcripts"
USAGE_LEDGER = ARCHIVE_DIR / "usage-ledger.jsonl"
WATERMARK_FILE = ARCHIVE_DIR / ".last-sync"
SYNC_OVERLAP_SECONDS = 900  # re-check recently-touched files; never skip a write


def line_key(text: str):
    """Identity of a transcript line: uuid when present, else the exact text."""
    try:
        entry = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ("raw", text)
    uid = entry.get("uuid")
    return ("uuid", uid) if uid else ("raw", text)


def read_lines(path: Path) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        return []


def atomic_write_lines(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def usage_rows(lines: list, session_id: str) -> list:
    """Extract token-usage rows from transcript lines."""
    rows = []
    for text in lines:
        if '"usage"' not in text:
            continue
        try:
            entry = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        rows.append({
            "sessionId": session_id,
            "uuid": entry.get("uuid", ""),
            "timestamp": entry.get("timestamp", ""),
            "model": msg.get("model", ""),
            "usage": usage,
        })
    return rows


def load_ledger_keys() -> set:
    keys = set()
    if not USAGE_LEDGER.exists():
        return keys
    with open(USAGE_LEDGER, "r", encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            keys.add((row.get("sessionId", ""), row.get("uuid", "")))
    return keys


def iter_live_transcripts(include_subagents: bool = True):
    """Yield (relative_path, absolute_path) for every transcript under projects/.

    Subagent and workflow transcripts live at
    projects/<proj>/<sessionId>/subagents/[workflows/<wf>/]agent-*.jsonl.
    They carry their own token usage and are deleted by the same retention
    sweep, so lifetime retention has to cover them too. The archive mirrors the
    full relative path, so the tree round-trips exactly on restore.
    """
    if not PROJECTS_DIR.exists():
        return
    for jsonl_file in PROJECTS_DIR.rglob("*.jsonl"):
        if not jsonl_file.is_file():
            continue
        name = str(jsonl_file)
        if "acompact" in name:
            continue
        if not include_subagents and "subagents" in name:
            continue
        yield jsonl_file.relative_to(PROJECTS_DIR), jsonl_file


def session_id_for(rel_path) -> str:
    """Owning session id for a transcript: the session dir for a subagent
    transcript, otherwise the file stem."""
    parts = rel_path.parts
    if len(parts) >= 3 and "subagents" in parts:
        return parts[1]
    return rel_path.stem


def cmd_sync(full: bool = False) -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    watermark = 0.0
    if not full and WATERMARK_FILE.exists():
        try:
            watermark = float(WATERMARK_FILE.read_text().strip())
        except Exception:
            watermark = 0.0
    cutoff = (watermark - SYNC_OVERLAP_SECONDS) if watermark else 0.0
    started = time.time()

    # Ledger dedup: on an incremental sync we only emit usage rows for lines we
    # just added to the archive, which are new by construction — so there is no
    # need to read the (large) ledger at all. A full sync re-derives everything
    # and therefore does need the existing key set.
    ledger_keys = load_ledger_keys() if full else set()
    new_ledger = []
    files_seen = files_updated = lines_added = 0

    for rel, live_path in iter_live_transcripts():
        files_seen += 1
        if cutoff:
            try:
                if live_path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue

        live_lines = read_lines(live_path)
        if not live_lines:
            continue

        archive_path = TRANSCRIPTS_DIR / rel
        archived = read_lines(archive_path)

        if archived:
            have = {line_key(ln) for ln in archived}
            additions = [ln for ln in live_lines if line_key(ln) not in have]
            if additions:
                # Append-only merge: the archive never loses a line it once held,
                # even if the live file was truncated or rewritten.
                atomic_write_lines(archive_path, archived + additions)
                files_updated += 1
                lines_added += len(additions)
        else:
            additions = live_lines
            atomic_write_lines(archive_path, live_lines)
            files_updated += 1
            lines_added += len(live_lines)

        # Only newly-archived lines can contribute new usage rows.
        if additions:
            session_id = session_id_for(rel)
            for row in usage_rows(additions, session_id):
                k = (row["sessionId"], row["uuid"])
                if full:
                    if k in ledger_keys:
                        continue
                    ledger_keys.add(k)
                new_ledger.append(row)

    if new_ledger:
        USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LEDGER, "a", encoding="utf-8") as f:
            for row in new_ledger:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        WATERMARK_FILE.write_text(str(started))
    except Exception:
        pass

    print(f"[session-archive] scanned {files_seen} live transcripts; "
          f"archived {files_updated} ({lines_added} new lines); "
          f"+{len(new_ledger)} usage rows")
    return 0


def deliberately_removed_keys() -> set:
    """Line identities that a repair tool removed ON PURPOSE.

    `api_repair` deletes lines that are valid JSON but that the Messages API
    rejects (an assistant line whose only content is an empty thinking block).
    Those lines stay in the archive forever, so without this the "live files
    missing archived lines" alarm would fire for every repaired session and the
    detector would become useless noise. Every deliberate removal is recorded
    in the permanent archive, so we can subtract exactly those and keep the
    alarm meaningful for genuine, unexplained loss.
    """
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
                # Do NOT restrict this to JSON-looking content: jsonl_repair
                # also removes control-character-only padding lines, and those
                # are just as deliberate. Excusing only JSON left them to fire
                # the alarm forever. Key both the raw and stripped forms, since
                # the archiver stores the stripped line.
                keys.add(line_key(content))
                stripped = content.strip()
                if stripped != content:
                    keys.add(line_key(stripped))
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
        a_keys = {line_key(ln) for ln in read_lines(arch)}
        l_keys = {line_key(ln) for ln in read_lines(live)}
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
        arch_lines = read_lines(arch)
        if not arch_lines:
            continue
        if live is None:
            target = PROJECTS_DIR / rel
            if target.exists():
                continue
            atomic_write_lines(target, arch_lines)
            restored += 1
            continue
        live_lines = read_lines(live)
        have = {line_key(ln) for ln in live_lines}
        missing = [ln for ln in arch_lines if line_key(ln) not in have]
        if missing:
            atomic_write_lines(live, arch_lines + [ln for ln in live_lines
                                                   if line_key(ln) not in
                                                   {line_key(x) for x in arch_lines}])
            merged += 1

    print(f"[session-archive] restored {restored} missing sessions, "
          f"merged lines back into {merged} shrunken transcripts")
    return 0


def cmd_status() -> int:
    archived = [q for q in TRANSCRIPTS_DIR.rglob("*.jsonl")] if TRANSCRIPTS_DIR.exists() else []
    total_bytes = sum(p.stat().st_size for p in archived) if archived else 0
    ledger_rows = 0
    ledger_tokens = 0
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
                ledger_tokens += sum(v for v in u.values() if isinstance(v, int))
    live = sum(1 for _ in iter_live_transcripts())
    print(json.dumps({
        "archived_sessions": len(archived),
        "live_sessions": live,
        "archive_bytes": total_bytes,
        "archive_mb": round(total_bytes / 1048576, 1),
        "usage_ledger_rows": ledger_rows,
        "usage_ledger_tokens": ledger_tokens,
        "archive_dir": str(ARCHIVE_DIR),
    }, indent=2))
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "sync":
        return cmd_sync(full="--full" in sys.argv)
    if mode == "verify":
        return cmd_verify()
    if mode == "restore":
        return cmd_restore()
    if mode == "status":
        return cmd_status()
    print(f"Usage: {sys.argv[0]} [sync [--full]|verify|restore|status]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
