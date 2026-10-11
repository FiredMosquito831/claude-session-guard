#!/usr/bin/env python3
"""
guard_hook -- one entry point per hook event (PR-10). Stdlib only.

Usage: python guard_hook.py <SessionStart|Stop|PreCompact|SessionEnd>

Each event runs its steps in order in this one process. Every step has its own try/except; a step
that fails or times out is recorded and the next step still runs.

Two kinds of step (PR-38):
  inline    the hook waits for it, under a deadline (a hard stop, a subprocess timeout).
            The deadlines of one event add up to at most INLINE_BUDGET, so the hook returns in about a second.
  detached  launched and never waited for. Its output goes to LOG_DIR/<name>.log. Heavy work is detached.

  SessionStart  register    (session_indexer_v2 register, inline)
                detect      (session_detect, inline; prints NEW or RESUME)
                scan        (session_indexer_v2 scan, detached)
                archive-all (session_archive_v2 sync, every transcript, detached; per-file state cache)
                sweep_api   (api_repair_v2 fix --all, detached; cooldown, lock and CleanCache)
                usage       (usage_db_v2 catch-up, detached; 6 h cooldown inside the tool)
  Stop          index       (session_indexer_v2 merge_history(), default throttle, inline)
                archive     (session_archive_v2 sync --transcript, detached; per-file state cache)
  PreCompact    index       (as Stop)
                archive     (as Stop)
                sweep_jsonl (jsonl_repair_v2 --all, detached; cooldown, lock and CleanCache)
                sweep_api   (api_repair_v2 fix --all, detached; cooldown, lock and CleanCache)
  SessionEnd    repair      (api_repair_v2 from-hook, payload on stdin, detached, never gated)

Stop has no usage step: usage catches up at SessionStart instead.

The summary line goes to stdout and to LOG_DIR/guard_hook.log:
  [guard] <Event>: <step>=<ok|fail|timeout|skipped> ... total=<seconds>s
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import guardkit  # noqa: E402

EVENTS = ("SessionStart", "Stop", "PreCompact", "SessionEnd")
USAGE = "usage: guard_hook.py " + "|".join(EVENTS)

# Inline steps and their deadlines in seconds. A deadline is a hard stop (subprocess timeout).
# The hook waits for these steps only, so the deadlines of one event add up to at most INLINE_BUDGET.
# register keeps its 2.5 s (the index lock wait is 2 s); detect is informational, so a stop there loses one line.
INLINE_BUDGET = 3.0
DEADLINES = {"register": 2.5, "detect": 0.5, "index": 3.0}

# One step of an event. deadline is None for a detached step.
Step = namedtuple("Step", "name fn deadline")

# A launch record older than this does not count as running (guards against a reused pid).
RUNNING_MAX_AGE = 3600.0

# Windows process-creation flags, written out as numbers (see launch_detached).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACH_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

API_SWEEP_LOCK = "api_repair-sweep"      # the lock api_repair_v2 sweep takes (FileLock name)
JSONL_SWEEP_LOCK = "jsonl_repair-sweep"  # the lock jsonl_repair_v2 sweep takes (FileLock name)

INDEX_SNIPPET = (
    "import sys; sys.path.insert(0, sys.argv[1]); import session_indexer_v2 as m; "
    "r = m.merge_history(); print('[guard-index] ' + r.status)"
)


class StepTimeout(Exception):
    """A subprocess step ran past its deadline."""


class StepFailed(Exception):
    """A subprocess step exited non-zero."""


def _tail(data, n: int = 300) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return text.strip()[-n:]


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid running? On Windows os.kill(pid, 0) is unsafe (it is CTRL_C_EVENT),
    so the Win32 process handle is checked instead."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5             # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                        # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_held_by_live_process(name: str) -> bool:
    """Fast path only: is the tool's lock file present, younger than its stale age, and held by a
    live pid? The tool takes its own lock as well, so a wrong answer here costs a launch, not data."""
    lock = guardkit.FileLock(name)
    try:
        age = time.time() - os.stat(lock.path).st_mtime
        pid_text = lock.path.read_text(encoding="utf-8", errors="replace").split()[0]
        pid = int(pid_text)
    except (OSError, ValueError, IndexError):
        return False
    if age >= lock.stale:
        return False
    return _pid_alive(pid)


def launch_detached(args, log_path, name=None, stdin_bytes=None) -> int:
    """Start args as a child that outlives this hook. Do not wait on it. Returns its pid.

    Windows: DETACH_FLAGS (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW). POSIX: a new session.
    stdin is DEVNULL, or, when stdin_bytes is given, a pipe that receives those bytes and is closed.
    The payload is a few hundred bytes, which the pipe buffer takes without blocking. stdout and stderr
    go to log_path. The pid and launch time go to STATE_DIR/detached-<name>.json, written atomically.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    name = name or log_path.stem
    if os.name == "nt":
        extra = {"creationflags": DETACH_FLAGS}
    else:
        extra = {"start_new_session": True}
    stdin = subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(list(args), stdin=stdin, stdout=log,
                                stderr=subprocess.STDOUT, close_fds=True, **extra)
    if stdin_bytes is not None:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.flush()
        except OSError as exc:
            guardkit.log_line("guard_hook", f"detached {name}: payload not written: {exc}")
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()
    try:
        guardkit.atomic_write_json(guardkit.STATE_DIR / f"detached-{name}.json",
                                   {"pid": proc.pid, "launched_at": time.time(), "args": list(args)})
    except OSError as exc:
        guardkit.log_line("guard_hook", f"detached {name}: state file not written: {exc}")
    return proc.pid


