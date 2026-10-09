"""
Tests for scripts/session_archive_v2.py (PR-05): append-only archive with per-transcript offsets.

Run from the worktree root:  python -I tests\\test_archive_pr05.py
Every test writes only under a fresh temporary SESSION_GUARD_HOME. Each test prints PASS or FAIL.
"""
import builtins
import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

# Set SESSION_GUARD_HOME before any plugin code is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="pr05_test_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import session_archive_v2 as sa  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"
assert str(sa.CLAUDE_DIR).startswith(HOME), "test would touch real data"
SLUG = "C--Users-fgghk-pr05-test"


def reset():
    """Empty the projects and archive folders. Both are under HOME, which this test created."""
    for d in (sa.PROJECTS_DIR, sa.ARCHIVE_DIR):
        assert str(d).startswith(HOME), f"refusing to delete {d}"
        shutil.rmtree(d, ignore_errors=True)
    sa.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def paths(sess="sess-1"):
    live = sa.PROJECTS_DIR / SLUG / f"{sess}.jsonl"
    rel = live.relative_to(sa.PROJECTS_DIR).as_posix()
    return live, rel, sa.TRANSCRIPTS_DIR / rel, sa.STATE_DIR / f"{rel}.json"


def make_line(rng, with_uuid=None):
    """One JSON transcript line as bytes, newline included. Some lines have no uuid (raw identity)."""
    if with_uuid is None:
        with_uuid = rng.random() < 0.8
    text = "".join(rng.choice("abcdef 0123 é") for _ in range(rng.randint(0, 300)))
    obj = {"type": "assistant", "timestamp": "2026-10-09T00:00:00Z",
           "message": {"model": "claude-test",
                       "usage": {"input_tokens": rng.randint(0, 9999), "output_tokens": rng.randint(0, 999)},
                       "content": [{"type": "text", "text": text}]}}
    if with_uuid:
        obj["uuid"] = f"{rng.getrandbits(128):032x}"
    return json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def chunk(rng, n):
    return b"".join(make_line(rng) for _ in range(n))


def complete_lines(data: bytes):
    return data[: data.rfind(b"\n") + 1].split(b"\n")[:-1]


class _Counting:
    """Proxy for a binary file that counts the bytes returned by read()."""

    def __init__(self, f, counts, key):
        self._f, self._counts, self._key = f, counts, key

    def read(self, *args):
        data = self._f.read(*args)
        self._counts[self._key] += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._f.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._f, name)


def sync_counting(live, rel):
    """sync_transcript with builtins.open wrapped, so bytes read from the live file and the archive are counted."""
    arch = sa.TRANSCRIPTS_DIR / rel
    watch = {os.path.normcase(os.path.abspath(str(live))): "live",
             os.path.normcase(os.path.abspath(str(arch))): "archive"}
    counts = {"live": 0, "archive": 0}
    real_open = builtins.open

    def counting_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        key = None
        if isinstance(file, (str, os.PathLike)):
            key = watch.get(os.path.normcase(os.path.abspath(os.fspath(file))))
        if key and "r" in mode:
            return _Counting(f, counts, key)
        return f

    builtins.open = counting_open
    try:
        result = sa.sync_transcript(live, rel)
    finally:
        builtins.open = real_open
    return result, counts


def count_full_merges(fn):
    """Run fn() and return (its result, how many times the full merge ran)."""
    calls = [0]
    real = sa._full_merge

    def wrapped(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    sa._full_merge = wrapped
    try:
        out = fn()
    finally:
        sa._full_merge = real
    return out, calls[0]


def test_fast_path_equals_full_merge():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(20261009)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"")
    merges = 0
    for _ in range(5):                                # the live file grows in 5 steps
        with open(live, "ab") as f:
            f.write(chunk(rng, 40))
        _, n = count_full_merges(lambda: sa.sync_transcript(live, rel))
        merges += n
    assert merges == 1, f"expected only the first sync to do a full merge, got {merges}"
    fast = arch.read_bytes()
    final = live.read_bytes()
    assert fast == final, "fast-path archive differs from the live file"
    # Forced full merge from scratch: delete the state and the archive, then sync.
    state.unlink()
    arch.unlink()
    _, n2 = count_full_merges(lambda: sa.sync_transcript(live, rel))
    assert n2 == 1, "the forced sync did not take the full-merge path"
    full = arch.read_bytes()
    assert len(fast) == len(full), f"byte totals differ: {len(fast)} vs {len(full)}"
    assert sorted(fast.split(b"\n")) == sorted(full.split(b"\n")), "line sets differ"
    assert fast == full, "archives are not byte-identical"
    print(f"INFO fast_path: live {len(final)} bytes, {len(complete_lines(final))} lines; "
          f"fast-path archive {len(fast)} bytes, {len(complete_lines(fast))} lines; "
          f"full merge from scratch {len(full)} bytes, {len(complete_lines(full))} lines")


