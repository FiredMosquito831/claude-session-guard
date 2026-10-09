"""
Tests for guardkit.append_removed_records (PR-09, Part A): the one locked, binary writer for the
removed-lines archive.

Run from the worktree root:  python -I tests\\test_removed_archive_pr09.py
Every test writes only under a fresh temporary SESSION_GUARD_HOME. Each check prints PASS or FAIL.
The same file is also the writer child: `--writer <tag>` appends 200 large records and exits.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="pr09_archive_home_")
os.environ["SESSION_GUARD_HOME"] = HOME               # set before guardkit is imported
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"

RESULTS = []


def check(name, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))


def fresh(tag):
    """A new archive and a new index folder under HOME. Returns the archive path."""
    root = Path(tempfile.mkdtemp(prefix=f"{tag}_", dir=HOME))
    gk.STATE_DIR = root / "state"
    archive = root / "backups" / "removed-lines-archive.jsonl"
    assert str(archive).startswith(HOME)
    return archive


def big_record(tag, k):
    body = (f"{tag}-{k}-" + "x" * 20000 + "   \"q\" \\ end")
    return {"source_file": f"/w/{tag}.jsonl", "source_line": k, "reason": "test", "content": body}


def read_records(archive):
    """Every line as bytes, parsed the way session_archive reads it (surrogateescape decoding)."""
    data = archive.read_bytes()
    parsed, bad = [], 0
    for ln in data.split(b"\n"):
        if not ln.strip():
            continue
        try:
            parsed.append(json.loads(ln.decode("utf-8", "surrogateescape")))
        except ValueError:
            bad += 1
    return data, parsed, bad


def writer_child(tag, archive, state):
    gk.STATE_DIR = Path(state)                          # the same archive and index as the parent
    for k in range(200):
        gk.append_removed_records([big_record(tag, k)], archive=Path(archive))
    sys.exit(0)


def t_two_processes_append():
    archive = fresh("two")
    env = os.environ.copy()                             # same SESSION_GUARD_HOME
    procs = [subprocess.Popen([sys.executable, "-I", __file__, "--writer", tag, str(archive), str(gk.STATE_DIR)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for tag in ("alpha", "beta")]
    outs = [p.communicate(timeout=600) for p in procs]
    rcs = [p.returncode for p in procs]
    check("two writers exit 0", all(rc == 0 for rc in rcs), f"rc={rcs} stderr={[o[1][-200:] for o in outs]}")
    data, recs, bad = read_records(archive)
    check("every archived line parses as one JSON record (no merged lines)", bad == 0 and len(recs) == 400,
          f"parsed={len(recs)} unparseable={bad}")
    check("the total record count is 400", len(recs) == 400, f"count={len(recs)}")
    keys = {(r["source_file"], r["source_line"]) for r in recs}
    check("no record is lost or duplicated (400 distinct source records)", len(keys) == 400, f"distinct={len(keys)}")
    check("every content is the full 20 KB body", all(len(r["content"]) > 20000 for r in recs))
    check("the archive has no CR bytes (binary writes, no CRLF)", b"\r" not in data)
    idx_bin = gk.STATE_DIR / "removed_index.bin"
    check("the index holds one 16-byte digest per record",
          idx_bin.exists() and idx_bin.stat().st_size == 16 * 400,
          f"bytes={idx_bin.stat().st_size if idx_bin.exists() else 'missing'}")


def t_repeat_writes_nothing():
    archive = fresh("repeat")
    recs = [big_record("rep", k) for k in range(5)]
    w1, s1 = gk.append_removed_records(recs, archive=archive)
    size1 = archive.stat().st_size
    w2, s2 = gk.append_removed_records(recs, archive=archive)
    check("a first call writes all 5 records", (w1, s1) == (5, 0), f"written={w1} skipped={s1}")
    check("a repeat call with the same records writes 0 and skips all 5", (w2, s2) == (0, 5),
          f"written={w2} skipped={s2}")
    check("the repeat call leaves the archive byte-identical", archive.stat().st_size == size1)


def t_xff_round_trip():
    archive = fresh("xff")
    raw = b"caf\xe9 \xff tail \x00 end"
    content = raw.decode("utf-8", "surrogateescape")
    w, s = gk.append_removed_records([{"source_file": "/w/x.jsonl", "source_line": 7,
                                       "reason": "bytes", "content": content}], archive=archive)
    data, recs, bad = read_records(archive)
    back = recs[0]["content"].encode("utf-8", "surrogateescape") if recs else None
    check("a record with a \\xff byte is written", (w, s) == (1, 0) and bad == 0, f"written={w} bad={bad}")
    check("the \\xff byte round-trips exactly", back == raw, f"back={back!r}")
    check("the raw archive line holds the \\xff byte itself", b"\xff" in data)


TESTS = [t_two_processes_append, t_repeat_writes_nothing, t_xff_round_trip]

if __name__ == "__main__":
    if len(sys.argv) > 4 and sys.argv[1] == "--writer":
        writer_child(sys.argv[2], sys.argv[3], sys.argv[4])
    for fn in TESTS:
        try:
            fn()
        except Exception as exc:                        # one broken test must not hide the others
            check(fn.__name__ + " ran without an exception", False, f"{type(exc).__name__}: {exc}")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if RESULTS and all(RESULTS) else 1)
