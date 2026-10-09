#!/usr/bin/env python3
"""
PR-18 end-to-end test of scripts/guard_hook.py with the documented hook payloads.

Each check runs the real entry point as a child process, the way Claude Code calls a hook: the event
name is the argument, the payload goes to stdin, and SESSION_GUARD_HOME points at a temporary home.
After each child, the check reads back what the hook wrote: the register files, the archive mirror,
the repaired transcript, the removal record, the backup and the repair log. Detached sweeps are
waited for, so their unattended path runs for real. Nothing is stubbed.

This file writes only under one temporary root (ROOT, from tempfile). Every path it uses is
asserted to lie under ROOT. It does not read or write any settings file, hook registration or live
data folder.

Run: python -I tests/test_guard_hook_e2e_pr18.py
Each check prints PASS <name> or FAIL <name>: <reason>. Exit 0 only when every check passes.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="pr18_e2e_root_")
os.environ["SESSION_GUARD_HOME"] = ROOT      # set before any plugin code is imported

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import guardkit  # noqa: E402
import guard_hook  # noqa: E402

if not os.path.normcase(str(guardkit.CLAUDE_DIR)).startswith(os.path.normcase(ROOT)):
    print(f"FAIL setup: guardkit.CLAUDE_DIR {guardkit.CLAUDE_DIR} is not under {ROOT}")
    sys.exit(1)

SUMMARY_RE = re.compile(
    r"^\[guard\] (SessionStart|Stop|PreCompact|SessionEnd): ((?:\w+=(?:ok|fail|timeout|skipped) )+)total=(\d+\.\d{2})s$")

TOTALS = []      # (event, total seconds) for every child run, filled by check_summary


# --- paths, homes and the child environment -----------------------------------

def assert_under_root(path) -> Path:
    p = Path(path)
    root = os.path.normcase(os.path.abspath(ROOT))
    here = os.path.normcase(os.path.abspath(str(p)))
    if not (here == root or here.startswith(root + os.sep)):
        raise AssertionError(f"path is outside the temporary root: {p.name}")
    return p


def case_home(name: str) -> Path:
    home = assert_under_root(Path(ROOT) / name)
    home.mkdir(parents=True, exist_ok=True)
    return home


def child_env(home: Path) -> dict:
    env = dict(os.environ)
    env["SESSION_GUARD_HOME"] = str(assert_under_root(home))
    assert env["SESSION_GUARD_HOME"], "SESSION_GUARD_HOME must not be empty"
    env.pop("CLAUDE_SESSION_ID", None)      # the run must not depend on the live session
    return env


# --- payloads and fixtures ------------------------------------------------------

COMMON = {"session_id", "transcript_path", "cwd", "hook_event_name", "permission_mode"}

# Field names and values come from the Claude Code hooks documentation as recorded on 2026-10-09.
# UNVERIFIED on this machine: no payload captured here has been checked against them.
EVENT_FIELDS = {
    "SessionStart": {"source": "startup"},
    "Stop": {"stop_hook_active": False, "last_assistant_message": "synthetic",
             "background_tasks": [], "session_crons": []},
    "PreCompact": {"trigger": "manual", "custom_instructions": None},
    "SessionEnd": {"reason": "clear"},
}

ALLOWED_VALUES = {
    "source": {"startup", "resume", "clear", "compact", "fork"},
    "trigger": {"manual", "auto"},
    "reason": {"clear", "resume", "logout", "prompt_input_exit", "other"},
}


def make_payload(event: str, sid: str, transcript: Path, **fields) -> bytes:
    d = {"session_id": sid, "transcript_path": str(transcript), "cwd": str(transcript.parent),
         "hook_event_name": event, "permission_mode": "default"}
    d.update(fields)
    return json.dumps(d).encode("ascii")


def jline(n: int) -> bytes:
    return json.dumps({"type": "user", "uuid": f"u{n}", "timestamp": "2026-10-09T10:00:00Z",
                       "message": {"role": "user", "content": f"hello {n}"}}).encode("utf-8")


def thinking_line() -> bytes:
    return json.dumps({"type": "assistant", "uuid": "a1", "timestamp": "2026-10-09T10:00:01Z",
                       "message": {"id": "m1", "role": "assistant",
                                   "content": [{"type": "thinking", "thinking": "   ", "signature": "s"}]}}
                      ).encode("utf-8")


def write_transcript(home: Path, slug: str, sid: str, lines) -> Path:
    path = assert_under_root(home / "projects" / slug / f"{sid}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + b"\n" for line in lines))
    return path


# --- running a child and reading back ---------------------------------------------

def run_hook(event: str, payload: bytes, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "guard_hook.py"), event],
                          input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=child_env(home), timeout=120)


def check_summary(proc: subprocess.CompletedProcess, event: str):
    if proc.returncode != 0:
        raise AssertionError(f"{event}: hook exit {proc.returncode}, stderr {len(proc.stderr)} bytes")
    if not proc.stdout.endswith(b"\n"):
        raise AssertionError(f"{event}: stdout does not end in a newline")
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if len(lines) != 1:
        raise AssertionError(f"{event}: expected 1 summary line, got {len(lines)} ({len(proc.stdout)} bytes)")
    m = SUMMARY_RE.match(lines[0])
    if not m or m.group(1) != event:
        raise AssertionError(f"{event}: summary line does not match the format: {lines[0]!r}")
    steps = dict(re.findall(r"(\w+)=(\w+)", m.group(2)))
    total = float(m.group(3))
    TOTALS.append((event, total))
    return steps, total


def expect_steps(name: str, event: str, steps: dict, expected: dict) -> None:
    if steps != expected:
        raise AssertionError(f"{name}: {event} steps {steps} != expected {expected}")


def wait_detached(home: Path, name: str) -> None:
    state = assert_under_root(home / "session-tools" / "state" / f"detached-{name}.json")
    pid = int(json.loads(state.read_text(encoding="utf-8"))["pid"])
    deadline = time.monotonic() + 120.0
    while guard_hook._pid_alive(pid):
        if time.monotonic() > deadline:
            raise AssertionError(f"detached {name} still running after 120 s")
        time.sleep(0.2)
    log = assert_under_root(home / "session-tools" / "logs" / f"{name}.log")
    if not log.is_file():
        raise AssertionError(f"detached {name}: log file is missing")


def check_prompt_file(name: str, path: Path, sid: str) -> None:
    assert_under_root(path)
    if not path.is_file():
        raise AssertionError(f"{name}: {path.name} was not written")
    rows = path.read_bytes().splitlines()
    if len(rows) != 2:
        raise AssertionError(f"{name}: {path.name} has {len(rows)} lines, expected 2")
    displays = set()
    for raw in rows:
        row = json.loads(raw.decode("utf-8"))
        if not isinstance(row, dict) or row.get("sessionId") != sid:
            raise AssertionError(f"{name}: a row in {path.name} does not carry this session id")
        displays.add(row.get("display"))
    if displays != {"hello 1", "hello 2"}:
        raise AssertionError(f"{name}: displays in {path.name} are not exactly hello 1 and hello 2")


def expect_mirror(name: str, home: Path, slug: str, sid: str, fixture: bytes) -> None:
    mirror = assert_under_root(home / "session-archive" / "transcripts" / slug / f"{sid}.jsonl")
    if not mirror.is_file():
        raise AssertionError(f"{name}: archive mirror {mirror.name} was not written")
    got = mirror.read_bytes()
    if got != fixture:
        raise AssertionError(f"{name}: archive mirror differs from the transcript ({len(got)} vs {len(fixture)} bytes)")


def removal_records_for(home: Path, transcript: Path) -> list:
    archive = assert_under_root(home / "backups" / "sessions" / "removed-lines-archive.jsonl")
    if not archive.is_file():
        return []
    want = os.path.normcase(os.path.abspath(str(transcript)))
    out = []
    for raw in archive.read_bytes().splitlines():
        rec = json.loads(raw.decode("utf-8"))      # every line must parse
        if os.path.normcase(os.path.abspath(str(rec["source_file"]))) == want:
            out.append(rec)
    return out


def backups_for(home: Path, sid: str) -> list:
    folder = assert_under_root(home / "backups" / "sessions")
    pattern = os.path.join(glob.escape(str(folder)), f"{sid}.apibackup.*.jsonl")
    return sorted(glob.glob(pattern))


def api_repair_log(home: Path) -> str:
    log = assert_under_root(home / "session-tools" / "logs" / "api_repair.log")
    if not log.is_file():
        return ""
    return log.read_text(encoding="utf-8", errors="replace")


# --- the checks, in run order -------------------------------------------------------

def session_start_register() -> float:
    name = "session_start_register"
    home = case_home("s1")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugStart", sid, [jline(1), jline(2)])
    payload = make_payload("SessionStart", sid, tp, **EVENT_FIELDS["SessionStart"])
    proc = run_hook("SessionStart", payload, home)
    steps, total = check_summary(proc, "SessionStart")
    expect_steps(name, "SessionStart", steps, {"register": "ok", "sweep": "ok"})
    wait_detached(home, "sweep-api_repair")
    check_prompt_file(name, home / ".session_index.jsonl", sid)
    check_prompt_file(name, home / "history.jsonl", sid)
    return total


def stop_archive_mirror() -> float:
    name = "stop_archive_mirror"
    home = case_home("s2")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugStop", sid, [jline(1), jline(2)])
    fixture = tp.read_bytes()
    payload = make_payload("Stop", sid, tp, **EVENT_FIELDS["Stop"])
    proc = run_hook("Stop", payload, home)
    steps, total = check_summary(proc, "Stop")
    expect_steps(name, "Stop", steps, {"archive": "ok", "index": "ok", "usage": "ok"})
    expect_mirror(name, home, "slugStop", sid, fixture)
    return total


def precompact_archive_mirror() -> float:
    name = "precompact_archive_mirror"
    home = case_home("s3")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugPre", sid, [jline(1), jline(2)])
    fixture = tp.read_bytes()
    payload = make_payload("PreCompact", sid, tp, **EVENT_FIELDS["PreCompact"])
    proc = run_hook("PreCompact", payload, home)
    steps, total = check_summary(proc, "PreCompact")
    expect_steps(name, "PreCompact", steps, {"archive": "ok", "sweep": "ok"})
    expect_mirror(name, home, "slugPre", sid, fixture)
    wait_detached(home, "sweep-jsonl_repair")
    return total


def session_end_clear_repairs_and_archives() -> float:
    name = "session_end_clear_repairs_and_archives"
    home = case_home("s4")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugEnd", sid, [jline(6), thinking_line()])
    fixture = tp.read_bytes()
    payload = make_payload("SessionEnd", sid, tp, **EVENT_FIELDS["SessionEnd"])
    proc = run_hook("SessionEnd", payload, home)
    steps, total = check_summary(proc, "SessionEnd")
    expect_steps(name, "SessionEnd", steps, {"repair": "ok"})
    if tp.read_bytes() != jline(6) + b"\n":
        raise AssertionError(f"{name}: transcript is not the kept line alone ({tp.stat().st_size} bytes)")
    backups = backups_for(home, sid)
    if len(backups) != 1:
        raise AssertionError(f"{name}: expected 1 backup, found {len(backups)}")
    if Path(backups[0]).read_bytes() != fixture:
        raise AssertionError(f"{name}: backup differs from the fixture")
    recs = removal_records_for(home, tp)
    if len(recs) != 1:
        raise AssertionError(f"{name}: expected 1 removal record for this transcript, found {len(recs)}")
    rec = recs[0]
    if set(rec) != {"archived_at", "source_file", "source_line", "reason", "content"}:
        raise AssertionError(f"{name}: removal record keys are {sorted(rec)}")
    if rec["source_line"] != 2 or rec["reason"] != "empty-thinking-only line":
        raise AssertionError(f"{name}: removal record has source_line {rec['source_line']} and reason {rec['reason']!r}")
    if rec["content"] != thinking_line().decode("ascii"):
        raise AssertionError(f"{name}: removal record content differs from the removed line")
    # The value of the end_reason label is not checked here. On the pre-PR-20 base the label reads
    # "end_reason=-" because the base cannot read "reason" (the defect), and the value "clear" on the
    # tip is covered by the PR-20 test. This check proves that the repair ran and what it removed.
    log = api_repair_log(home)
    for needle in ("lines=1 relinked=0",):
        if needle not in log:
            raise AssertionError(f"{name}: api_repair.log lacks {needle!r}")
    return total


def session_end_resume_skips() -> float:
    # Regression guard for the resume rule: the documented SessionEnd field is "reason".
    name = "session_end_resume_skips"
    home = case_home("s5")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugResume", sid, [jline(6), thinking_line()])
    fixture = tp.read_bytes()
    payload = make_payload("SessionEnd", sid, tp, reason="resume")
    proc = run_hook("SessionEnd", payload, home)
    steps, total = check_summary(proc, "SessionEnd")
    expect_steps(name, "SessionEnd", steps, {"repair": "ok"})
    if tp.read_bytes() != fixture:
        raise AssertionError(f"{name}: transcript changed on a resume (unchanged expected; "
                             f"{tp.stat().st_size} bytes now, {len(fixture)} before)")
    if backups_for(home, sid):
        raise AssertionError(f"{name}: a backup was taken on a resume")
    if removal_records_for(home, tp):
        raise AssertionError(f"{name}: a removal record was written on a resume")
    log = api_repair_log(home)
    for needle in ("from-hook end_reason=resume", "about to be reopened"):
        if needle not in log:
            raise AssertionError(f"{name}: api_repair.log lacks {needle!r}")
    return total


def stop_unknown_extra_field() -> float:
    name = "stop_unknown_extra_field"
    home = case_home("s6")
    sid = str(uuid.uuid4())
    tp = write_transcript(home, "slugExtra", sid, [jline(1), jline(2)])
    fixture = tp.read_bytes()
    payload = make_payload("Stop", sid, tp, **EVENT_FIELDS["Stop"],
                           x_future_field={"nested": [1, "two", None]})
    proc = run_hook("Stop", payload, home)
    steps, total = check_summary(proc, "Stop")
    expect_steps(name, "Stop", steps, {"archive": "ok", "index": "ok", "usage": "ok"})
    expect_mirror(name, home, "slugExtra", sid, fixture)
    return total


def documented_field_names() -> None:
    # No child runs here. This checks the fixture names themselves, so an invented name fails.
    name = "documented_field_names"
    sid = str(uuid.uuid4())
    fake = assert_under_root(Path(ROOT) / "names-only" / f"{sid}.jsonl")   # never created
    cases = [("SessionStart", EVENT_FIELDS["SessionStart"]),
             ("Stop", EVENT_FIELDS["Stop"]),
             ("PreCompact", EVENT_FIELDS["PreCompact"]),
             ("SessionEnd", EVENT_FIELDS["SessionEnd"]),
             ("SessionEnd", {"reason": "resume"})]
    for event, fields in cases:
        d = json.loads(make_payload(event, sid, fake, **fields).decode("ascii"))
        missing = (COMMON | set(fields)) - set(d)
        if missing:
            raise AssertionError(f"{name}: {event} payload lacks {sorted(missing)}")
        for key, allowed in ALLOWED_VALUES.items():
            if key in d and d[key] not in allowed:
                raise AssertionError(f"{name}: {event} field {key} has a value outside the documented list")
        if "stop_hook_active" in d and not isinstance(d["stop_hook_active"], bool):
            raise AssertionError(f"{name}: {event} field stop_hook_active is not a bool")
    return None


def timing_under_60s() -> None:
    # A sanity bound, not a measurement under load.
    name = "timing_under_60s"
    for event, total in TOTALS:
        print(f"  {event} total={total:.2f}s")
    slow = [(e, t) for e, t in TOTALS if not t < 60.0]
    if slow:
        raise AssertionError(f"{name}: child run(s) at or over 60 s: "
                             + ", ".join(f"{e} {t:.2f}s" for e, t in slow))
    return None


TESTS = [session_start_register, stop_archive_mirror, precompact_archive_mirror,
         session_end_clear_repairs_and_archives, session_end_resume_skips,
         stop_unknown_extra_field, documented_field_names, timing_under_60s]


def main() -> int:
    failed = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            total = fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
            continue
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            continue
        if total is None:
            print(f"PASS {name}")
        else:
            print(f"PASS {name} total={total:.2f}s")
    shutil.rmtree(ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
