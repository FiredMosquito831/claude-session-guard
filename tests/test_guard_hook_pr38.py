"""PR-38: the hook dispatcher runs heavy steps detached and keeps every check.

Run from the worktree:  python -I -B tests/test_guard_hook_pr38.py
Each check prints PASS or FAIL <name>: <detail>. Exit 1 if any check fails.

Environment:
  PR38_HOME_BASE  optional folder for the temporary home. The default is tempfile.gettempdir().
  PR38_SCRIPTS    another scripts folder to test (the mutation check uses a scratch copy of scripts/).

The live folder is the .claude folder of the real home, Path.home() / ".claude", read before this test
changes the home. The temporary home's .claude folder must not equal, or sit under, the live folder.
That live folder is the property guarded, not the whole profile. Nothing under it is read, listed,
statted or written. Subprocesses are faked:
inline steps go through a stub of run_tool, and detached launches go through a fake Popen.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = Path(os.environ["PR38_SCRIPTS"]).resolve() if os.environ.get("PR38_SCRIPTS") else REPO / "scripts"
# The live .claude folder of the real home. Read here, before this test changes HOME or USERPROFILE.
LIVE_CLAUDE = os.path.normcase(os.path.abspath(str(Path.home() / ".claude")))
DETACH_FLAGS_VALUE = 0x08000208      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _under(path, base) -> bool:
    p = os.path.normcase(os.path.abspath(str(path)))
    b = os.path.normcase(os.path.abspath(str(base))).rstrip("\\/")
    return p == b or p.startswith(b + os.sep)


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("PASS " + name if ok else f"FAIL {name}: {detail}")


def guard(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:                       # a broken check is a FAIL, not a crash
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    check(name, ok, detail)


# --- the temporary home, set before any plugin import --------------------------

BASE = Path(os.environ.get("PR38_HOME_BASE") or tempfile.gettempdir())
if _under(BASE, LIVE_CLAUDE):                       # checked before anything is created
    print("FAIL temp home outside the live .claude folder: the base is inside it; set PR38_HOME_BASE")
    sys.exit(1)
BASE.mkdir(parents=True, exist_ok=True)
HOME = Path(tempfile.mkdtemp(prefix="pr38_home_", dir=BASE))
if _under(HOME / ".claude", LIVE_CLAUDE):
    print("FAIL temp home outside the live .claude folder: " + str(HOME / ".claude"))
    sys.exit(1)
os.environ["SESSION_GUARD_HOME"] = str(HOME / ".claude")
os.environ["HOME"] = str(HOME)
os.environ["USERPROFILE"] = str(HOME)

sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import guard_hook as gh  # noqa: E402

guard("guardkit home is the temporary home", lambda: (
    _under(gk.CLAUDE_DIR, HOME) and not _under(gk.CLAUDE_DIR, LIVE_CLAUDE), str(gk.CLAUDE_DIR)))


# --- fakes ------------------------------------------------------------------

CALLS = []      # every inline and detached call, in order: {"kind", "cmd", "proc", "stdin"}


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b):
        self.data += bytes(b)
        return len(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, args, kwargs):
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 40000 + len(CALLS)
        self.waits = 0
        self.comms = 0
        self.stdin = FakeStdin() if kwargs.get("stdin") == subprocess.PIPE else None

    def wait(self, *a, **k):
        self.waits += 1
        return 0

    def communicate(self, *a, **k):
        self.comms += 1
        return (b"", b"")

    def poll(self):
        return None


def fake_popen(args, **kwargs):
    proc = FakeProc(args, kwargs)
    CALLS.append({"kind": "detached", "cmd": list(args), "proc": proc, "stdin": None})
    return proc


def fake_run_tool(label, argv_tail, timeout, stdin_bytes=None):
    cmd = [sys.executable, *argv_tail]
    CALLS.append({"kind": "inline", "cmd": cmd, "proc": None, "stdin": stdin_bytes, "timeout": timeout})
    if cmd[1] == "-c":
        out = b"[guard-index] ok\n"                 # the merge snippet prints its status last
    elif Path(cmd[1]).name == "session_detect.py":
        out = b"[session-detect] NEW session 12345678... (method: none, history: 0 entries)\n"
    else:
        out = b""
    return SimpleNamespace(stdout=out, stderr=b"", returncode=0)


def sig(cmd) -> str:
    """A short name for a command: the script and its arguments, without the transcript path."""
    if cmd[1] == "-c":
        return "merge_history"
    rest = [a for a in cmd[2:] if not a.endswith(".jsonl")]
    return " ".join([Path(cmd[1]).name] + rest)


def reset_state():
    d = gk.STATE_DIR
    if d.is_dir():
        for f in d.glob("detached-*.json"):
            f.unlink()


def run_event(event, payload, reset=True):
    if reset:
        reset_state()
    CALLS.clear()
    buf = io.StringIO()
    with mock.patch.object(gh, "run_tool", fake_run_tool), \
            mock.patch("subprocess.Popen", fake_popen), \
            contextlib.redirect_stdout(buf):
        rc = gh.main([event], payload=payload)
    return rc, buf.getvalue().strip(), list(CALLS)


# --- what section 4 of the brief says each event runs --------------------------

EXPECT_PLAN = {
    "SessionStart": [("register", "inline"), ("detect", "inline"), ("scan", "detached"),
                     ("archive", "detached"), ("sweep_api", "detached"), ("usage", "detached")],
    "Stop": [("index", "inline"), ("archive", "detached")],
    "PreCompact": [("index", "inline"), ("archive", "detached"),
                   ("sweep_jsonl", "detached"), ("sweep_api", "detached")],
    "SessionEnd": [("repair", "detached")],
}
EXPECT_SIGS = {
    "SessionStart": ["session_indexer_v2.py register", "session_detect.py", "session_indexer_v2.py scan",
                     "session_archive_v2.py sync", "api_repair_v2.py fix --all", "usage_db_v2.py catch-up"],
    "Stop": ["merge_history", "session_archive_v2.py sync --transcript"],
    "PreCompact": ["merge_history", "session_archive_v2.py sync --transcript",
                   "jsonl_repair_v2.py --all", "api_repair_v2.py fix --all"],
    "SessionEnd": ["api_repair_v2.py from-hook"],
}
EXPECT_DETACHED_COUNT = {"SessionStart": 4, "Stop": 1, "PreCompact": 3, "SessionEnd": 1}
OLD_STEP_FUNCTIONS = ["step_register", "step_archive", "step_index", "step_usage",
                      "step_repair", "step_sweep_api", "step_sweep_jsonl"]   # git show 8c03dfc:scripts/guard_hook.py
OLD_TOOL_SCRIPTS = ["session_indexer_v2.py", "session_archive_v2.py", "usage_db_v2.py",
                    "api_repair_v2.py", "jsonl_repair_v2.py"]

TP = str(HOME / "projects" / "slug-pr38" / "sess-pr38.jsonl")
SS_PAYLOAD = {"session_id": "sess-pr38", "transcript_path": TP, "cwd": str(HOME)}
END_PAYLOAD = {"session_id": "sess-pr38", "transcript_path": TP, "cwd": str(HOME),
               "reason": "other", "end_reason": "other"}
PAYLOADS = {"SessionStart": SS_PAYLOAD, "Stop": SS_PAYLOAD, "PreCompact": SS_PAYLOAD, "SessionEnd": END_PAYLOAD}

RUNS = {}
for _ev in gh.EVENTS:
    RUNS[_ev] = run_event(_ev, PAYLOADS[_ev])


def plan_modes(event):
    return [(s.name, "inline" if s.deadline is not None else "detached") for s in gh._plan(event)]


# --- the seven checks, then the extra ones -------------------------------------

def c1_plan():
    bad = [(ev, plan_modes(ev)) for ev in EXPECT_PLAN if plan_modes(ev) != EXPECT_PLAN[ev]]
    return (not bad), f"plan differs from section 4: {bad}"


def c1_commands():
    bad = []
    for ev in EXPECT_SIGS:
        got = [sig(c["cmd"]) for c in RUNS[ev][2]]
        if got != EXPECT_SIGS[ev]:
            bad.append((ev, got))
    return (not bad), f"commands differ from section 4: {bad}"


def c2_stop_no_usage():
    names = [s.name for s in gh._plan("Stop")]
    usage_calls = [sig(c["cmd"]) for c in RUNS["Stop"][2] if "usage_db" in " ".join(c["cmd"])]
    return ("usage" not in names and not usage_calls), f"Stop names {names}, usage calls {usage_calls}"


def c3_budget():
    totals = {}
    for ev in ("SessionStart", "Stop", "PreCompact"):
        totals[ev] = sum(s.deadline for s in gh._plan(ev) if s.deadline is not None)
    ok = all(t <= 3.0 for t in totals.values()) and gh.INLINE_BUDGET <= 3.0
    return ok, f"inline deadline sums {totals}, INLINE_BUDGET {gh.INLINE_BUDGET}"


def c4_detached_flags_and_no_wait():
    problems = []
    for ev, (_rc, _line, calls) in RUNS.items():
        detached = [c for c in calls if c["kind"] == "detached"]
        if len(detached) != EXPECT_DETACHED_COUNT[ev]:
            problems.append((ev, "launches", len(detached)))
        for c in detached:
            p = c["proc"]
            if os.name == "nt":
                if p.kwargs.get("creationflags") != DETACH_FLAGS_VALUE:
                    problems.append((ev, "creationflags", p.kwargs.get("creationflags")))
            elif p.kwargs.get("start_new_session") is not True:
                problems.append((ev, "start_new_session"))
            if p.kwargs.get("stdin") not in (subprocess.DEVNULL, subprocess.PIPE):
                problems.append((ev, "stdin", p.kwargs.get("stdin")))
            if p.waits or p.comms:
                problems.append((ev, "waited", p.waits, p.comms))
    return (not problems), str(problems)


def c5_allowlist():
    spec = importlib.util.spec_from_file_location("deploy_pr38", REPO / "tools" / "deploy.py")
    dep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dep)
    expect = ["guardkit.py", "api_repair_v2.py", "usage_db_v2.py", "session_archive_v2.py",
              "jsonl_repair_v2.py", "session_indexer_v2.py", "session_detect.py", "guard_hook.py"]
    ok = dep.ALLOWLIST == expect and (SCRIPTS / "guard_hook.py").is_file()
    return ok, str(dep.ALLOWLIST)


def c6_old_checks_kept():
    missing_fns = [n for n in OLD_STEP_FUNCTIONS if not callable(getattr(gh, n, None))]
    seen = {sig(c["cmd"]).split(" ")[0] for ev in RUNS for c in RUNS[ev][2]}
    missing_tools = [t for t in OLD_TOOL_SCRIPTS if t not in seen]
    ok = not missing_fns and not missing_tools and "merge_history" in seen
    return ok, f"missing functions {missing_fns}, tools never run {missing_tools}"


def c7_no_transcript_does_nothing():
    problems = []
    for ev in ("Stop", "PreCompact"):
        for payload in ({"session_id": "x"}, {"transcript_path": ""}):
            rc, line, calls = run_event(ev, payload)
            if rc != 0:
                problems.append((ev, "rc", rc))
            if any("session_archive_v2" in sig(c["cmd"]) for c in calls):
                problems.append((ev, "archive launched"))
            if "archive=skipped" not in line:
                problems.append((ev, "line", line))
    return (not problems), str(problems)


def x1_repair_payload_on_stdin_and_ungated():
    _rc, _line, calls = RUNS["SessionEnd"]
    proc = calls[0]["proc"]
    got = json.loads(proc.stdin.data.decode("utf-8"))
    ok = got == END_PAYLOAD and proc.stdin.closed
    # a previous repair still running must not block the repair of the session that just closed
    reset_state()
    rec = {"pid": os.getpid(), "launched_at": time.time(), "args": []}
    gk.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (gk.STATE_DIR / "detached-repair-from-hook.json").write_text(json.dumps(rec), encoding="utf-8")
    _rc2, _line2, calls2 = run_event("SessionEnd", END_PAYLOAD, reset=False)
    ok = ok and len(calls2) == 1
    return ok, f"payload {got!r}, closed {proc.stdin.closed}, calls while a repair runs {len(calls2)}"


def x2_scan_gate_by_pid_and_age():
    reset_state()
    gk.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = gk.STATE_DIR / "detached-scan.json"
    results = []
    for launched_at, want in ((time.time(), "skipped"), (time.time() - gh.RUNNING_MAX_AGE - 10, "ok")):
        path.write_text(json.dumps({"pid": os.getpid(), "launched_at": launched_at, "args": []}), encoding="utf-8")
        CALLS.clear()
        with mock.patch("subprocess.Popen", fake_popen), contextlib.redirect_stdout(io.StringIO()):
            got = gh.step_scan({})
        results.append((got, want, len(CALLS)))
    reset_state()
    return all(g == w for g, w, _n in results), str(results)


def x3_stale_sweep_lock_does_not_block():
    reset_state()
    lock_path = gk.FileLock(gh.API_SWEEP_LOCK).path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    cases = [("999999\n", 0, "ok"),                         # dead pid, fresh: not held
             (f"{os.getpid()}\n", 0, "skipped"),            # live pid, fresh: held
             (f"{os.getpid()}\n", 10 ** 6, "ok")]           # live pid but older than stale: not held
    for text, age, want in cases:
        lock_path.write_text(text, encoding="utf-8")
        stamp = time.time() - age
        os.utime(lock_path, (stamp, stamp))
        CALLS.clear()
        with mock.patch("subprocess.Popen", fake_popen), contextlib.redirect_stdout(io.StringIO()):
            got = gh.step_sweep_api({})
        results.append((got, want))
    lock_path.unlink(missing_ok=True)
    return all(g == w for g, w in results), str(results)


guard("1a plan names and modes match section 4", c1_plan)
guard("1b commands match section 4, in order", c1_commands)
guard("2 Stop has no usage step and no usage_db call", c2_stop_no_usage)
guard("3 inline deadlines per event sum to at most 3 s", c3_budget)
guard("4 detached steps use the detached flags and are never waited on", c4_detached_flags_and_no_wait)
guard("5 guard_hook.py is in the deploy allowlist, in order", c5_allowlist)
guard("6 every check that existed at 8c03dfc still runs", c6_old_checks_kept)
guard("7 a payload with no transcript path does nothing and exits 0", c7_no_transcript_does_nothing)
guard("x1 the repair gets its payload on stdin and is never gated", x1_repair_payload_on_stdin_and_ungated)
guard("x2 scan is gated by its last launch's pid and age", x2_scan_gate_by_pid_and_age)
guard("x3 a stale or dead sweep lock does not block", x3_stale_sweep_lock_does_not_block)

shutil.rmtree(HOME, ignore_errors=True)
failed = RESULTS.count(False)
print(f"{len(RESULTS) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
