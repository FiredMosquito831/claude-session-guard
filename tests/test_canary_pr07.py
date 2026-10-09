"""
Tests for the verify canary in scripts/session_archive_v2.py (PR-07): the excused-line index and the
three classes (excused, excused_unreadable, unexplained). The live tool scripts/session_archive.py is read
only, for the comparison test.

Run from the worktree root:  python -I tests\\test_canary_pr07.py
Every test works only under a fresh temporary SESSION_GUARD_HOME. Each test prints PASS or FAIL.
"""
import builtins
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Set SESSION_GUARD_HOME before any plugin code is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="pr07_canary_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import session_archive_v2 as sa  # noqa: E402
import session_archive as live_tool  # noqa: E402  (read only: the old verify, for the comparison)

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"
assert str(sa.CLAUDE_DIR).startswith(HOME), "test would touch real data"

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
U4 = "44444444-4444-4444-8444-444444444444"
RESULTS = []
_counter = [0]


def new_home(label: str) -> Path:
    """A fresh home under HOME. The module globals of both tools are pointed at it."""
    _counter[0] += 1
    home = Path(HOME) / f"{_counter[0]:03d}-{label}"
    assert str(home).startswith(HOME)
    home.mkdir(parents=True)
    for mod in (sa, live_tool):
        mod.CLAUDE_DIR = home
        mod.PROJECTS_DIR = home / "projects"
        mod.ARCHIVE_DIR = home / "session-archive"
        mod.TRANSCRIPTS_DIR = mod.ARCHIVE_DIR / "transcripts"
        mod.USAGE_LEDGER = mod.ARCHIVE_DIR / "usage-ledger.jsonl"
    sa.STATE_DIR = sa.ARCHIVE_DIR / "state"
    sa.QUARANTINE_DIR = sa.ARCHIVE_DIR / "quarantine"
    return home


def tline(uid: str, text: str = "reply") -> str:
    return json.dumps({"type": "assistant", "uuid": uid, "message": {"content": text}})


def live_path(home: Path, rel: str = "proj/s1.jsonl") -> Path:
    return home / "projects" / rel


def arch_path(home: Path, rel: str = "proj/s1.jsonl") -> Path:
    return home / "session-archive" / "transcripts" / rel


def removed_path(home: Path) -> Path:
    return home / "backups" / "sessions" / "removed-lines-archive.jsonl"