def test_no_line_lost_after_rewrite():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(7)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(chunk(rng, 30))
    sa.sync_transcript(live, rel)
    old_lines = complete_lines(live.read_bytes())
    kept = old_lines[:10]
    new_lines = [make_line(rng, True).rstrip(b"\n") for _ in range(5)]
    replacement = b"".join(ln + b"\n" for ln in kept + new_lines)
    assert len(replacement) < len(live.read_bytes()), "test setup: the replacement must be shorter"
    live.write_bytes(replacement)
    status, _, _ = sa.sync_transcript(live, rel)
    assert status == "ok", status
    arch_lines = set(complete_lines(arch.read_bytes()))
    lost = [ln for ln in old_lines if ln not in arch_lines]
    absent = [ln for ln in new_lines if ln not in arch_lines]
    assert not lost, f"{len(lost)} old lines were lost"
    assert not absent, f"{len(absent)} new lines are missing from the archive"


def test_archive_is_never_rewritten():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(3)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(chunk(rng, 10))
    sa.sync_transcript(live, rel)
    before = os.stat(arch)
    head_before = arch.read_bytes()[:1024]
    assert before.st_size >= 1024, "test setup: archive must be at least 1 KB"
    with open(live, "ab") as f:
        f.write(chunk(rng, 5))
    sa.sync_transcript(live, rel)
    after = os.stat(arch)
    assert after.st_size > before.st_size, "the sync did not append"
    assert after.st_ino == before.st_ino, f"inode changed: {before.st_ino} -> {after.st_ino}"
    assert arch.read_bytes()[:1024] == head_before, "the first 1 KB of the archive changed"
    print(f"INFO never_rewritten: inode {before.st_ino}, size {before.st_size} -> {after.st_size}")


def test_crash_between_append_and_state():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(11)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(chunk(rng, 20))
    sa.sync_transcript(live, rel)
    recorded = arch.stat().st_size
    new_lines = b"".join(make_line(rng, True) for _ in range(3))
    garbage = os.urandom(10 * 1024)
    with open(live, "ab") as f:
        f.write(new_lines)                            # the live file gets 3 new lines
    with open(arch, "ab") as f:                       # crash: the append landed, the state did not
        f.write(new_lines + garbage)
    status, _, _ = sa.sync_transcript(live, rel)
    assert status == "ok", status
    assert arch.read_bytes() == live.read_bytes(), (
        f"archive ({arch.stat().st_size} bytes) does not equal the live file ({live.stat().st_size} bytes)")
    keys = [sa.line_key(ln) for ln in complete_lines(arch.read_bytes())]
    assert len(keys) == len(set(keys)), "a line is duplicated in the archive"
    assert len(keys) == len(complete_lines(live.read_bytes())), "a line is missing from the archive"
    qdir = sa.QUARANTINE_DIR / Path(rel).parent
    found = [q for q in qdir.iterdir() if q.name.startswith(Path(rel).name)] if qdir.exists() else []
    assert len(found) == 1, f"expected one quarantine file, found {len(found)}"
    assert found[0].read_bytes() == new_lines + garbage, "the quarantine file does not hold the removed bytes"
    print(f"INFO crash: recorded archive size {recorded}, removed tail {len(new_lines) + len(garbage)} bytes, "
          f"quarantined as {found[0].name}")


def test_binary_and_complete_lines():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(5)
    live.parent.mkdir(parents=True, exist_ok=True)
    crlf_line = make_line(rng, True)[:-1] + b"\r\n"
    plain = make_line(rng, True)
    partial = b'{"type":"assistant","uuid":"partial-1-0001"'
    completion = b',"message":{"usage":{"input_tokens":1}}}\n'
    live.write_bytes(crlf_line + plain + partial)     # the last line has no newline yet
    sa.sync_transcript(live, rel)
    a = arch.read_bytes()
    assert a.count(crlf_line) == 1, "the CRLF line was not archived unchanged, once"
    assert partial not in a, "a partial line reached the archive before it was complete"
    with open(live, "ab") as f:
        f.write(completion)
    status, _, _ = sa.sync_transcript(live, rel)
    assert status == "ok", status
    a = arch.read_bytes()
    completed = partial + completion
    assert a.count(completed) == 1, f"the completed line appears {a.count(completed)} times"
    assert a.count(b"partial-1-0001") == 1, "the completed line is not archived exactly once"
    assert a.count(crlf_line) == 1, "the CRLF line changed on the second sync"


