"""PR-08 tests for scripts/session_indexer_v2.py. Run from WT: python -I tests\test_indexer_pr08.py

Every test runs against a fresh temporary SESSION_GUARD_HOME. Nothing outside it is read or written.
"""
import os, tempfile
_HOME = tempfile.mkdtemp(prefix="sg_pr08_home_")
os.environ["SESSION_GUARD_HOME"] = _HOME

import hashlib, importlib.util, inspect, json, shutil, sys, threading, time
from collections import Counter
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk
import session_indexer_v2 as v2
assert str(gk.CLAUDE_DIR).startswith(_HOME), "test would touch real data"
assert str(v2.OFFICIAL_HISTORY).startswith(_HOME) and str(v2.STATE_DIR).startswith(_HOME)


class Fail(Exception):
    pass


def expect(cond, reason):
    if not cond:
        raise Fail(reason)


def reset_home():
    """Empty history, parallel index and state under the temporary home only."""
    assert str(v2.OFFICIAL_HISTORY).startswith(_HOME) and str(v2.STATE_DIR).startswith(_HOME)
    v2.OFFICIAL_HISTORY.unlink(missing_ok=True)
    v2.PARALLEL_INDEX.unlink(missing_ok=True)
    shutil.rmtree(v2.STATE_DIR, ignore_errors=True)
    v2.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)


def prompt(sid, n, text=None):
    return {"display": text if text is not None else f"prompt {sid} #{n}",
            "pastedContents": {}, "timestamp": 1_700_000_000_000 + n,
            "project": "C:\\\\work", "sessionId": sid}