def write_lines(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for ln in lines:
            f.write(ln.encode("utf-8") + b"\n")


def removed_record(source: Path, content: str) -> str:
    return json.dumps({"archived_at": "2026-10-09T00:00:00", "source_file": str(source),
                       "source_line": 1, "reason": "test", "content": content})


def write_removed(home: Path, records) -> None:
    """Append raw record lines (strings without a newline) to the removed-lines archive."""
    removed_path(home).parent.mkdir(parents=True, exist_ok=True)
    with open(removed_path(home), "ab") as f:
        for r in records:
            f.write(r.encode("utf-8") + b"\n")


def write_scenario(home: Path) -> None:
    """Live has lines U1 and U2. The archive has U1, U2 and U3. U3 is missing from the live file."""
    write_lines(live_path(home), [tline(U1), tline(U2)])
    write_lines(arch_path(home), [tline(U1), tline(U2), tline(U3)])


def bulk_records(home: Path, n: int, prefix: str = "a") -> bytes:
    """n unrelated, readable removal records, as bytes."""
    lines = [removed_record(live_path(home, "other/s9.jsonl"),
                            tline(f"{prefix}{i:07x}-0000-4000-8000-000000000000", "pad"))
             for i in range(n)]
    return b"".join(ln.encode("utf-8") + b"\n" for ln in lines)


def run_verify():
    """The verify of this module, in process. Returns (exit code, printed output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = sa.cmd_verify()
    return code, buf.getvalue()


def run_verify_process(home: Path):
    """The verify of this module as a separate process, so the real exit code is seen."""
    env = dict(os.environ)
    env["SESSION_GUARD_HOME"] = str(home)
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import session_archive_v2 as s; sys.exit(s.main())"
    p = subprocess.run([sys.executable, "-I", "-c", code, "verify"], capture_output=True,
                       text=True, env=env, timeout=300)
    return p.returncode, p.stdout, p.stderr


def num(out: str, pattern: str) -> int:
    m = re.search(pattern, out)
    assert m, f"no line matching {pattern!r} in output:\n{out}"
    return int(m.group(1))


def num_or_zero(out: str, pattern: str) -> int:
    m = re.search(pattern, out)
    return int(m.group(1)) if m else 0


class Counted:
    """Wraps an open file and records how many bytes each read returned."""

    def __init__(self, f, reads):
        self._f, self._reads = f, reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def read(self, n=-1):
        data = self._f.read(n)
        self._reads.append(len(data))
        return data

    def seek(self, *args):
        return self._f.seek(*args)

    def __getattr__(self, name):
        return getattr(self._f, name)


def test_excused_parseable_record():
    h = new_home("parseable")
    write_scenario(h)
    write_removed(h, [removed_record(live_path(h), tline(U3))])
    code, out = run_verify()
    assert code == 0, f"exit code {code}, expected 0"
    assert num(out, r"missing lines unexplained: (\d+)") == 0, out
    assert num_or_zero(out, r"\((\d+) further missing lines") == 1, out
    assert num(out, r"excused by an unreadable removal record: (\d+)") == 0, out
    assert num(out, r"unreadable removal records found: (\d+)") == 0, out


def test_excused_unreadable_record():
    h = new_home("unreadable")
    write_scenario(h)
    broken = '{"source_file": "x", "content": "' + U3 + ' cut off'     # does not parse; holds U3
    write_removed(h, [broken])
    code, out = run_verify()
    assert code == 0, f"exit code {code}, expected 0"
    assert num(out, r"missing lines unexplained: (\d+)") == 0, out
    assert num(out, r"excused by an unreadable removal record: (\d+)") == 1, out
    assert num(out, r"unreadable removal records found: (\d+)") == 1, out


def test_unexplained_is_reported_and_fails():
    h = new_home("unexplained")
    write_scenario(h)
    write_removed(h, [removed_record(live_path(h), tline(U4))])     # a record, but not for U3
    code, out = run_verify()
    assert code == 1, f"exit code {code}, expected 1"
    assert num(out, r"missing lines unexplained: (\d+)") == 1, out
    assert num(out, r"live files missing archived lines: (\d+)") == 1, out
    assert "    proj\\s1.jsonl: 1 unexplained lines" in out, out
    rc, sout, serr = run_verify_process(h)
    assert rc == 1, f"process exit code {rc}, expected 1; stderr: {serr[-500:]}"
    assert "missing lines unexplained: 1" in sout, sout


def test_exit_zero_when_clean():
    h = new_home("clean")
    write_lines(live_path(h), [tline(U1), tline(U2)])
    write_lines(arch_path(h), [tline(U1), tline(U2)])
    write_removed(h, [removed_record(live_path(h), tline(U4))])
    code, out = run_verify()
    assert code == 0, f"in process: exit code {code}, expected 0"
    rc, sout, serr = run_verify_process(h)
    assert rc == 0, f"process exit code {rc}, expected 0; stderr: {serr[-500:]}"
    assert "live files missing archived lines: 0" in sout, sout


def test_index_is_incremental():
    h = new_home("incremental")
    write_scenario(h)
    write_removed(h, [removed_record(live_path(h), tline(U3))])
    before = sa.refresh_excused_index()
    assert len(before["uuid"]) == 1, f"uuid set has {len(before['uuid'])} entries, expected 1"
    rec_file = removed_path(h)
    extra = bulk_records(h, 1000)
    with open(rec_file, "ab") as f:
        f.write(extra)
    reads = []
    real_open = builtins.open
    target = str(rec_file)

    def counting_open(file, *args, **kwargs):
        f = real_open(file, *args, **kwargs)
        return Counted(f, reads) if str(file) == target else f

    sa.open = counting_open          # shadows builtins.open for this module only
    try:
        after = sa.refresh_excused_index()
    finally:
        del sa.open
    total = sum(reads)
    assert len(extra) <= total <= len(extra) + 4096, \
        f"read {total} bytes from the removed-lines archive; new records are {len(extra)} bytes"
    assert len(after["uuid"]) == 1001, f"uuid set has {len(after['uuid'])} entries, expected 1001"
    assert len(after["record"]) == 1001, f"record set has {len(after['record'])} entries, expected 1001"


def test_index_rebuilds_when_archive_shrinks():
    h = new_home("shrink")
    write_scenario(h)
    write_removed(h, [removed_record(live_path(h), tline(U3))])
    code0, out0 = run_verify()
    size0 = removed_path(h).stat().st_size
    with open(removed_path(h), "ab") as f:
        f.write(bulk_records(h, 1000))
    code1, out1 = run_verify()
    assert (code1, out1) == (code0, out0), "adding unrelated records changed the result"
    with open(removed_path(h), "r+b") as f:
        f.truncate(size0)
    code2, out2 = run_verify()
    assert code2 == code0 and out2 == out0, f"after truncation:\n{out2}\nbefore:\n{out0}"
    idx = sa.refresh_excused_index()
    assert len(idx["record"]) == 1 and len(idx["uuid"]) == 1, \
        f"rebuilt index holds {len(idx['record'])} records, expected 1"
    state = json.loads((sa.ARCHIVE_DIR / "state" / "excused.json").read_text(encoding="utf-8"))
    assert state["archive_offset"] == size0, f"index offset {state['archive_offset']}, expected {size0}"


def test_verify_writes_nothing_to_the_archive():
    h = new_home("nowrite")
    write_scenario(h)
    write_removed(h, [removed_record(live_path(h), tline(U3))])

    def snapshot(root: Path) -> dict:
        files = {}
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in ("state/excused.json", "state/excused.bin"):     # the index files: the only writes allowed
                continue
            st = p.stat()
            files[rel] = (st.st_size, st.st_mtime_ns)
        return files

    before = snapshot(sa.ARCHIVE_DIR)
    removed_before = (removed_path(h).stat().st_size, removed_path(h).stat().st_mtime_ns)
    code, out = run_verify()
    assert code == 0, f"exit code {code}, expected 0"
    after = snapshot(sa.ARCHIVE_DIR)
    assert before == after, f"archive files changed: {set(before.items()) ^ set(after.items())}"
    assert (removed_path(h).stat().st_size, removed_path(h).stat().st_mtime_ns) == removed_before, \
        "the removed-lines archive changed"
    assert (sa.ARCHIVE_DIR / "state" / "excused.json").exists(), "the index was not written"


def test_matches_old_verify_on_a_clean_corpus():
    h = new_home("clean-corpus")
    write_lines(live_path(h, "proj/a.jsonl"), [tline(U1), tline(U2)])
    write_lines(arch_path(h, "proj/a.jsonl"), [tline(U1), tline(U2)])
    write_lines(live_path(h, "proj/b.jsonl"), [tline(U1, "x")])
    write_lines(arch_path(h, "proj/b.jsonl"), [tline(U1, "x"), tline(U3)])
    write_lines(arch_path(h, "proj/only.jsonl"), [tline(U4)])
    write_removed(h, [removed_record(live_path(h, "proj/b.jsonl"), tline(U3))])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        old_code = live_tool.cmd_verify()
    old = buf.getvalue().splitlines()
    new_code, new_text = run_verify()
    new_lines = [ln for ln in new_text.splitlines()
                 if not ln.startswith(("[session-archive] missing lines excused by an unreadable",
                                       "[session-archive] missing lines unexplained",
                                       "[session-archive] unreadable removal records found"))]
    assert old_code == 0 and new_code == 0, f"exit codes old={old_code} new={new_code}"
    assert new_lines == old, f"old:\n" + "\n".join(old) + "\nnew:\n" + "\n".join(new_lines)
    canary = [ln for ln in old if ln.startswith("[session-archive] live files missing archived lines")]
    assert canary and canary[0].split(":")[1].split()[0] == "0", f"canary line: {canary}"


def test_excused_raw_line_needs_matching_source():
    h = new_home("raw-match")
    write_lines(live_path(h), [tline(U1)])
    write_lines(arch_path(h), [tline(U1), "   padding line"])     # a raw line with no uuid
    write_removed(h, [removed_record(live_path(h), "   padding line")])
    code, out = run_verify()
    assert code == 0, f"matching source: exit code {code}, expected 0"
    assert num(out, r"missing lines unexplained: (\d+)") == 0, out
    assert num_or_zero(out, r"\((\d+) further missing lines") == 1, out
    h2 = new_home("raw-other-source")
    write_lines(live_path(h2), [tline(U1)])
    write_lines(arch_path(h2), [tline(U1), "   padding line"])
    write_removed(h2, [removed_record(live_path(h2, "proj/other.jsonl"), "   padding line")])
    code2, out2 = run_verify()
    assert code2 == 1, f"other source: exit code {code2}, expected 1"
    assert num(out2, r"missing lines unexplained: (\d+)") == 1, out2


TESTS = [
    test_excused_parseable_record,
    test_excused_unreadable_record,
    test_unexplained_is_reported_and_fails,
    test_exit_zero_when_clean,
    test_index_is_incremental,
    test_index_rebuilds_when_archive_shrinks,
    test_verify_writes_nothing_to_the_archive,
    test_matches_old_verify_on_a_clean_corpus,
    test_excused_raw_line_needs_matching_source,
]


def run_one(fn) -> bool:
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL {fn.__name__}: {exc}")
        return False
    except Exception as exc:                       # any other error is a failure too
        print(f"FAIL {fn.__name__}: unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"PASS {fn.__name__}")
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        results = [run_one(t) for t in TESTS]
    finally:
        shutil.rmtree(HOME, ignore_errors=True)    # HOME was created by this test
    print(f"{sum(results)} of {len(results)} passed")
    sys.exit(0 if all(results) else 1)