def test_counting_fast_path_reads():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(9)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(chunk(rng, 30))
    sa.sync_transcript(live, rel)
    new = chunk(rng, 4)
    with open(live, "ab") as f:
        f.write(new)
    result, merges = count_full_merges(lambda: sync_counting(live, rel))
    (status, lines, nbytes), counts = result
    assert merges == 0, "the fast path was not taken"
    assert status == "ok" and lines == 4 and nbytes == len(new), (status, lines, nbytes)
    assert counts["archive"] == 0, f"read {counts['archive']} bytes from the archive"
    assert counts["live"] <= len(new) + 8192, f"read {counts['live']} live bytes for {len(new)} new bytes"
    print(f"INFO counting: new {len(new)} bytes; live bytes read {counts['live']} "
          f"(limit {len(new) + 8192}); archive bytes read {counts['archive']}")


def test_status_rename():
    reset()
    sa.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    sa.USAGE_LEDGER.write_text(json.dumps({"sessionId": "s", "uuid": "u",
                                           "usage": {"input_tokens": 5, "output_tokens": 7}}) + "\n",
                               encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sa.cmd_status()
    out = buf.getvalue()
    assert rc == 0, rc
    assert "usage_ledger_raw_per_line_tokens" in out, "renamed field is missing"
    assert "usage_ledger_tokens" not in out, "the old field name is still printed"
    assert "not a usage total" in out, "the note that this is not a usage total is missing"
    assert json.loads(out)["usage_ledger_raw_per_line_tokens"] == 12


def test_sync_transcript_mode():
    reset()
    rng = random.Random(13)
    a_live, a_rel, a_arch, _ = paths("sess-A")
    b_live, b_rel, b_arch, b_state = paths("sess-B")
    sub = a_live.with_suffix("") / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    sub_live = sub / "agent-1.jsonl"
    a_live.write_bytes(chunk(rng, 5))
    b_live.write_bytes(chunk(rng, 5))
    sub_live.write_bytes(chunk(rng, 3))
    with contextlib.redirect_stdout(io.StringIO()):
        sa.cmd_sync_all()                             # archive everything once
    b_size = b_arch.stat().st_size
    b_state_before = b_state.read_bytes()
    sub_arch = sa.TRANSCRIPTS_DIR / sub_live.relative_to(sa.PROJECTS_DIR).as_posix()
    sub_size = sub_arch.stat().st_size
    with open(a_live, "ab") as f:
        f.write(chunk(rng, 2))
    with open(b_live, "ab") as f:
        f.write(chunk(rng, 2))
    with open(sub_live, "ab") as f:
        f.write(chunk(rng, 1))
    a_before = a_arch.stat().st_size
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sa.cmd_sync_transcript(str(a_live))
    out = buf.getvalue().strip()
    assert rc == 0, out
    assert a_arch.stat().st_size > a_before, "session A was not synced"
    assert sub_arch.stat().st_size > sub_size, "session A's subagent transcript was not synced"
    assert b_arch.stat().st_size == b_size, "session B's archive changed"
    assert b_state.read_bytes() == b_state_before, "session B's state changed"
    assert out.startswith("[session-archive] scanned 2 live transcripts;"), out
    assert "archived 2 (3 new lines)" in out, out
    assert out.endswith("+0 usage rows"), out


def test_lock_busy_skips():
    reset()
    live, rel, arch, state = paths()
    rng = random.Random(17)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(chunk(rng, 3))
    sa.sync_transcript(live, rel)
    before = arch.read_bytes()
    with open(live, "ab") as f:
        f.write(chunk(rng, 2))
    holder = gk.FileLock("session-archive-" + rel.replace("/", "__"))
    assert holder.acquire(), "test setup: could not take the lock"
    old_timeout = sa.LOCK_TIMEOUT
    sa.LOCK_TIMEOUT = 0.5
    try:
        status, _, _ = sa.sync_transcript(live, rel)
    finally:
        sa.LOCK_TIMEOUT = old_timeout
        holder.release()
    assert status == "skipped", status
    assert arch.read_bytes() == before, "the archive changed while another process held the lock"


TESTS = [test_fast_path_equals_full_merge, test_no_line_lost_after_rewrite, test_archive_is_never_rewritten,
         test_crash_between_append_and_state, test_binary_and_complete_lines, test_counting_fast_path_reads,
         test_status_rename, test_sync_transcript_mode, test_lock_busy_skips]


def main():
    failed = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print(f"[pr05] {len(TESTS) - failed} of {len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