def _last_launch_running(name: str) -> bool:
    """Is the last launch under this name still running? Reads the pid from its state file. A record
    older than RUNNING_MAX_AGE does not count, so a reused pid cannot block for long."""
    try:
        rec = json.loads((guardkit.STATE_DIR / f"detached-{name}.json").read_text(encoding="utf-8"))
        pid, launched = int(rec["pid"]), float(rec["launched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if time.time() - launched >= RUNNING_MAX_AGE:
        return False
    return _pid_alive(pid)


def _launch_once(name: str, args) -> str:
    """Launch a detached step unless its previous launch is still running. The tool takes its own lock
    as well; this check only stops a new process piling up behind a long one."""
    if _last_launch_running(name):
        guardkit.log_line("guard_hook", f"{name}: skipped: previous launch still running")
        return "skipped"
    launch_detached(args, guardkit.LOG_DIR / f"{name}.log", name=name)
    return "ok"


# --- running one subprocess step under its deadline ---------------------------

def run_tool(label: str, argv_tail, timeout: float, stdin_bytes=None):
    """Run `python <argv_tail>` with a hard deadline. Output is captured, so only the summary line
    reaches this hook's stdout. Raises StepTimeout or StepFailed."""
    cmd = [sys.executable, *argv_tail]
    kwargs = {"input": stdin_bytes} if stdin_bytes is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise StepTimeout(f"{label} ran past {timeout:g}s")
    if proc.returncode != 0:
        raise StepFailed(f"{label} exit {proc.returncode}: {_tail(proc.stderr or proc.stdout)}")
    return proc


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogateescape")


def _script(name: str) -> str:
    return str(SCRIPTS_DIR / name)


# --- the steps. Each returns "ok" or "skipped", or raises. run_event records the rest. ---

def step_register(payload: dict) -> str:
    run_tool("session_indexer_v2 register", [_script("session_indexer_v2.py"), "register"],
             DEADLINES["register"], _json_bytes(payload))
    return "ok"


def step_detect(payload: dict) -> str:
    proc = run_tool("session_detect", [_script("session_detect.py")], DEADLINES["detect"])
    guardkit.log_line("guard_hook", "detect: " + _tail(proc.stdout, 200))
    return "ok"


def step_scan(payload: dict) -> str:
    return _launch_once("scan", [sys.executable, _script("session_indexer_v2.py"), "scan"])


def step_archive_all(payload: dict) -> str:
    # Every transcript. Each file's own state (bytes already archived) is the cache, so an unchanged file is not re-read.
    return _launch_once("archive-all", [sys.executable, _script("session_archive_v2.py"), "sync"])


def step_archive(payload: dict) -> str:
    # One transcript, from the hook payload. The tool's per-file state makes an unchanged transcript a stat and two small reads.
    tp = payload.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return "skipped"
    launch_detached([sys.executable, _script("session_archive_v2.py"), "sync", "--transcript", tp],
                    guardkit.LOG_DIR / "archive-transcript.log", name="archive-transcript")
    return "ok"


def step_index(payload: dict) -> str:
    # The CLI `merge` passes min_interval 0, so it has no throttle. This calls merge_history() with
    # its default throttle, in a child process so the deadline is a hard stop.
    proc = run_tool("session_indexer_v2 merge", ["-c", INDEX_SNIPPET, str(SCRIPTS_DIR)],
                    DEADLINES["index"])
    out = proc.stdout.decode("utf-8", "replace").strip()
    status = out.rsplit(" ", 1)[-1] if out else "error"
    if status in ("ok", "noop"):
        return "ok"
    if status in ("throttled", "busy", "deferred"):
        guardkit.log_line("guard_hook", f"index: skipped: merge status {status}")
        return "skipped"
    raise StepFailed(f"session_indexer_v2 merge status {status}")


def step_usage(payload: dict) -> str:
    # usage_db_v2 catch-up holds its own 6 h cooldown and runs at most 4 times in 24 h.
    return _launch_once("usage", [sys.executable, _script("usage_db_v2.py"), "catch-up"])


def step_repair(payload: dict) -> str:
    # The payload (with reason and end_reason) goes on stdin. from-hook refuses to repair when reason or
    # end_reason is resume. Ungated: every session that closes is repaired. Detached, so SessionEnd does not wait.
    launch_detached([sys.executable, _script("api_repair_v2.py"), "from-hook"],
                    guardkit.LOG_DIR / "repair-from-hook.log", name="repair-from-hook",
                    stdin_bytes=_json_bytes(payload))
    return "ok"


def _launch_guarded(lock_name: str, args, name: str) -> str:
    if lock_held_by_live_process(lock_name):
        guardkit.log_line("guard_hook", f"{name}: skipped: already running ({lock_name} is held)")
        return "skipped"
    launch_detached(args, guardkit.LOG_DIR / f"{name}.log", name=name)
    return "ok"


def step_sweep_api(payload: dict) -> str:
    return _launch_guarded(API_SWEEP_LOCK,
                           [sys.executable, _script("api_repair_v2.py"), "fix", "--all"],
                           "sweep-api_repair")


def step_sweep_jsonl(payload: dict) -> str:
    return _launch_guarded(JSONL_SWEEP_LOCK,
                           [sys.executable, _script("jsonl_repair_v2.py"), "--all"],
                           "sweep-jsonl_repair")


def _plan(event: str) -> list:
    """The steps for each event, in order. Each entry is a Step: its name, its function, and its inline
    deadline (None when detached). Functions are looked up when called, so a step can be replaced."""
    if event == "SessionStart":
        return [Step("register", step_register, DEADLINES["register"]),
                Step("detect", step_detect, DEADLINES["detect"]),
                Step("scan", step_scan, None),
                Step("archive", step_archive_all, None),
                Step("sweep_api", step_sweep_api, None),
                Step("usage", step_usage, None)]
    if event == "Stop":
        return [Step("index", step_index, DEADLINES["index"]),
                Step("archive", step_archive, None)]
    if event == "PreCompact":
        return [Step("index", step_index, DEADLINES["index"]),
                Step("archive", step_archive, None),
                Step("sweep_jsonl", step_sweep_jsonl, None),
                Step("sweep_api", step_sweep_api, None)]
    return [Step("repair", step_repair, None)]          # SessionEnd


def _run_step(event: str, name: str, fn, payload: dict) -> str:
    """Run one step. Any exception or deadline is recorded, never raised."""
    try:
        return "skipped" if fn(payload) == "skipped" else "ok"
    except StepTimeout as exc:
        status, msg = "timeout", str(exc)
    except StepFailed as exc:
        status, msg = "fail", str(exc)
    except Exception as exc:                  # one broken step must not stop the next one
        status, msg = "fail", f"{type(exc).__name__}: {exc}"
    guardkit.log_line("guard_hook", f"{event} {name}: {status}: {msg}")
    return status


def run_event(event: str, payload: dict) -> str:
    """Run every step of one event. Returns the summary line (no newline)."""
    t0 = time.perf_counter()
    results = [(step.name, _run_step(event, step.name, step.fn, payload)) for step in _plan(event)]
    total = time.perf_counter() - t0
    return f"[guard] {event}: " + " ".join(f"{n}={s}" for n, s in results) + f" total={total:.2f}s"


def main(argv=None, payload=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    event = args[0] if args else ""
    if event not in EVENTS:
        print(USAGE)
        return 0
    if payload is None:
        payload = guardkit.read_hook_payload()
    if not isinstance(payload, dict):
        payload = {}
    keep = ("session_id", "transcript_path", "cwd") + (("reason", "end_reason") if event == "SessionEnd" else ())
    payload = {k: payload[k] for k in keep if k in payload}
    t0 = time.perf_counter()
    try:
        line = run_event(event, payload)
    except Exception as exc:                  # a bug in the plan itself must not fail the hook
        guardkit.log_line("guard_hook", f"{event} guard: fail: {type(exc).__name__}: {exc}")
        line = f"[guard] {event}: guard=fail total={time.perf_counter() - t0:.2f}s"
    print(line)
    guardkit.log_line("guard_hook", line)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
