#!/usr/bin/env python3
"""
PR-10 tests for scripts/guard_hook.py. Every test writes only under a fresh temporary
SESSION_GUARD_HOME. In the SessionStart test the Popen and run calls and the register step are
monkeypatched, so the detached launches are only recorded. The Stop and SessionEnd tests run the
detached archive and repair for real, in the temporary home, and wait for each one to exit.

Run: python -I tests\\test_guard_hook_pr10.py
Each check prints PASS <name> or FAIL <name>: <reason>. Exit 0 only when every check passes.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="pr10_guard_hook_home_")
os.environ["SESSION_GUARD_HOME"] = HOME      # set before any plugin code is imported
os.environ["HOME"] = HOME                    # session_detect reads Path.home(); keep it in the temporary home
os.environ["USERPROFILE"] = HOME

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import guardkit  # noqa: E402
import guard_hook  # noqa: E402

if not os.path.normcase(str(guardkit.CLAUDE_DIR)).startswith(os.path.normcase(HOME)):
    print(f"FAIL setup: guardkit.CLAUDE_DIR {guardkit.CLAUDE_DIR} is not under {HOME}")
    sys.exit(1)

SUMMARY_RE = re.compile(
    r"^\[guard\] (SessionStart|Stop|PreCompact|SessionEnd): (\w+=(ok|fail|timeout|skipped) )+total=\d+\.\d{2}s$")


def reset_home() -> None:
    """Empty the temporary home. Refuses to run unless the home is the temporary one."""
    root = Path(guardkit.CLAUDE_DIR)
    if not os.path.normcase(str(root)).startswith(os.path.normcase(HOME)):
        raise AssertionError("refusing to reset a folder outside the temporary home")
    for child in list(root.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()


@contextlib.contextmanager
def patched(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def wait_detached(name: str, timeout: float = 120.0) -> None:
    """Wait until the detached child launched under this name has exited. Its pid is in the state file."""
    state = Path(guardkit.STATE_DIR) / f"detached-{name}.json"
    pid = int(json.loads(state.read_text(encoding="utf-8"))["pid"])
    deadline = time.monotonic() + timeout
    while guard_hook._pid_alive(pid):
        if time.monotonic() > deadline:
            raise AssertionError(f"detached {name} still running after {timeout:g} s")
        time.sleep(0.2)


def jline(n: int) -> bytes:
    return json.dumps({"type": "user", "uuid": f"u{n}", "timestamp": "2026-10-09T10:00:00Z",
                       "message": {"role": "user", "content": f"hello {n}"}}).encode("utf-8")


def thinking_line() -> bytes:
    return json.dumps({"type": "assistant", "uuid": "a1", "timestamp": "2026-10-09T10:00:01Z",
                       "message": {"id": "m1", "role": "assistant",
                                   "content": [{"type": "thinking", "thinking": "   ", "signature": "s"}]}}
                      ).encode("utf-8")


def make_transcript(rel: str, lines) -> Path:
    path = Path(guardkit.PROJECTS_DIR) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + b"\n" for line in lines))
    return path


# --- checks -------------------------------------------------------------------

def test_usage_line():
    reset_home()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard_hook.main(["Bogus"], payload={})
    assert rc == 0, f"exit code {rc}"
    assert out.getvalue().startswith("usage: guard_hook.py SessionStart|Stop|PreCompact|SessionEnd"), \
        f"usage line is {out.getvalue()!r}"
    proc = subprocess.run([sys.executable, str(SCRIPTS / "guard_hook.py"), "Bogus"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=60)
    assert proc.returncode == 0, f"CLI exit code {proc.returncode}"
    assert proc.stdout.decode("utf-8").startswith("usage: guard_hook.py"), "CLI usage line missing"


def test_session_start_order_and_detach():
    reset_home()
    calls = []

    def fake_register(payload):
        calls.append(("register", dict(payload)))
        return "ok"

    class FakeProc:
        pid = 4242

        def wait(self, *a, **k):
            raise AssertionError("the hook waited on the detached sweep")

        def communicate(self, *a, **k):
            raise AssertionError("the hook waited on the detached sweep")

        def poll(self):
            return None

    def fake_popen(args, **kw):
        calls.append(("popen", list(args), dict(kw)))
        return FakeProc()

    def fake_run(cmd, **kw):
        calls.append(("run", list(cmd)))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    payload = {"session_id": "s-start", "transcript_path": "unused", "cwd": "unused"}
    with patched(guard_hook, "step_register", fake_register), \
            patched(subprocess, "Popen", fake_popen), patched(subprocess, "run", fake_run):
        line = guard_hook.run_event("SessionStart", payload)

    assert [c[0] for c in calls] == ["register", "run", "popen", "popen", "popen", "popen"], \
        f"call order was {[c[0] for c in calls]}"
    assert calls[0][1]["session_id"] == "s-start", "register did not get the payload"
    assert calls[1][1][-1].endswith("session_detect.py"), f"detect args were {calls[1][1]}"
    launches = [(c[1], c[2]) for c in calls if c[0] == "popen"]
    got = [Path(a[1]).name + " " + " ".join(a[2:]) for a, _kw in launches]
    assert got == ["session_indexer_v2.py scan", "session_archive_v2.py sync",
                   "api_repair_v2.py fix --all", "usage_db_v2.py catch-up"], f"detached launches were {got}"
    for popen_args, popen_kw in launches:
        assert popen_kw.get("stdin") == subprocess.DEVNULL, "a detached step's stdin is not DEVNULL"
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200 | 0x08000000
            assert popen_kw.get("creationflags") == flags, f"creationflags {popen_kw.get('creationflags')}"
            assert "start_new_session" not in popen_kw, "POSIX flag set on Windows"
        else:
            assert popen_kw.get("start_new_session") is True, "start_new_session not set"
    state = Path(guardkit.STATE_DIR) / "detached-sweep-api_repair.json"
    assert state.is_file(), "detached state file was not written"
    assert json.loads(state.read_text(encoding="utf-8"))["pid"] == 4242, "state file has the wrong pid"
    assert line.startswith("[guard] SessionStart: register=ok detect=ok scan=ok archive=ok "
                           "sweep_api=ok usage=ok total="), line


def test_stop_scoped_to_transcript():
    reset_home()
    a = make_transcript("slugA/sessA.jsonl", [jline(1), jline(2)])
    make_transcript("slugB/sessB.jsonl", [jline(3), jline(4)])
    arch = Path(guardkit.CLAUDE_DIR) / "session-archive" / "transcripts"
    arch_a = arch / "slugA" / "sessA.jsonl"
    arch_b = arch / "slugB" / "sessB.jsonl"
    arch_a.parent.mkdir(parents=True, exist_ok=True)
    arch_b.parent.mkdir(parents=True, exist_ok=True)
    arch_a.write_bytes(jline(1) + b"\n")
    arch_b.write_bytes(jline(3) + b"\n")          # behind its live file, so a sync would change it
    before_b = arch_b.read_bytes()
    line = guard_hook.run_event("Stop", {"session_id": "sessA", "transcript_path": str(a)})
    assert "archive=ok" in line, line
    wait_detached("archive-transcript")
    assert jline(2) in arch_a.read_bytes(), "the new line of transcript A was not archived"
    assert arch_b.read_bytes() == before_b, "the archive of transcript B changed"


def test_step_failure_is_isolated():
    reset_home()
    a = make_transcript("slugC/sessC.jsonl", [jline(5)])
    payload = {"session_id": "sessC", "transcript_path": str(a)}
    launched = []

    def fake_launch(args, log_path, name=None):
        launched.append(name)
        return 1

    def boom_index(payload):
        raise RuntimeError("boom in index")

    def boom_archive(payload):
        raise RuntimeError("boom in archive")

    # A failing index step is recorded, and the archive step after it still runs.
    out = io.StringIO()
    with patched(guard_hook, "step_index", boom_index), patched(guard_hook, "launch_detached", fake_launch), \
            contextlib.redirect_stdout(out):
        rc = guard_hook.main(["Stop"], payload=payload)
    assert rc == 0, f"exit code {rc}"
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, f"stdout has {len(lines)} lines"
    assert "index=fail" in lines[0] and "archive=ok" in lines[0], f"later step did not run: {lines[0]}"
    assert launched == ["archive-transcript"], f"detached launches were {launched}"
    log = (Path(guardkit.LOG_DIR) / "guard_hook.log").read_text(encoding="utf-8")
    assert "Stop index: fail: RuntimeError: boom in index" in log, "the index failure was not logged"

    # A failing archive step is recorded, and the index step before it still ran.
    out = io.StringIO()
    with patched(guard_hook, "step_archive", boom_archive), contextlib.redirect_stdout(out):
        rc = guard_hook.main(["Stop"], payload=payload)
    assert rc == 0, f"exit code {rc}"
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, f"stdout has {len(lines)} lines"
    assert "archive=fail" in lines[0], lines[0]
    assert "index=ok" in lines[0], f"earlier step did not run: {lines[0]}"
    log = (Path(guardkit.LOG_DIR) / "guard_hook.log").read_text(encoding="utf-8")
    assert "Stop archive: fail: RuntimeError: boom in archive" in log, "the failure was not logged"


def test_timeout_is_recorded():
    reset_home()
    launched = []

    def fake_launch(args, log_path, name=None):
        launched.append(name)
        return 1

    def slow_index(payload):
        guard_hook.run_tool("sleeping child", ["-c", "import time; time.sleep(30)"], 0.5)
        return "ok"

    t0 = time.perf_counter()
    with patched(guard_hook, "step_index", slow_index), patched(guard_hook, "launch_detached", fake_launch):
        line = guard_hook.run_event("Stop", {"transcript_path": "unused"})
    elapsed = time.perf_counter() - t0
    assert "index=timeout" in line, line
    assert "archive=ok" in line, f"the step after the timeout did not run: {line}"
    assert launched == ["archive-transcript"], f"detached launches were {launched}"
    assert elapsed < 20, f"the deadline did not stop the child: {elapsed:.1f}s"


def test_session_end_passes_reason():
    reset_home()
    t = make_transcript("slugE/sessE.jsonl", [jline(6), thinking_line()])
    before = t.read_bytes()
    line = guard_hook.run_event("SessionEnd", {"session_id": "sessE", "transcript_path": str(t),
                                               "cwd": "unused", "end_reason": "resume"})
    assert line.startswith("[guard] SessionEnd: repair=ok "), line
    wait_detached("repair-from-hook")
    assert t.read_bytes() == before, "the transcript was changed although the session resumes"
    log = (Path(guardkit.LOG_DIR) / "api_repair.log").read_text(encoding="utf-8")
    assert "from-hook end_reason=resume" in log and "skipped" in log, "end_reason was not passed through"


def test_session_end_reason_resume_skips():
    reset_home()
    sid = "20000000-0000-4000-8000-000000000001"
    t = make_transcript(f"slugR1/{sid}.jsonl", [jline(6), thinking_line()])
    before = t.read_bytes()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard_hook.main(["SessionEnd"], payload={"session_id": sid, "transcript_path": str(t),
                                                       "cwd": "unused", "reason": "resume"})
    line = out.getvalue()
    assert rc == 0, f"exit code {rc}"
    assert line.startswith("[guard] SessionEnd: repair=ok "), line
    wait_detached("repair-from-hook")
    assert t.read_bytes() == before, "the transcript was changed although the session resumes (reason)"
    log = (Path(guardkit.LOG_DIR) / "api_repair.log").read_text(encoding="utf-8")
    assert "from-hook end_reason=resume" in log and "skipped" in log, "reason was not read by from-hook"


def test_session_end_reason_clear_repairs():
    reset_home()
    sid = "20000000-0000-4000-8000-000000000002"
    t = make_transcript(f"slugR2/{sid}.jsonl", [jline(6), thinking_line()])
    before = t.read_bytes()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard_hook.main(["SessionEnd"], payload={"session_id": sid, "transcript_path": str(t),
                                                       "cwd": "unused", "reason": "clear"})
    line = out.getvalue()
    assert rc == 0, f"exit code {rc}"
    assert line.startswith("[guard] SessionEnd: repair=ok "), line
    wait_detached("repair-from-hook")
    assert t.read_bytes() != before, "the transcript was not repaired although the session ended (clear)"


def test_session_end_reason_wins_over_end_reason():
    reset_home()
    sid = "20000000-0000-4000-8000-000000000003"
    t = make_transcript(f"slugR3/{sid}.jsonl", [jline(6), thinking_line()])
    before = t.read_bytes()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard_hook.main(["SessionEnd"], payload={"session_id": sid, "transcript_path": str(t),
                                                       "cwd": "unused", "reason": "clear",
                                                       "end_reason": "resume"})
    line = out.getvalue()
    assert rc == 0, f"exit code {rc}"
    assert line.startswith("[guard] SessionEnd: repair=ok "), line
    wait_detached("repair-from-hook")
    assert t.read_bytes() != before, "reason=clear did not win over end_reason=resume"
    log = (Path(guardkit.LOG_DIR) / "api_repair.log").read_text(encoding="utf-8")
    assert "from-hook end_reason=clear" in log, "reason did not take precedence over end_reason"


def test_session_end_end_reason_fallback():
    reset_home()
    sid = "20000000-0000-4000-8000-000000000004"
    t = make_transcript(f"slugR4/{sid}.jsonl", [jline(6), thinking_line()])
    before = t.read_bytes()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard_hook.main(["SessionEnd"], payload={"session_id": sid, "transcript_path": str(t),
                                                       "cwd": "unused", "end_reason": "resume"})
    line = out.getvalue()
    assert rc == 0, f"exit code {rc}"
    assert line.startswith("[guard] SessionEnd: repair=ok "), line
    wait_detached("repair-from-hook")
    assert t.read_bytes() == before, "the transcript was changed; end_reason alone must still skip"
    log = (Path(guardkit.LOG_DIR) / "api_repair.log").read_text(encoding="utf-8")
    assert "from-hook end_reason=resume" in log and "skipped" in log, "end_reason fallback was not read"


def test_no_double_launch():
    reset_home()
    lock = guardkit.FileLock("api_repair-sweep")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    launched = []

    def fake_launch(args, log_path, name=None):
        launched.append(list(args))
        return 1

    def sweeps():
        return [a for a in launched if "fix" in a]

    # A lock held by this live test process: the sweep launch must be skipped.
    lock.path.write_text(f"{os.getpid()} {time.time()}\n", encoding="utf-8")
    with patched(guard_hook, "launch_detached", fake_launch):
        line = guard_hook.run_event("SessionStart", {"session_id": "s"})
    assert "sweep_api=skipped" in line, line
    assert sweeps() == [], "a second sweep was launched while the lock was held"

    # A lock whose pid has exited: the sweep launch must go ahead.
    launched.clear()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=60)
    lock.path.write_text(f"{dead.pid} {time.time()}\n", encoding="utf-8")
    with patched(guard_hook, "launch_detached", fake_launch):
        line = guard_hook.run_event("SessionStart", {"session_id": "s"})
    assert "sweep_api=ok" in line, line
    assert len(sweeps()) == 1, "a sweep was not launched after the lock holder exited"
    assert dead.returncode == 0, f"helper process exit {dead.returncode}"


def test_summary_line_format():
    reset_home()
    cases = [("SessionEnd", json.dumps({"session_id": "s", "end_reason": "resume"}).encode("utf-8")),
             ("Stop", b"")]
    for event, stdin_bytes in cases:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "guard_hook.py"), event],
                              input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
        assert proc.returncode == 0, f"{event}: exit code {proc.returncode}"
        out = proc.stdout.decode("utf-8")
        lines = out.splitlines()
        assert len(lines) == 1 and out.endswith("\n"), f"{event}: stdout has {len(lines)} lines"
        assert SUMMARY_RE.match(lines[0]), f"{event}: summary does not match: {lines[0]!r}"
    wait_detached("repair-from-hook")


TESTS = [test_usage_line, test_session_start_order_and_detach, test_stop_scoped_to_transcript,
         test_step_failure_is_isolated, test_timeout_is_recorded, test_session_end_passes_reason,
         test_session_end_reason_resume_skips, test_session_end_reason_clear_repairs,
         test_session_end_reason_wins_over_end_reason, test_session_end_end_reason_fallback,
         test_no_double_launch, test_summary_line_format]


def main() -> int:
    failed = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