def write_parallel(entries):
    with open(v2.PARALLEL_INDEX, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def history_entries():
    out = []
    for raw in v2.OFFICIAL_HISTORY.read_bytes().split(b"\n"):
        if raw.strip():
            out.append(json.loads(raw.decode("utf-8", "surrogateescape")))
    return out


def merge(min_interval=0):
    return v2.merge_history(min_interval_seconds=min_interval)


def test_append_only_no_loss_with_external_writer():
    reset_home()
    with open(v2.OFFICIAL_HISTORY, "w", encoding="utf-8") as f:   # pre-existing: 50 lines
        for i in range(50):
            f.write(json.dumps(prompt("ext-pre", i)) + "\n")
    merged = [prompt(f"idx-{i % 7}", i) for i in range(200)]
    write_parallel(merged)
    written, errors, statuses, added_total = [], [], [], 0
    snapshots = []   # (offset, sha256 of the bytes before that offset) after each merge

    def writer():
        try:
            for i in range(500):
                e = prompt("ext-live", i, text=f"external {i}")
                with open(v2.OFFICIAL_HISTORY, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                written.append(e)
                time.sleep(0.002)
        except Exception as ex:
            errors.append(repr(ex))

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(20):
        r = merge(0)
        statuses.append(r.status)
        added_total += r.added
        st = gk.load_json(v2.STATE_FILE, None)
        expect(st is not None, "no state after a merge")
        with open(v2.OFFICIAL_HISTORY, "rb") as f:
            snapshots.append((st["history_offset"], hashlib.sha256(f.read(st["history_offset"])).hexdigest()))
        time.sleep(0.005)
    t.join()
    expect(not errors, f"writer thread failed: {errors}")
    final = merge(0)
    statuses.append(final.status)
    added_total += final.added
    expect(set(statuses) <= {"ok", "deferred"}, f"unexpected statuses {sorted(set(statuses))}")
    expect(final.status == "ok", f"final merge {final.status}: {final.detail}")
    expect(added_total == 200, f"merges added {added_total}, expected 200")

    data = v2.OFFICIAL_HISTORY.read_bytes()
    for off, sha in snapshots:
        expect(hashlib.sha256(data[:off]).hexdigest() == sha, f"bytes before offset {off} changed")
    counts = Counter(v2.entry_key(e) for e in history_entries())
    expect(sum(counts.values()) == 50 + 500 + 200, f"history has {sum(counts.values())} lines, expected 750")
    for e in written:
        expect(counts[v2.entry_key(e)] == 1, f"external line lost or duplicated: {e['display']}")
    for e in merged:
        expect(counts[v2.entry_key(e)] == 1, f"merged entry not exactly once: {e['display']}")


def test_no_duplicates_on_repeat():
    reset_home()
    write_parallel([prompt("s1", i) for i in range(30)])
    r1 = merge(0)
    r2 = merge(0)
    expect(r1.status == "ok" and r1.added == 30, f"first merge {r1.status} added {r1.added}")
    expect(r2.status == "ok" and r2.added == 0, f"second merge {r2.status} added {r2.added}")
    keys = [v2.entry_key(e) for e in history_entries()]
    expect(len(keys) == 30 and len(set(keys)) == 30, f"{len(keys)} lines, {len(set(keys))} distinct")
    expect(b"\r" not in v2.OFFICIAL_HISTORY.read_bytes(), "merged lines were written with CRLF")
    expect(v2.KEYS_FILE.stat().st_size == 16 * 30, f"keys file is {v2.KEYS_FILE.stat().st_size} bytes, expected 480")


def test_refuses_when_history_rewritten():
    reset_home()
    write_parallel([prompt("s1", i) for i in range(20)])
    expect(merge(0).status == "ok", "first merge did not succeed")
    write_parallel([prompt("s2", i) for i in range(5)])
    with open(v2.OFFICIAL_HISTORY, "r+b") as f:
        f.seek(0)
        f.write(b"#" * 100)                       # rewrite the first 100 bytes by hand
    before = v2.OFFICIAL_HISTORY.read_bytes()
    r = merge(0)
    expect(r.status == "refused", f"expected refused, got {r.status}: {r.detail}")
    expect(r.added == 0, f"refused merge reports added {r.added}")
    expect(v2.OFFICIAL_HISTORY.read_bytes() == before, "refused merge changed history.jsonl")


def test_state_rebuild():
    reset_home()
    write_parallel([prompt("s1", i) for i in range(40)])
    expect(merge(0).added == 40, "setup merge did not add 40")
    with open(v2.OFFICIAL_HISTORY, "a", encoding="utf-8") as f:   # 10 more lines, not from the index
        for i in range(10):
            f.write(json.dumps(prompt("ext", i)) + "\n")
    v2.STATE_FILE.unlink()
    v2.KEYS_FILE.unlink()
    r = merge(0)
    expect(r.status == "ok" and r.added == 0, f"rebuild merge {r.status} added {r.added}")
    lines = [v2.entry_key(e) for e in history_entries()]
    expect(len(set(lines)) == len(lines) == 50, f"{len(lines)} lines, {len(set(lines))} distinct, expected 50")
    keys_file_size = v2.KEYS_FILE.stat().st_size
    expect(keys_file_size == 16 * len(lines), f"keys file holds {keys_file_size // 16} keys, file has {len(lines)} lines")


def test_register_uses_payload():
    reset_home()
    proj = v2.PROJECTS_DIR / "C--work"
    proj.mkdir(parents=True, exist_ok=True)
    transcript = proj / "sess-abc.jsonl"
    ts = "2026-10-09T10:00:00Z"
    rows = [
        {"type": "user", "cwd": "C:\\work", "timestamp": ts, "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]}},
        {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "user", "isMeta": True, "timestamp": ts, "message": {"role": "user", "content": "meta text"}},
        {"type": "user", "timestamp": "2026-10-09T10:05:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "second prompt with blocks"}]}},
        {"type": "user", "timestamp": ts, "message": {"role": "user", "content": "ok"}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    # The v2 extraction must match the live scan's extraction on the same file.
    spec = importlib.util.spec_from_file_location("session_indexer_v1", SCRIPTS / "session_indexer.py")
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    expect(v2.extract_prompts(transcript, "C--work") == v1.extract_prompts(transcript, "C--work"),
           "extract_prompts differs from session_indexer.py")

    original = gk.read_hook_payload
    try:
        gk.read_hook_payload = lambda: {"session_id": "sess-abc", "transcript_path": str(transcript), "cwd": "C:\\work"}
        rc = v2.main(["register"])
        expect(rc == 0, f"register with payload returned {rc}")
        idx = [json.loads(l) for l in v2.PARALLEL_INDEX.read_text(encoding="utf-8").splitlines() if l.strip()]
        displays = sorted(e["display"] for e in idx if e["sessionId"] == "sess-abc")
        expect(displays == ["first prompt", "second prompt with blocks"], f"parallel index has {displays}")
        hist = [e["display"] for e in history_entries() if e["sessionId"] == "sess-abc"]
        expect(sorted(hist) == displays, f"history has {hist}")

        before = v2.PARALLEL_INDEX.read_bytes()
        gk.read_hook_payload = lambda: {}
        rc = v2.main(["register"])
        expect(rc == 0, f"register without payload returned {rc}")
        expect(v2.PARALLEL_INDEX.read_bytes() == before, "register without payload changed the index")
    finally:
        gk.read_hook_payload = original


def test_lock_busy_returns_busy():
    reset_home()
    write_parallel([prompt("s1", i) for i in range(5)])
    expect(inspect.signature(v2.merge_history).parameters["lock_timeout"].default == 30.0,
           "lock_timeout default is not 30 s")
    lock = gk.lock_wait(v2.LOCK_NAME, timeout=1)
    expect(lock is not None, "test could not take the lock")
    try:
        r = v2.merge_history(min_interval_seconds=0, lock_timeout=1)
    finally:
        lock.release()
    expect(r.status == "busy" and r.added == 0, f"expected busy, got {r.status} added {r.added}")
    expect(not v2.OFFICIAL_HISTORY.exists(), "busy merge wrote history.jsonl")
    r2 = merge(0)
    expect(r2.status == "ok" and r2.added == 5, f"merge after release: {r2.status} added {r2.added}")


def test_throttle():
    reset_home()
    expect(v2.DEFAULT_MIN_INTERVAL == 300, "default interval is not 300 s")
    write_parallel([prompt("s1", i) for i in range(3)])
    r1 = v2.merge_history()
    expect(r1.status == "ok" and r1.added == 3, f"first merge {r1.status} added {r1.added}")
    write_parallel([prompt("s2", i) for i in range(2)])
    r2 = v2.merge_history()
    expect(r2.status == "throttled" and r2.added == 0, f"second merge within 300 s: {r2.status}")
    expect(len(history_entries()) == 3, "throttled merge wrote history.jsonl")
    r3 = merge(0)
    expect(r3.status == "ok" and r3.added == 2, f"unthrottled merge {r3.status} added {r3.added}")


TESTS = [
    test_append_only_no_loss_with_external_writer,
    test_no_duplicates_on_repeat,
    test_refuses_when_history_rewritten,
    test_state_rebuild,
    test_register_uses_payload,
    test_lock_busy_returns_busy,
    test_throttle,
]

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Fail as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"SUMMARY {passed}/{len(TESTS)} passed")
    shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(0 if passed == len(TESTS) else 1)
