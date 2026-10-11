#!/usr/bin/env python3
"""
Session Detect — determines if the current session is NEW or a RESUME.
Compares session ID against history.jsonl + projects/ on SessionStart.
No external deps — stdlib only.
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CLAUDE_DIR = Path.home() / ".claude"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def load_history_session_ids() -> set:
    ids = set()
    if not HISTORY_FILE.exists():
        return ids
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # A bad line is skipped on its own. It must not end the loop, or later sessions read as NEW.
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        continue
                    session_id = entry.get('sessionId')
                    if isinstance(session_id, str) and session_id:
                        ids.add(session_id)
                except (ValueError, TypeError, AttributeError):
                    pass
    except Exception:
        pass
    return ids


def get_current_session_id() -> str | None:
    settings_file = CLAUDE_DIR / "settings.local.json"
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding='utf-8'))
            return data.get("sessionId")
        except Exception:
            pass
    return None


def detect_session_type(session_id: str, history_ids: set) -> dict:
    result = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "is_new": True,
        "detection_method": "history_jsonl_lookup",
        "history_entries": len(history_ids),
    }

    if session_id in history_ids:
        result["is_new"] = False
        result["detection_method"] = "found_in_history"
    else:
        # Also check projects/ for the .jsonl file
        if PROJECTS_DIR.exists():
            for project_dir in PROJECTS_DIR.iterdir():
                if project_dir.is_dir():
                    jsonl_file = project_dir / f"{session_id}.jsonl"
                    if jsonl_file.exists():
                        result["is_new"] = False
                        result["detection_method"] = "jsonl_file_exists"
                        break

    return result


def main():
    session_id = get_current_session_id()
    if not session_id:
        print("[session-detect] No active session found")
        return

    history_ids = load_history_session_ids()
    result = detect_session_type(session_id, history_ids)

    status = "RESUME" if not result["is_new"] else "NEW"
    print(f"[session-detect] {status} session {session_id[:8]}... "
          f"(method: {result['detection_method']}, history: {result['history_entries']} entries)")


if __name__ == "__main__":
    main()
