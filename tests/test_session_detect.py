"""
Tests for scripts/session_detect.py (PR-31), the SessionStart helper that prints NEW or RESUME.
The script is a byte-identical copy of the live file. It takes no stdin and no environment variable
for the session ID. It reads "sessionId" from <home>/.claude/settings.local.json, then looks for that
ID in <home>/.claude/history.jsonl, and in <home>/.claude/projects/<folder>/<session>.jsonl.

Run from the worktree root:  python -I -B tests/test_session_detect.py
Every file is written under a fresh temporary home, which USERPROFILE and HOME point to. The real home is
never used, and no check reads a file outside the temporary home.
Prints one line per check: PASS <name>, or FAIL <name>: <detail>. Exit 1 if any FAIL line is printed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# First, before anything else: this run must use a temporary home, never the real one.
REAL_HOME = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
REAL_CLAUDE = os.path.normcase(os.path.join(REAL_HOME, ".claude"))
TEMP_ROOT = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
HOME = tempfile.mkdtemp(prefix="pr31_session_detect_")
HOME_TEXT = os.path.normcase(os.path.abspath(HOME))
assert HOME_TEXT.startswith(TEMP_ROOT + os.sep), "the temporary home must be inside the temporary folder"
assert HOME_TEXT != REAL_HOME, "the temporary home must not be the real home"
assert os.path.normcase(os.path.join(HOME_TEXT, ".claude")) != REAL_CLAUDE, "the temporary .claude must not be the real one"

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_detect.py"
SID_A = "aaaaaaaa-1111-4222-8333-444444444444"
SID_B = "bbbbbbbb-1111-4222-8333-444444444444"
SID_C = "cccccccc-1111-4222-8333-444444444444"
COUNTS = {"PASS": 0, "FAIL": 0}
EXIT_CODES = []


def emit(kind, name, detail=""):
    COUNTS[kind] += 1
    print(f"{kind} {name}" + (f": {detail}" if detail else ""), flush=True)


def scenario(name):
    """A fresh temporary home for one check, with an empty .claude folder."""
    root = Path(HOME) / name
    (root / ".claude").mkdir(parents=True)
    return root


def write_settings(root, session_id):
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"sessionId": session_id}), encoding="utf-8")


def write_history(root, lines):
    (root / ".claude" / "history.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def history_line(session_id, timestamp, display):
    return json.dumps({"sessionId": session_id, "timestamp": timestamp, "display": display})


def expected_line(status, session_id, method, entries):
    # Exact text printed by main() in scripts/session_detect.py. The ID is cut to 8 characters.
    return f"[session-detect] {status} session {session_id[:8]}... (method: {method}, history: {entries} entries)"


def run_script(root):
    env = dict(os.environ)
    env["USERPROFILE"] = str(root)
    env["HOME"] = str(root)
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT)],
        cwd=str(root), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, timeout=60,
    )
    EXIT_CODES.append(proc.returncode)
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


def expect_line(name, root, expected):
    try:
        code, out, err = run_script(root)
    except Exception as exc:  # a check that cannot run is a failure, never a pass
        emit("FAIL", name, f"{type(exc).__name__}: {exc}")
        return
    if code != 0:
        emit("FAIL", name, f"exit {code}; stderr tail: {err[-300:]!r}")
    elif out != expected:
        emit("FAIL", name, f"expected {expected!r}, got {out!r}")
    else:
        emit("PASS", name)


def main():
    try:
        history = [
            history_line(SID_A, 1728600001000, "first prompt"),
            history_line(SID_A, 1728600002000, "second prompt"),
            history_line(SID_B, 1728600003000, "other session"),
        ]

        # 1. No history file: the session is NEW.
        root = scenario("no_history")
        write_settings(root, SID_A)
        expect_line("1 no history file reports NEW", root,
                    expected_line("NEW", SID_A, "history_jsonl_lookup", 0))

        # 2. The same session ID is in history: RESUME. The count after "history:" is the number of
        #    distinct sessionId values (2 here: SID_A and SID_B), not the number of lines (3).
        root = scenario("resume")
        write_history(root, history)
        write_settings(root, SID_A)
        expect_line("2 session in history reports RESUME", root,
                    expected_line("RESUME", SID_A, "found_in_history", 2))

        # 3. A different session ID, same history: NEW.
        root = scenario("new_with_history")
        write_history(root, history)
        write_settings(root, SID_C)
        expect_line("3 session not in history reports NEW", root,
                    expected_line("NEW", SID_C, "history_jsonl_lookup", 2))

        # 4. A malformed line and a blank line in history: no crash, exit 0, and the entry after
        #    them is still read (RESUME for SID_B).
        root = scenario("malformed_history")
        write_history(root, [
            history_line(SID_A, 1728600001000, "first prompt"),
            "{not json",
            "",
            history_line(SID_B, 1728600003000, "other session"),
        ])
        write_settings(root, SID_B)
        expect_line("4 malformed history line does not crash", root,
                    expected_line("RESUME", SID_B, "found_in_history", 2))

        # 6 (extra, beyond section 5). Not in history, but a transcript file exists under projects/: RESUME.
        root = scenario("projects_file")
        write_history(root, [history_line(SID_B, 1728600003000, "other session")])
        write_settings(root, SID_A)
        project = root / ".claude" / "projects" / "synthetic-project"
        project.mkdir(parents=True)
        (project / f"{SID_A}.jsonl").write_bytes(b"")
        expect_line("6 transcript in projects/ reports RESUME", root,
                    expected_line("RESUME", SID_A, "jsonl_file_exists", 1))

        # 5. The script exits 0 in every run above.
        if EXIT_CODES and all(code == 0 for code in EXIT_CODES):
            emit("PASS", "5 script exits 0 in every run", f"{len(EXIT_CODES)} runs")
        else:
            emit("FAIL", "5 script exits 0 in every run", f"exit codes {EXIT_CODES}")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print(f"checks run: {COUNTS['PASS'] + COUNTS['FAIL']}", flush=True)
    return 1 if COUNTS["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
