"""
Tests for scripts/guardkit.py: the cross-process lock, binary-exact writes, CleanCache
checkpoints, the transcript walk and the thread pool.

Run from the worktree root:  python -I tests\\test_guardkit.py
Every test writes only under a fresh temporary SESSION_GUARD_HOME.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Set SESSION_GUARD_HOME before guardkit is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="guardkit_test_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"


def scratch(tag):
    return Path(tempfile.mkdtemp(prefix=f"{tag}_", dir=HOME))


# Child process for the mutual-exclusion test. __SCRIPTS__ is replaced before it is written out.
DRIVER = r'''
import os, sys, time
sys.path.insert(0, __SCRIPTS__)
from guardkit import FileLock

log_path, name = sys.argv[1], sys.argv[2]
pid = os.getpid()


def note(line):
    with open(log_path, "a", encoding="ascii") as f:
        f.write(line + "\n")


for _ in range(5):
    lock = FileLock(name, stale=600)
    while not lock.acquire():
        time.sleep(0.005)
    note(f"A {pid} {time.time_ns()}")
    time.sleep(0.02)
    note(f"R {pid} {time.time_ns()}")
    lock.release()
'''


def test_lock_mutual_exclusion():
    work = scratch("mutex")
    driver = work / "driver.py"
    log = work / "log.txt"
    driver.write_text(DRIVER.replace("__SCRIPTS__", repr(str(SCRIPTS))), encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, "-I", str(driver), str(log), "test-mutex"],
                              env=os.environ.copy()) for _ in range(6)]
    codes = [p.wait(timeout=120) for p in procs]
    assert all(c == 0 for c in codes), f"child exit codes {codes}"

    opened = {}
    intervals = []
    for line in log.read_text(encoding="ascii").splitlines():
        kind, pid, t = line.split()
        if kind == "A":
            opened[pid] = int(t)
        else:
            intervals.append((opened.pop(pid), int(t), pid))
    intervals.sort()
    assert len(intervals) == 30, f"{len(intervals)} intervals, expected 30"
    # Sorted by start, any overlap shows up between two neighbours.
    for (s1, e1, p1), (s2, _e2, p2) in zip(intervals, intervals[1:]):
        assert s2 >= e1, f"pid {p2} entered at {s2} before pid {p1} left at {e1}"


def test_stale_lock_is_taken_over():
    locks = gk.STATE_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / "test-stale.lock"
    path.write_text("99998 0\n", encoding="ascii")
    old = time.time() - 2 * 3600
    os.utime(path, (old, old))
    lock = gk.FileLock("test-stale", stale=60)
    assert lock.acquire() is True, "stale lock was not taken over"
    first = path.read_bytes().split()[0]
    assert first == str(os.getpid()).encode(), f"lock now holds {first!r}"
    assert not (locks / "test-stale.lock.takeover").exists(), "takeover marker left behind"
    lock.release()


def test_live_lock_is_not_taken():
    first = gk.FileLock("test-live", stale=600)
    assert first.acquire() is True, "could not take a free lock"
    second = gk.FileLock("test-live", stale=600)
    got = second.acquire()
    first.release()
    assert got is False, "a second lock object took a live lock"


def test_release_does_not_remove_someone_elses_lock():
    lock = gk.FileLock("test-release", stale=600)
    assert lock.acquire() is True, "could not take a free lock"
    lock.path.write_text("99999 0", encoding="ascii")
    lock.release()
    assert lock.path.exists(), "release removed a lock that now belongs to someone else"
    content = lock.path.read_text(encoding="ascii")
    assert content == "99999 0", f"lock content changed to {content!r}"


def test_atomic_write_keeps_old_file_on_failure():
    d = scratch("atomic")
    target = d / "state.bin"
    target.write_bytes(b"old")
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError("injected fsync failure")

    raised = False
    os.fsync = failing_fsync
    try:
        gk.write_bytes_atomic(target, b"new")
    except OSError:
        raised = True
    finally:
        os.fsync = real_fsync
    assert raised, "write_bytes_atomic did not raise"
    assert target.read_bytes() == b"old", f"old file changed to {target.read_bytes()!r}"
    leftovers = [p.name for p in d.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temporary files left behind: {leftovers}"
    gk.write_bytes_atomic(target, b"new")
    assert target.read_bytes() == b"new", "successful write did not replace the file"


def test_read_raw_lines_preserves_bytes():
    d = scratch("rawlines")
    p = d / "f.bin"
    p.write_bytes(b"a\r\nb\n\xff\n")
    got = gk.read_raw_lines(p)
    assert got == [b"a\r", b"b", b"\xff"], f"got {got!r}"
    p.write_bytes(b"x")
    assert gk.read_raw_lines(p) == [b"x"], "file without a trailing newline"
    assert gk.read_raw_lines(d / "missing.bin") == [], "missing file should give []"


def test_transcript_files_shape():
    root = scratch("walk")
    rels = ["proj1/s1.jsonl",
            "proj1/s1/subagents/agent-a.jsonl",
            "proj1/s1/subagents/workflows/wf1/agent-b.jsonl",
            "proj1/acompact-x.jsonl",
            "proj2/s2.jsonl"]
    for rel in rels:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"{}\n")
    top = sorted(t[0] for t in gk.transcript_files(root, top_only=True))
    assert top == ["proj1/s1.jsonl", "proj2/s2.jsonl"], f"top_only gave {top}"
    full = list(gk.transcript_files(root))
    names = sorted(t[0] for t in full)
    assert len(full) == 4, f"full walk gave {names}"
    assert all(len(t) == 5 for t in full), "each entry should have 5 fields"
    assert "proj1/acompact-x.jsonl" not in names, f"acompact file was yielded: {names}"


def test_cleancache_checkpoints():
    cache = gk.CleanCache("test-cache")
    path = cache.path
    for i in range(19):
        cache.mark(f"p/{i}.jsonl", 10, i)
    assert not path.exists(), "saved before the 20th mark"
    cache.mark("p/19.jsonl", 10, 19)
    assert path.exists(), "not saved at the 20th mark"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 20, f"{len(saved)} entries at the 20th mark, expected 20"

    cache.mark("p/20.jsonl", 10, 20)
    real_time = time.time
    time.time = lambda: real_time() + 11
    try:
        cache.mark("p/21.jsonl", 10, 21)          # 11 seconds after the last save: must rewrite
    finally:
        time.time = real_time
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 22, f"{len(saved)} entries after 11 seconds, expected 22"

    cache.mark("p/22.jsonl", 10, 22)
    cache.close()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 23 and all(f"p/{i}.jsonl" in saved for i in range(23)), \
        f"close() did not save every entry ({len(saved)} saved)"


def test_cleancache_round_trip_and_prune():
    name = "test-cache-rt"
    cache = gk.CleanCache(name)
    for i in range(3):
        cache.mark(f"q/{i}.jsonl", 5, i)
    cache.close()
    again = gk.CleanCache(name)
    assert all(again.is_clean(f"q/{i}.jsonl", 5, i) for i in range(3)), \
        "entries did not survive a reload"
    again.prune({"q/0.jsonl"})
    again.close()
    final = gk.CleanCache(name)
    assert sorted(final.data) == ["q/0.jsonl"], \
        f"after prune and close, a reload has {sorted(final.data)}"


def test_run_pool_isolation():
    def work(i):
        if i in (3, 7):
            raise ValueError(f"bad item {i}")
        return i * 2

    out = list(gk.run_pool(work, range(10)))
    assert len(out) == 10, f"{len(out)} results, expected 10"
    errors = [o for o in out if o[2] is not None]
    clean = [o for o in out if o[2] is None]
    assert len(errors) == 2 and len(clean) == 8, f"{len(errors)} errors, {len(clean)} clean"


TESTS = [
    test_lock_mutual_exclusion,
    test_stale_lock_is_taken_over,
    test_live_lock_is_not_taken,
    test_release_does_not_remove_someone_elses_lock,
    test_atomic_write_keeps_old_file_on_failure,
    test_read_raw_lines_preserves_bytes,
    test_transcript_files_shape,
    test_cleancache_checkpoints,
    test_cleancache_round_trip_and_prune,
    test_run_pool_isolation,
]


def main() -> int:
    failed = 0
    try:
        for test in TESTS:
            try:
                test()
            except Exception as exc:              # an assertion or an unexpected error is a FAIL
                failed += 1
                print(f"FAIL {test.__name__}: {str(exc) or type(exc).__name__}")
            else:
                print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print(f"{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
