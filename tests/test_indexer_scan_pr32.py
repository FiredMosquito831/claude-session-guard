#!/usr/bin/env python3
"""
PR-32 tests: the start-up scan in session_indexer_v2 (scan), checked against session_indexer.py
(scan) and against the base commit 3dfa104 (register).

Each run builds its own temporary homes under a test root. The root must not be under the real
real user profile; the run stops before it creates anything there. v1 and v2 are
redirected with HOME and USERPROFILE (v1 reads Path.home()) and SESSION_GUARD_HOME (v2 reads it).
The temporary folder is removed at the end.

Test root: PR32_TEST_ROOT if set, else C:\\tmp\\claude\\pr32-tests on Windows, else the system temp folder.
Git repository for the base commit: PR32_GIT_REPO if set, else this repository.

Run from the worktree:  python -I -B tests/test_indexer_scan_pr32.py
Exit 0 when every check passes, 1 otherwise. Each check prints PASS or FAIL with its detail.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = REPO / "scripts"
V1 = SCRIPTS / "session_indexer.py"
V2 = SCRIPTS / "session_indexer_v2.py"
GIT_REPO = Path(os.environ.get("PR32_GIT_REPO") or REPO)
BASE_REF = "3dfa104"
REGISTER_WAIT_LIMIT = 3.5  # seconds: the register wait is 2 s, plus interpreter start-up
REAL_PROFILE = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())  # read before the redirect
PY = sys.executable


def _under(child, parent) -> bool:
    c = os.path.normcase(os.path.abspath(str(child)))
    p = os.path.normcase(os.path.abspath(str(parent))).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _pick_root() -> Path:
    env = os.environ.get("PR32_TEST_ROOT")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(r"C:\tmp\claude\pr32-tests")
    return Path(tempfile.gettempdir())


ROOT = _pick_root()
if _under(ROOT, REAL_PROFILE):
    print(f"FAIL setup: test root {ROOT} is under {REAL_PROFILE}; set PR32_TEST_ROOT to a folder outside it")
    sys.exit(1)
ROOT.mkdir(parents=True, exist_ok=True)
T = Path(tempfile.mkdtemp(prefix="pr32-", dir=str(ROOT)))

# Redirect both versions before any plugin code is imported.
MAIN_HOME = T / "home-main"
os.environ["HOME"] = str(MAIN_HOME)
os.environ["USERPROFILE"] = str(MAIN_HOME)
os.environ["SESSION_GUARD_HOME"] = str(MAIN_HOME / ".claude")
sys.path.insert(0, str(SCRIPTS))
import session_indexer as v1  # noqa: E402
import session_indexer_v2 as v2  # noqa: E402
import guardkit as gk  # noqa: E402


# --- helpers ----------------------------------------------------------------------

def _claude(home: Path) -> Path:
    return home / ".claude"


def _throttle(home: Path) -> Path:
    return _claude(home) / "session-tools" / "state" / "index_last_merge"


def _ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _env(home: Path) -> dict:
    if not _under(home, T):
        raise AssertionError(f"test home {home} is outside {T}")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["SESSION_GUARD_HOME"] = str(_claude(home))
    return env


def _run(script: Path, args: list, home: Path, stdin: str = None):
    kwargs = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    p = subprocess.run([PY, "-I", "-B", str(script), *args], env=_env(home), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=150, **kwargs)
    return p.returncode, p.stdout, p.stderr


def _py(code: str, home: Path, *argv: str):
    p = subprocess.run([PY, "-I", "-B", "-c", code, *argv], env=_env(home), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
    return p.returncode, p.stdout, p.stderr


def v2_scan(home: Path, full: bool = False):
    return _run(V2, ["scan", "--full"] if full else ["scan"], home)


def v1_scan(home: Path):
    return _run(V1, ["scan"], home)


def clear_throttle(home: Path) -> None:
    try:
        _throttle(home).unlink()
    except FileNotFoundError:
        pass


def _write(path: Path, items: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    for item in items:
        if isinstance(item, bytes):
            chunks.append(item)
        else:
            chunks.append((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
    path.write_bytes(b"".join(chunks))


def _user(sid: str, iso: str, content, **extra) -> dict:
    obj = {"type": "user", "sessionId": sid, "timestamp": iso, "message": {"role": "user", "content": content}}
    obj.update(extra)
    return obj


def _write_old(home: Path, proj: str, sid: str, iso: str, text: str, age: float = 7200.0) -> Path:
    """A transcript whose mtime is `age` seconds in the past, so an incremental scan skips it."""
    f = _claude(home) / "projects" / proj / f"{sid}.jsonl"
    _write(f, [_user(sid, iso, text)])
    past = time.time() - age
    os.utime(f, (past, past))
    return f


def make_home(home: Path) -> Path:
    """Fixture: two projects, subagent and compaction files, a loose file, a history and a pending index."""
    c = _claude(home)
    p = c / "projects"
    _write(p / "proj-alpha" / "s1.jsonl", [
        {"type": "user", "cwd": "C:\\work\\alpha", "sessionId": "s1", "timestamp": "2026-10-10T10:00:00Z",
         "message": {"role": "user", "content": "first prompt about alpha"}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-10T10:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "a reply"}]}},
        _user("s1", "2026-10-10T10:00:02Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]),
        _user("s1", "2026-10-10T10:00:03Z", "meta note here", isMeta=True),
        _user("s1", "2026-10-10T10:00:04Z", "summary of earlier work", isCompactSummary=True),
        _user("s1", "2026-10-10T10:00:05Z", "ok"),
        _user("s1", "2026-10-10T10:00:06Z", "yes!"),
        _user("s1", "2026-10-10T10:00:07Z", [{"type": "text", "text": "list one"}, {"type": "text", "text": "list two"}]),
        {"type": "user_message", "sessionId": "s1", "timestamp": "2026-10-10T10:00:08Z",
         "message": {"content": "user_message type prompt"}},
        {"type": "user", "sessionId": "s1", "timestamp": "2026-10-10T10:00:09Z", "message": "plain string message"},
        _user("s1", "2026-10-10T10:00:10Z", ["bare string item", {"type": "text", "text": "and text"}]),
        _user("s1", "2026-10-10T10:00:11Z", "   spaced   "),
        _user("s1", "2026-10-10T10:00:12Z", "   "),
        _user("s1", "2026-10-10T10:00:13Z", [{"type": "text", "text": "partly"}, {"type": "tool_result", "content": "x"}]),
        b"{not valid json\n",
        b"\n",
    ])
    _write(p / "proj-alpha" / "s2.jsonl", [
        _user("s2", "2026-10-10T10:05:00Z", "second session prompt, no cwd"),
        _user("s2", "2026-10-10T10:05:01Z", "L" * 600),
        _user("s2", "2026-10-10T10:05:02Z", "no"),
    ])
    _write(p / "proj-beta" / "s3.jsonl", [
        {"type": "system", "cwd": "D:\\beta", "sessionId": "s3", "timestamp": "2026-10-10T09:59:59Z"},
        _user("s3", "2026-10-10T10:10:00Z", "café résumé ✓ Ελληνικά"),
    ])
    _write(p / "proj-beta" / "s3" / "subagents" / "agent-a.jsonl",
           [_user("s3a", "2026-10-10T10:10:01Z", "subagent prompt must be skipped")])
    _write(p / "proj-beta" / "s3" / "subagents" / "workflows" / "wf1" / "agent-b.jsonl",
           [_user("s3b", "2026-10-10T10:10:02Z", "workflow subagent prompt must be skipped")])
    _write(p / "proj-beta" / "s4-acompact.jsonl",
           [_user("s4", "2026-10-10T10:11:00Z", "compaction file prompt must be skipped")])
    _write(p / "proj-beta" / "notes.txt", [b"not a transcript\n"])
    _write(p / "loose.jsonl", [_user("loose", "2026-10-10T10:12:00Z", "loose file prompt must be skipped")])
    _write(c / "history.jsonl", [
        {"display": "first prompt about alpha", "pastedContents": {}, "timestamp": _ms("2026-10-10T10:00:00Z"),
         "project": "C:\\work\\alpha", "sessionId": "s1"},
        {"display": "old unrelated prompt", "pastedContents": {}, "timestamp": 1, "project": "x", "sessionId": "sold"},
    ])
    _write(c / ".session_index.jsonl", [
        {"display": "pending prompt from earlier", "pastedContents": {}, "timestamp": _ms("2026-10-10T09:00:00Z"),
         "project": "x", "sessionId": "spend"},
    ])
    return home


# Every prompt the walk must extract: (sessionId, timestamp, display, project).
ALL_KEPT = [
    ("s1", "2026-10-10T10:00:00Z", "first prompt about alpha", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:06Z", "yes!", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:07Z", "list one list two", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:08Z", "user_message type prompt", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:09Z", "plain string message", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:10Z", "bare string item and text", "C:\\work\\alpha"),
    ("s1", "2026-10-10T10:00:11Z", "spaced", "C:\\work\\alpha"),
    ("s2", "2026-10-10T10:05:00Z", "second session prompt, no cwd", "proj-alpha"),
    ("s2", "2026-10-10T10:05:01Z", "L" * 500, "proj-alpha"),
    ("s3", "2026-10-10T10:10:00Z", "café résumé ✓ Ελληνικά", "D:\\beta"),
]
OLD_HISTORY_KEYS = {("s1", _ms("2026-10-10T10:00:00Z"), "first prompt about alpha"),
                    ("sold", 1, "old unrelated prompt")}
PENDING_KEY = ("spend", _ms("2026-10-10T09:00:00Z"), "pending prompt from earlier")


def expected_added() -> set:
    """Keys a complete scan adds to history.jsonl: every new prompt, plus the pending index entry."""
    keys = {(sid, _ms(iso), disp) for sid, iso, disp, _proj in ALL_KEPT[1:]}
    keys.add(PENDING_KEY)
    return keys


def key_of(o: dict) -> tuple:
    return (o.get("sessionId", ""), o.get("timestamp", 0), (o.get("display", "") or "")[:500])


def _bytes(path: Path):
    return path.read_bytes() if path.exists() else None


def _objs(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(raw.decode("utf-8")) for raw in path.read_bytes().split(b"\n") if raw.strip()]


def _keys(path: Path) -> list:
    return [key_of(o) for o in _objs(path)]


def _history(home: Path) -> Path:
    return _claude(home) / "history.jsonl"


def _index(home: Path) -> Path:
    return _claude(home) / ".session_index.jsonl"


def _snapshot(home: Path) -> dict:
    return {"index": _bytes(_index(home)), "history": _bytes(_history(home)),
            "watermark": _bytes(_claude(home) / ".session_scan_last")}


def _fail(problems: list) -> None:
    if problems:
        raise AssertionError("; ".join(problems))


READ_WM_CODE = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import session_indexer as v\n"
    "print(v.SCAN_WATERMARK_FILE)\n"
    "print(float(v.SCAN_WATERMARK_FILE.read_text().strip()))\n"
)

HOLDER_CODE = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import guardkit as gk\n"
    "lock = gk.lock_wait('session-index', timeout=10)\n"
    "print('held' if lock is not None else 'not-held', flush=True)\n"
    "sys.stdin.readline()\n"
    "if lock is not None:\n"
    "    lock.release()\n"
)

CRASH_CODE = (
    "import os, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import session_indexer_v2 as v\n"
    "def _die(*args, **kwargs):\n"
    "    os._exit(9)\n"
    "v.merge_history = _die\n"
    "v.scan_new_sessions(full=False)\n"
)


# --- checks -----------------------------------------------------------------------

def check_setup():
    problems = []
    if Path.home() != MAIN_HOME:
        problems.append(f"Path.home() is {Path.home()}")
    if v1.CLAUDE_DIR != _claude(MAIN_HOME):
        problems.append(f"v1 CLAUDE_DIR is {v1.CLAUDE_DIR}")
    if gk.CLAUDE_DIR != _claude(MAIN_HOME):
        problems.append(f"guardkit CLAUDE_DIR is {gk.CLAUDE_DIR}")
    if v2.THROTTLE_FILE != _throttle(MAIN_HOME):
        problems.append(f"v2 THROTTLE_FILE is {v2.THROTTLE_FILE}")
    if v1.SCAN_WATERMARK_FILE != _claude(MAIN_HOME) / ".session_scan_last":
        problems.append(f"v1 watermark is {v1.SCAN_WATERMARK_FILE}")
    if v2.SCAN_WATERMARK_FILE != v1.SCAN_WATERMARK_FILE:
        problems.append(f"v2 watermark {v2.SCAN_WATERMARK_FILE} differs from v1 {v1.SCAN_WATERMARK_FILE}")
    if (v2.LOCK_NAME, v2.LOCK_TIMEOUT, v2.DEFAULT_MIN_INTERVAL) != ("session-index", 30.0, 300.0):
        problems.append("lock name, lock timeout or throttle interval differ from the brief")
    _fail(problems)


def _walk_like_v1(projects: Path) -> list:
    found = []
    for pdir in sorted(projects.iterdir()):
        if not pdir.is_dir():
            continue
        for f in sorted(pdir.glob("*.jsonl")):
            if "subagents" in str(f) or "acompact" in str(f):
                continue
            found.append((f, pdir.name))
    return found


def check_extraction():
    home = make_home(T / "extract")
    projects = _claude(home) / "projects"
    problems, got = [], []
    for f, pname in _walk_like_v1(projects):
        a = [(p["sessionId"], p["timestamp"], p["display"], p["project"]) for p in v1.extract_prompts(f, pname)]
        b = [(p["sessionId"], p["timestamp"], p["display"], p["project"]) for p in v2.extract_prompts(f, pname)]
        if a != b:
            problems.append(f"{f.name}: v1 gives {len(a)} prompts, v2 gives {len(b)}, and they differ")
        got.extend(a)
    expected = [(sid, _ms(iso), disp, proj) for sid, iso, disp, proj in ALL_KEPT]
    if sorted(got) != sorted(expected):
        problems.append(f"extracted {len(got)} prompts, expected {len(expected)}; "
                        f"missing {sorted(set(expected) - set(got))[:2]}, extra {sorted(set(got) - set(expected))[:2]}")
    for skipped in (projects / "proj-beta" / "s3" / "subagents" / "agent-a.jsonl",
                    projects / "proj-beta" / "s4-acompact.jsonl",
                    projects / "loose.jsonl"):
        for mod in (v1, v2):
            if not mod.extract_prompts(skipped, "x"):
                problems.append(f"fixture {skipped.name} yields no prompts, so its skip is not exercised")
    _fail(problems)


def check_scan_equivalence():
    h1, h2 = make_home(T / "eq-v1"), make_home(T / "eq-v2")
    orig = _bytes(_history(h1))
    orig_objs = _objs(_history(h1))
    rc1, _, err1 = v1_scan(h1)
    rc2, _, err2 = v2_scan(h2)
    problems = []
    if rc1 != 0:
        problems.append(f"v1 scan rc={rc1}: {err1[-200:]}")
    if rc2 != 0:
        problems.append(f"v2 scan rc={rc2}: {err2[-200:]}")
    before = {key_of(o) for o in orig_objs}
    after1, after2 = _objs(_history(h1)), _objs(_history(h2))
    new1 = {key_of(o) for o in after1} - before
    new2 = {key_of(o) for o in after2} - before
    exp = expected_added()
    if new1 != exp:
        problems.append(f"v1 added {len(new1)} keys, expected {len(exp)}; differ by {sorted(new1 ^ exp)[:2]}")
    if new2 != exp:
        problems.append(f"v2 added {len(new2)} keys, expected {len(exp)}; differ by {sorted(new2 ^ exp)[:2]}")
    if new1 != new2:
        problems.append("v1 and v2 added different sets of keys")
    for label, after in (("v1", after1), ("v2", after2)):
        keys_after = [key_of(o) for o in after]
        if len(keys_after) != len(set(keys_after)):
            problems.append(f"{label}: duplicate keys in history")
        for o in orig_objs:
            if o not in after:
                problems.append(f"{label}: an original entry was removed or changed: {o.get('display', '')[:40]!r}")
    if not (_bytes(_history(h2)) or b"").startswith(orig):
        problems.append("v2 changed bytes already in history (the write is not an append)")
    _fail(problems)


def check_idempotence():
    h = make_home(T / "idem")
    rc, out1, err = v2_scan(h)
    if rc != 0 or "merge ok" not in out1:
        raise AssertionError(f"first scan rc={rc}, output {out1.strip()[:160]!r}")
    first = _snapshot(h)
    problems = []
    rc, out2, err = v2_scan(h)       # inside the 300 s window: the merge is throttled
    if rc != 0:
        problems.append(f"second scan rc={rc}: {err[-200:]}")
    if "0 new prompt(s) queued" not in out2:
        problems.append(f"second scan queued prompts: {out2.strip()[:160]!r}")
    if "merge throttled" not in out2:
        problems.append(f"second scan's merge was not throttled: {out2.strip()[:160]!r}")
    if _snapshot(h)["index"] != first["index"] or _snapshot(h)["history"] != first["history"]:
        problems.append("second scan changed index or history")
    clear_throttle(h)
    rc, out3, err = v2_scan(h)       # after the window: the merge runs, and still adds nothing
    if "0 new prompt(s) queued" not in out3 or "merge ok (0 added)" not in out3:
        problems.append(f"third scan output {out3.strip()[:160]!r}")
    if _snapshot(h)["index"] != first["index"] or _snapshot(h)["history"] != first["history"]:
        problems.append("third scan changed index or history")
    _fail(problems)


def check_watermark():
    problems = []
    # A: v2 writes the watermark; v1 reads it; v2 honours it; --full re-reads and adds only what is missing.
    h = make_home(T / "wm-a")
    wm_path = _claude(h) / ".session_scan_last"
    t0 = time.time()
    rc, _, err = v2_scan(h)
    t1 = time.time()
    if rc != 0:
        raise AssertionError(f"v2 scan rc={rc}: {err[-200:]}")
    if not wm_path.exists():
        raise AssertionError("v2 scan wrote no watermark")
    wm = float(wm_path.read_text(encoding="utf-8").strip())
    if not (t0 <= wm <= t1):
        problems.append(f"watermark {wm} is outside the scan window [{t0:.3f}, {t1:.3f}]")
    rc, out, err = _py(READ_WM_CODE, h, str(SCRIPTS))
    lines = out.splitlines()
    if rc != 0 or len(lines) < 2:
        problems.append(f"v1 could not read the watermark: rc={rc} {err[-200:]}")
    else:
        if Path(lines[0]) != wm_path:
            problems.append(f"v1 reads the watermark from {lines[0]}, not {wm_path}")
        if float(lines[1]) != wm:
            problems.append(f"v1 read {lines[1]}, v2 wrote {wm}")
    late = ("s6", _ms("2026-10-10T11:00:00Z"), "late prompt from an old transcript")
    _write_old(h, "proj-beta", "s6", "2026-10-10T11:00:00Z", "late prompt from an old transcript")
    clear_throttle(h)
    rc, _, err = v2_scan(h)          # incremental: the old file is past the cutoff
    if rc != 0:
        problems.append(f"incremental scan rc={rc}: {err[-200:]}")
    if late in set(_keys(_index(h))):
        problems.append("incremental scan read an old transcript despite the watermark")
    before_full = set(_keys(_history(h)))
    clear_throttle(h)
    rc, out, err = v2_scan(h, full=True)  # --full ignores the watermark
    if rc != 0:
        problems.append(f"--full scan rc={rc}: {err[-200:]}")
    keys_full = _keys(_history(h))
    added = set(keys_full) - before_full
    if added != {late}:
        problems.append(f"--full added {sorted(added)}, expected only the late prompt")
    if len(keys_full) != len(set(keys_full)):
        problems.append("duplicate keys in history after --full")
    # B: v1 writes the watermark; v2 honours it. Only the index is checked: v1 rewrites history.jsonl,
    # which makes v2's merge refuse, and that is a separate matter.
    h2 = make_home(T / "wm-b")
    rc, _, err = v1_scan(h2)
    if rc != 0:
        raise AssertionError(f"v1 scan rc={rc}: {err[-200:]}")
    wm_v1_path = _claude(h2) / ".session_scan_last"
    wm_v1 = float(wm_v1_path.read_text(encoding="utf-8").strip())
    late2 = ("s7", _ms("2026-10-10T11:30:00Z"), "late prompt two")
    _write_old(h2, "proj-beta", "s7", "2026-10-10T11:30:00Z", "late prompt two")
    clear_throttle(h2)
    rc, _, err = v2_scan(h2)
    if rc != 0:
        problems.append(f"v2 scan after v1 rc={rc}: {err[-200:]}")
    if late2 in set(_keys(_index(h2))):
        problems.append("v2 read an old transcript despite the watermark v1 wrote")
    if float(wm_v1_path.read_text(encoding="utf-8").strip()) < wm_v1:
        problems.append("v2 did not move the watermark forward")
    # C: v1 reads the watermark of v2 and skips the old transcript, as it did before.
    h3 = make_home(T / "wm-c")
    rc, _, err = v2_scan(h3)
    if rc != 0:
        raise AssertionError(f"v2 scan rc={rc}: {err[-200:]}")
    late3 = ("s9", _ms("2026-10-10T11:45:00Z"), "late prompt three")
    _write_old(h3, "proj-beta", "s9", "2026-10-10T11:45:00Z", "late prompt three")
    rc, _, err = v1_scan(h3)
    if rc != 0:
        problems.append(f"v1 scan after v2 rc={rc}: {err[-200:]}")
    if late3 in set(_keys(_index(h3))) | set(_keys(_history(h3))):
        problems.append("v1 read an old transcript despite the watermark v2 wrote")
    _fail(problems)


def check_lock_busy():
    h = make_home(T / "busy")
    before = _snapshot(h)
    holder = subprocess.Popen([PY, "-I", "-B", "-c", HOLDER_CODE, str(SCRIPTS)], env=_env(h),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace")
    holder_rc = None
    try:
        first = holder.stdout.readline().strip()
        if first != "held":
            raise AssertionError(f"the holder did not take the lock (printed {first!r})")
        rc, out, err = v2_scan(h)    # waits LOCK_TIMEOUT (30 s), then gives up
        after = _snapshot(h)
    finally:
        try:
            holder.stdin.write("\n")
            holder.stdin.flush()
        except OSError:
            pass
        try:
            holder_rc = holder.wait(timeout=30)
        except subprocess.TimeoutExpired:
            holder.kill()
    problems = []
    if rc != 0:
        problems.append(f"scan rc={rc}: {err[-200:]}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) != 1 or "held by another process" not in lines[0]:
        problems.append(f"scan printed {lines!r}, expected one busy line")
    if after != before:
        problems.append("scan wrote something while the lock was held")
    if holder_rc != 0:
        problems.append(f"holder exited {holder_rc}")
    _fail(problems)


def check_crash_safety():
    h = make_home(T / "crash")
    rc, _, err = _py(CRASH_CODE, h, str(SCRIPTS))
    if rc != 9:
        raise AssertionError(f"crash child exited {rc}, expected 9: {err[-300:]}")
    problems = []
    if set(_keys(_history(h))) != OLD_HISTORY_KEYS:
        problems.append("history changed although the merge never ran")
    if not expected_added() <= set(_keys(_index(h))):
        problems.append("index lacks the queued prompts after the crash")
    index_after_crash = _bytes(_index(h))
    clear_throttle(h)
    rc, out, err = v2_scan(h)
    if rc != 0:
        problems.append(f"recovery scan rc={rc}: {err[-200:]}")
    if "0 new prompt(s) queued" not in out:
        problems.append(f"recovery scan queued again: {out.strip()[:160]!r}")
    if _bytes(_index(h)) != index_after_crash:
        problems.append("recovery scan wrote to the index")
    expected_hist = OLD_HISTORY_KEYS | expected_added()
    hist = _keys(_history(h))
    if set(hist) != expected_hist:
        problems.append(f"history after recovery differs by {sorted(set(hist) ^ expected_hist)[:2]}")
    if len(hist) != len(set(hist)):
        problems.append("duplicate keys in history after recovery")
    idx = _keys(_index(h))
    if len(idx) != len(set(idx)):
        problems.append("duplicate keys in the index after recovery")
    _fail(problems)


def check_register_matches_base():
    base_dir = T / "base-scripts"
    base_dir.mkdir(parents=True, exist_ok=True)
    for name in ("session_indexer_v2.py", "guardkit.py"):
        p = subprocess.run(["git", "-C", str(GIT_REPO), "show", f"{BASE_REF}:scripts/{name}"], capture_output=True)
        if p.returncode != 0:
            raise AssertionError(f"git show {BASE_REF}:scripts/{name} failed: {p.stderr.decode('utf-8', 'replace')[:200]}")
        (base_dir / name).write_bytes(p.stdout)
    results = {}
    for label, script in (("base", base_dir / "session_indexer_v2.py"), ("new", V2)):
        home = make_home(T / f"reg-{label}")
        payload = json.dumps({
            "session_id": "s1",
            "transcript_path": str(_claude(home) / "projects" / "proj-alpha" / "s1.jsonl"),
            "hook_event_name": "SessionStart",
            "source": "startup",
        })
        rc, out, err = _run(script, ["register"], home, stdin=payload)
        results[label] = (rc, out, err, _bytes(_index(home)), _bytes(_history(home)))
    b, n = results["base"], results["new"]
    problems = []
    if n[0] != 0:
        problems.append(f"new register rc={n[0]}: {n[2][-200:]}")
    if "register s1: 7 prompts, 7 queued" not in n[1]:
        problems.append(f"new register output {n[1].strip()[:160]!r}")
    if b[0] != n[0] or b[1] != n[1]:
        problems.append(f"register output differs: base {b[1].strip()[:120]!r}; new {n[1].strip()[:120]!r}")
    if b[3] != n[3]:
        problems.append("register wrote a different index than the base commit")
    if b[4] != n[4]:
        problems.append("register wrote a different history than the base commit")
    _fail(problems)


def check_throttle_window():
    h = make_home(T / "throttle")
    rc, out, err = v2_scan(h)
    if rc != 0 or "merge ok" not in out:
        raise AssertionError(f"first scan rc={rc}, output {out.strip()[:160]!r}")
    fresh = ("s8", _ms("2026-10-10T10:20:00Z"), "fresh prompt inside the throttle window")
    _write(_claude(h) / "projects" / "proj-alpha" / "s8.jsonl",
           [_user("s8", "2026-10-10T10:20:00Z", "fresh prompt inside the throttle window")])
    problems = []
    rc, out, err = v2_scan(h)        # inside the window
    if "merge throttled" not in out:
        problems.append(f"merge ran inside the window: {out.strip()[:160]!r}")
    if fresh in set(_keys(_history(h))):
        problems.append("prompt reached history inside the window")
    if fresh not in set(_keys(_index(h))):
        problems.append("prompt was not queued in the index inside the window")
    clear_throttle(h)
    rc, out, err = v2_scan(h)        # after the window
    count = _keys(_history(h)).count(fresh)
    if count != 1:
        problems.append(f"after the window the prompt appears {count} times in history")
    _fail(problems)


def _start_holder(home: Path):
    """A subprocess that holds the session-index lock until a line is written to its stdin."""
    return subprocess.Popen([PY, "-I", "-B", "-c", HOLDER_CODE, str(SCRIPTS)], env=_env(home),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")


def _release_holder(holder) -> int:
    try:
        holder.stdin.write("\n")
        holder.stdin.flush()
    except OSError:
        pass
    try:
        return holder.wait(timeout=30)
    except subprocess.TimeoutExpired:
        holder.kill()
        return -1


def check_register_lock_busy():
    h = make_home(T / "reg-busy")
    rc, out, err = v2_scan(h)
    if rc != 0 or "merge ok" not in out:
        raise AssertionError(f"first scan rc={rc}, output {out.strip()[:160]!r}")
    # A session that started after the last scan: its transcript is newer than the watermark's cutoff.
    transcript = _claude(h) / "projects" / "proj-alpha" / "s10.jsonl"
    _write(transcript, [
        _user("s10", "2026-10-10T12:00:00Z", "backfill prompt one"),
        _user("s10", "2026-10-10T12:00:01Z", "backfill prompt two"),
    ])
    payload = json.dumps({"session_id": "s10", "transcript_path": str(transcript),
                          "hook_event_name": "SessionStart", "source": "resume"})
    backfill = [("s10", _ms("2026-10-10T12:00:00Z"), "backfill prompt one"),
                ("s10", _ms("2026-10-10T12:00:01Z"), "backfill prompt two")]
    before = _snapshot(h)
    holder = _start_holder(h)
    problems = []
    try:
        first = holder.stdout.readline().strip()
        if first != "held":
            raise AssertionError(f"the holder did not take the lock (printed {first!r})")
        t0 = time.monotonic()
        rc, out, err = _run(V2, ["register"], h, stdin=payload)
        elapsed = time.monotonic() - t0
        after = _snapshot(h)
    finally:
        holder_rc = _release_holder(holder)
    if rc != 0:
        problems.append(f"register rc={rc}: {err[-200:]}")
    if elapsed > REGISTER_WAIT_LIMIT:
        problems.append(f"register took {elapsed:.2f} s, limit {REGISTER_WAIT_LIMIT} s")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) != 1 or "lock busy" not in lines[0]:
        problems.append(f"register printed {lines!r}, expected one lock-busy line")
    if after != before:
        problems.append("register wrote something while the lock was held")
    if holder_rc != 0:
        problems.append(f"holder exited {holder_rc}")
    returned_in = f"register returned in {elapsed:.2f} s"
    # The next scan backfills the prompts that register skipped.
    clear_throttle(h)
    rc, out, err = v2_scan(h)
    if rc != 0:
        problems.append(f"scan after register rc={rc}: {err[-200:]}")
    keys = _keys(_history(h))
    for k in backfill:
        if keys.count(k) != 1:
            problems.append(f"after the next scan {k[2]!r} appears {keys.count(k)} times in history")
    _fail(problems)
    return returned_in


CHECKS = [
    ("setup: temporary home redirects v1 and v2", check_setup),
    ("extraction equivalence (v1 and v2, same prompts and projects)", check_extraction),
    ("scan equivalence (same keys added, nothing removed)", check_scan_equivalence),
    ("idempotence (a second scan adds nothing)", check_idempotence),
    ("watermark (written, read by v1, honoured, --full)", check_watermark),
    ("lock busy (one line, nothing written)", check_lock_busy),
    ("crash between index write and merge", check_crash_safety),
    ("register matches the base commit", check_register_matches_base),
    ("register with the lock held: skipped in time, backfilled by the scan", check_register_lock_busy),
    ("throttle window kept at 300 s", check_throttle_window),
]


def main() -> int:
    passed = failed = 0
    try:
        for name, fn in CHECKS:
            try:
                note = fn()
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # any other error is a failure too, with its type
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
            else:
                passed += 1
                print(f"PASS {name}" + (f" ({note})" if note else ""))
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print(f"SUMMARY {passed}/{passed + failed} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
