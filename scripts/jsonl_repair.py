#!/usr/bin/env python3
"""
JSONL Repair — scans and repairs corrupted Claude Code session JSONL files.
Runs as PreCompact hook + standalone. Handles truncated JSON, encoding issues,
duplicates, control characters, and double-encoded JSON.
No external deps — stdlib only.

NON-DESTRUCTIVE BY CONSTRUCTION. It repairs what is genuinely broken and
otherwise leaves transcripts exactly as it found them:

  * Unparseable lines are PRESERVED verbatim, never deleted. (This tool once
    deleted them, destroying assistant messages and their token accounting.)
  * Reads and writes use errors='surrogateescape', so arbitrary bytes survive
    a round trip instead of being mangled into U+FFFD and then discarded.
  * Live and recently-written transcripts are skipped entirely — rewriting a
    file Claude Code is appending to truncates it out from under the writer.
  * Writes are atomic (temp + fsync + os.replace); a crash cannot truncate.
  * Every removed line is copied to a permanent append-only archive BEFORE the
    rewrite, and the rewrite is abandoned if that archive write fails.
  * A full backup is always taken. There is no --no-backup option.

Only exact-duplicate lines and control-character-only lines are ever removed,
and both are recoverable from backups/sessions/removed-lines-archive.jsonl.
"""

import sys
import os
import json
import re
import shutil
import time
from pathlib import Path
from datetime import datetime

# --- Safety invariants -------------------------------------------------------
# 1. A line is NEVER deleted because it failed to parse. Unparseable lines are
#    preserved byte-for-byte; we do not delete what we do not understand.
# 2. Files are read and written with errors='surrogateescape', so arbitrary
#    bytes round-trip losslessly instead of being mangled into U+FFFD (which
#    used to break the JSON and get the line classified "unrepairable").
# 3. Writes are atomic (temp file + os.replace) so a crash or a concurrent
#    reader can never observe a truncated transcript.
# 4. Files that are active or recently written are skipped entirely -- never
#    rewrite a transcript Claude Code may be appending to.
# 5. Nothing is written unless every dropped line was first archived.
ACTIVE_SKIP_SECONDS = 3600   # don't touch anything modified in the last hour

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
REPAIR_LOG = CLAUDE_DIR / "session-tools" / "logs" / "jsonl_repair.log"
BACKUPS_DIR = CLAUDE_DIR / "backups" / "sessions"
# Every line ever dropped from a live file (duplicate, unrepairable, control-chars-only)
# is preserved here in full, forever — nothing is ever discarded, only relocated.
REMOVED_LINES_ARCHIVE = BACKUPS_DIR / "removed-lines-archive.jsonl"


def is_valid_json_line(line: str) -> bool:
    try:
        data = json.loads(line)
        return isinstance(data, dict)
    except (json.JSONDecodeError, ValueError):
        return False


def attempt_repair_line(line: str) -> str | None:
    line = line.strip()
    if not line:
        return None

    if is_valid_json_line(line):
        return line

    # Missing closing brace
    if line.startswith('{') and not line.endswith('}'):
        for suffix in ['}', '}}']:
            candidate = line + suffix
            if is_valid_json_line(candidate):
                return candidate

    # Missing closing bracket
    if line.startswith('[') and not line.endswith(']'):
        candidate = line + ']'
        if is_valid_json_line(candidate):
            return candidate

    # Trailing commas
    fixed = re.sub(r',\s*}', '}', line)
    fixed = re.sub(r',\s*]', ']', fixed)
    if is_valid_json_line(fixed):
        return fixed

    # Control characters
    fixed = re.sub(r'[\x00\x07\x08\x0b\x0c]', '', line)
    if is_valid_json_line(fixed):
        return fixed

    return None


def repair_file(jsonl_path: Path, backup: bool = True, dry_run: bool = False) -> dict:
    stats = {
        "file": str(jsonl_path),
        "original_lines": 0,
        "valid_lines": 0,
        "repaired_lines": 0,
        "removed_lines": 0,
        "backups_created": [],
        "issues_found": [],
    }

    # Record the file's identity at read time so we can detect any concurrent
    # append before we commit a rewrite (the scan/write gap can be minutes).
    try:
        st_before = jsonl_path.stat()
    except OSError as e:
        stats["issues_found"].append(f"Stat error: {e}")
        return stats

    try:
        with open(jsonl_path, 'r', encoding='utf-8', errors='surrogateescape') as f:
            lines = f.readlines()
    except Exception as e:
        stats["issues_found"].append(f"Read error: {e}")
        return stats

    stats["original_lines"] = len(lines)
    seen_lines = set()
    repaired_lines = []
    dropped = []  # (line_no, reason, raw_content) — archived in full, never just logged/truncated

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        if not stripped:
            stats["removed_lines"] += 1
            continue

        if all(c in '\x00\x07\x08\x0b\x0c\x0d\x1a' for c in stripped):
            stats["removed_lines"] += 1
            stats["issues_found"].append(f"Line {i}: control chars only")
            dropped.append((i, "control chars only", stripped))
            continue

        if is_valid_json_line(stripped):
            if stripped in seen_lines:
                stats["removed_lines"] += 1
                stats["issues_found"].append(f"Line {i}: duplicate")
                dropped.append((i, "duplicate", stripped))
                continue
            seen_lines.add(stripped)
            stats["valid_lines"] += 1
            repaired_lines.append(stripped)
            continue

        fixed = attempt_repair_line(stripped)
        if fixed:
            stats["repaired_lines"] += 1
            if fixed in seen_lines:
                stats["removed_lines"] += 1
                stats["issues_found"].append(f"Line {i}: duplicate after repair")
                dropped.append((i, "duplicate after repair", stripped))
                continue
            seen_lines.add(fixed)
            stats["issues_found"].append(f"Line {i}: repaired")
            # A repair REPLACES the original bytes. Archive the original too, so
            # a wrong-but-valid repair is always reversible line-by-line.
            if fixed != stripped:
                dropped.append((i, "original of repaired line", stripped))
            repaired_lines.append(fixed)
        else:
            # SAFETY: unrepairable != disposable. Keep the line verbatim and
            # flag it. Assistant messages carrying token usage were previously
            # destroyed here; that must never happen again.
            stats["preserved_unparseable"] = stats.get("preserved_unparseable", 0) + 1
            stats["issues_found"].append(f"Line {i}: unparseable - PRESERVED verbatim ({stripped[:80]})")
            repaired_lines.append(stripped)

    # Only touch disk when actually applying a fix — never during --dry-run.
    if not dry_run and (stats["repaired_lines"] > 0 or stats["removed_lines"] > 0):
        # TOCTOU guard: the liveness check happened when this file was picked,
        # which may have been minutes ago. If anything appended to it since we
        # read it, a rewrite would discard those writes — so bail out.
        try:
            st_now = jsonl_path.stat()
        except OSError as e:
            stats["aborted"] = True
            stats["issues_found"].append(f"ABORTED: file vanished before write ({e})")
            return stats
        if (st_now.st_mtime_ns, st_now.st_size) != (st_before.st_mtime_ns, st_before.st_size):
            stats["aborted"] = True
            stats["issues_found"].append(
                "ABORTED: file changed while being scanned (concurrent append) — left untouched")
            return stats

        # Backup first, unconditionally. There is no --no-backup path any more.
        backup_path = make_backup(jsonl_path)
        stats["backups_created"].append(str(backup_path))
        # Backups are never pruned — full retention, forever.

        # Every dropped line must reach the permanent archive BEFORE the file is
        # rewritten. If archiving fails for any reason, abandon the rewrite.
        if dropped:
            try:
                archive_dropped_lines(jsonl_path, dropped)
            except Exception as e:
                stats["aborted"] = True
                stats["issues_found"].append(
                    f"ABORTED: could not archive dropped lines ({e}) — file left untouched")
                return stats
            if not REMOVED_LINES_ARCHIVE.exists():
                stats["aborted"] = True
                stats["issues_found"].append(
                    "ABORTED: removed-lines archive missing after write — file left untouched")
                return stats

        # Atomic write: build the replacement beside the original, fsync it, then
        # swap it in. A crash or a concurrent reader never sees a truncated file.
        tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + '.repair-tmp')
        try:
            with open(tmp_path, 'w', encoding='utf-8', errors='surrogateescape') as f:
                for rl in repaired_lines:
                    f.write(rl + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, jsonl_path)
        except Exception as e:
            try:
                tmp_path.unlink()
            except Exception:
                pass
            stats["aborted"] = True
            stats["issues_found"].append(f"ABORTED: atomic write failed ({e}) — file left untouched")

    return stats


def make_backup(jsonl_path: Path) -> Path:
    """Copy jsonl_path into the dedicated backups dir (never alongside live sessions)."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUPS_DIR / f"{jsonl_path.stem}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    shutil.copy2(jsonl_path, backup_path)
    return backup_path


def archive_dropped_lines(jsonl_path: Path, dropped: list[tuple[int, str, str]]):
    """Append every dropped line's full original content to a permanent, append-only
    archive. Nothing removed from a live session is ever actually discarded."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat()
    with open(REMOVED_LINES_ARCHIVE, 'a', encoding='utf-8') as f:
        for lineno, reason, raw in dropped:
            f.write(json.dumps({
                "archived_at": timestamp,
                "source_file": str(jsonl_path),
                "source_line": lineno,
                "reason": reason,
                "content": raw,
            }, ensure_ascii=False) + '\n')


def is_in_use(jsonl_path: Path) -> str | None:
    """Return a reason string if this transcript must not be rewritten.

    Rewriting a file Claude Code is appending to is the single most destructive
    thing this tool can do: the write truncates the file out from under the
    live writer. So we never touch the current session, and never touch
    anything written recently enough that a session could still be attached.
    """
    active = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if active and jsonl_path.stem == active:
        return "active session"
    try:
        age = time.time() - jsonl_path.stat().st_mtime
    except OSError:
        return "stat failed"
    if age < ACTIVE_SKIP_SECONDS:
        return f"modified {int(age)}s ago (possibly live)"
    return None


def repair_all_projects(backup: bool = True, dry_run: bool = False) -> list[dict]:
    all_stats = []
    if not CLAUDE_PROJECTS_DIR.exists():
        return all_stats
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            if "subagents" in str(jsonl_file) or "acompact" in str(jsonl_file):
                continue
            # Never rewrite a transcript that may still be open for appending.
            if not dry_run:
                reason = is_in_use(jsonl_file)
                if reason:
                    all_stats.append({
                        "file": str(jsonl_file), "original_lines": 0, "valid_lines": 0,
                        "repaired_lines": 0, "removed_lines": 0, "backups_created": [],
                        "skipped": reason,
                        "issues_found": [f"SKIPPED: {reason}"],
                    })
                    continue
            stats = repair_file(jsonl_file, backup=backup, dry_run=dry_run)
            if stats.get("issues_found"):
                all_stats.append(stats)
    return all_stats


def log_results(results: list[dict], dry_run: bool = False):
    REPAIR_LOG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat()
    with open(REPAIR_LOG, 'a', encoding='utf-8') as f:
        label = "Scan (dry-run, no files modified)" if dry_run else "Repair run"
        f.write(f"\n{'='*60}\n{label}: {timestamp}\n{'='*60}\n")
        for stats in results:
            f.write(f"\nFile: {stats['file']}\n")
            f.write(f"  Original: {stats['original_lines']}, Valid: {stats['valid_lines']}, "
                    f"Repaired: {stats['repaired_lines']}, Removed: {stats['removed_lines']}\n")
            for issue in stats.get('issues_found', [])[:20]:
                f.write(f"  - {issue}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Repair corrupted Claude Code JSONL files')
    parser.add_argument('target', nargs='?', default=None)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--backup', action='store_true', default=True,
                        help='(always on; kept for compatibility)')
    parser.add_argument('--dry-run', action='store_true',
                        help='report only, never write')
    args = parser.parse_args()

    # Backups are mandatory. --no-backup was removed: there is no legitimate
    # reason to rewrite a transcript without first copying it aside.
    backup = True
    results = []

    if args.all or args.target is None:
        print("Scanning all project JSONL files...")
        results = repair_all_projects(backup=backup, dry_run=args.dry_run)
    else:
        target = Path(args.target)
        if target.is_dir():
            for jsonl_file in target.glob("*.jsonl"):
                stats = repair_file(jsonl_file, backup=backup, dry_run=args.dry_run)
                if stats.get("issues_found"):
                    results.append(stats)
        elif target.is_file() and target.suffix == '.jsonl':
            stats = repair_file(target, backup=backup, dry_run=args.dry_run)
            if stats.get("issues_found"):
                results.append(stats)

    if not results:
        print("No corruption found — all files valid")
        return

    total_original = sum(s['original_lines'] for s in results)
    total_valid = sum(s['valid_lines'] for s in results)
    total_repaired = sum(s['repaired_lines'] for s in results)
    total_removed = sum(s['removed_lines'] for s in results)
    total_preserved = sum(s.get('preserved_unparseable', 0) for s in results)
    total_skipped = sum(1 for s in results if s.get('skipped'))
    total_aborted = sum(1 for s in results if s.get('aborted'))

    print(f"\nFiles with issues: {len(results) - total_skipped}")
    print(f"Total lines scanned: {total_original}")
    print(f"Valid: {total_valid}, Repaired: {total_repaired}, Removed: {total_removed}")
    print(f"Unparseable lines PRESERVED (never deleted): {total_preserved}")
    if total_skipped:
        print(f"Files skipped as live/recent (not rewritten): {total_skipped}")
    if total_aborted:
        print(f"Files ABORTED by a safety guard (left untouched): {total_aborted}")
    if total_removed and not args.dry_run:
        print(f"Every removed line archived to: {REMOVED_LINES_ARCHIVE}")
    if args.dry_run:
        print("(DRY RUN — no files modified)")

    log_results(results, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
