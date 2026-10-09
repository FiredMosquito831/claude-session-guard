"""
Tests for scripts/jsonl_repair_v2.py (PR-09, Part B): the byte-exact, archive-first, incremental sweep.

Run from the worktree root:  python -I tests\\test_jsonl_pr09.py
Every test writes only under a fresh temporary SESSION_GUARD_HOME. Each check prints PASS or FAIL.
Printed differences use ascii(), because the console code page is cp1252.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Set SESSION_GUARD_HOME before any plugin module is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="pr09_jsonl_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
os.environ.pop("CLAUDE_SESSION_ID", None)
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import api_repair_v2 as api  # noqa: E402
import jsonl_repair_v2 as jr  # noqa: E402
import jsonl_repair as v1  # noqa: E402  (read only: the legacy repair is run on copies)

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"
api.running_session_ids = lambda: set()      # no PowerShell in the tests; liveness by name is tested below

RESULTS = []


def check(name, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  {ascii(extra)}" if extra else ""))


def fresh_env(tag):
    """Point every path the repair code can write at a new folder under HOME."""
    root = Path(tempfile.mkdtemp(prefix=f"{tag}_", dir=HOME))
    jr.PROJECTS_DIR = root / "projects"
    jr.BACKUPS_DIR = root / "backups" / "sessions"
    jr.SCHEDULE_FILE = root / "schedule.json"
    gk.STATE_DIR = root / "state"
    gk.LOG_DIR = root / "logs"
    gk.ARCHIVE_STATE = root / "archive-state"
    gk.USAGE_STATE = root / "usage-state"
    gk.REMOVED_LINES_ARCHIVE = jr.BACKUPS_DIR / "removed-lines-archive.jsonl"
    v1.BACKUPS_DIR = jr.BACKUPS_DIR
    v1.REMOVED_LINES_ARCHIVE = jr.BACKUPS_DIR / "v1-removed.jsonl"   # v1's own archive, kept apart
    v1.REPAIR_LOG = root / "logs" / "v1.log"
    for p in (jr.PROJECTS_DIR, jr.BACKUPS_DIR, gk.REMOVED_LINES_ARCHIVE, gk.STATE_DIR, jr.SCHEDULE_FILE):
        assert str(p).startswith(HOME), f"write path outside the temporary home: {p}"
    return root


def U(tag):
    return json.dumps({"type": "user", "uuid": f"u-{tag}", "message": {"content": "hi " + tag}}).encode("utf-8")


def jl(*lines):
    return b"\n".join(lines) + b"\n"


def write_old(path, raw, hours=2.0):
    """Write a file with an mtime `hours` in the past, outside the one-hour live window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def archive_recs():
    p = gk.REMOVED_LINES_ARCHIVE
    if not p.exists():
        return []
    return [json.loads(ln.decode("utf-8", "surrogateescape")) for ln in p.read_bytes().split(b"\n") if ln.strip()]


def recs_for(path):
    return [r for r in archive_recs() if r["source_file"] == str(path)]


def run_main(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = jr.main(args)
    return rc, buf.getvalue()


def snap(root):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


# The fixture: valid lines, an exact duplicate, a blank line, two repairable lines, a control-only
# line, an unparseable line with a \xff byte and trailing spaces, and a CRLF line.
FIX = [U("a"), U("b"), U("a"), b"   ", b'{"type":"user","uuid":"m1","message":{"content":"x"',
       b'{"type":"user","uuid":"m2","a":1,}', b"\x00\x07", b"garbage not json \xff  ", U("c") + b"\r"]


def t_equivalence_on_fixtures():
    fresh_env("eq")
    raw = jl(*FIX)
    p1, p2 = jr.PROJECTS_DIR / "P" / "v1.jsonl", jr.PROJECTS_DIR / "P" / "v2.jsonl"
    write_old(p1, raw)
    write_old(p2, raw)
    s1 = v1.repair_file(p1, backup=True, dry_run=False)
    s2 = jr.repair_file(p2)
    out1 = [x for x in p1.read_bytes().split(b"\n") if x.strip()]
    out2 = [x for x in p2.read_bytes().split(b"\n") if x.strip()]
    k1 = [x.strip() for x in out1]
    k2 = [x.strip() for x in out2]
    for i, (a, b) in enumerate(zip(out1, out2), 1):
        if a != b:
            print(f"  DIFF kept line {i}: v1 {ascii(a)} | v2 {ascii(b)}")
    if len(out1) != len(out2):
        print(f"  DIFF kept line count: v1 {len(out1)} | v2 {len(out2)}")
    if s1.get("removed_lines") != s2.get("removed_lines"):
        print(f"  DIFF removed count: v1 {s1.get('removed_lines')} | v2 {s2.get('removed_lines')}")
    if s1.get("repaired_lines") != s2.get("repaired_lines"):
        print(f"  DIFF repaired count: v1 {s1.get('repaired_lines')} | v2 {s2.get('repaired_lines')}")
    check("the same kept lines (stripped forms) and the same removed and repaired counts",
          k1 == k2 and s1.get("removed_lines") == s2.get("removed_lines") == 3
          and s1.get("repaired_lines") == s2.get("repaired_lines") == 2,
          f"kept v1={len(k1)} v2={len(k2)}")
    check("v2 keeps the unparseable line byte-exact, with its \\xff byte and trailing spaces",
          b"garbage not json \xff  " in out2)


def t_no_line_lost():
    fresh_env("lost")
    path = jr.PROJECTS_DIR / "P" / "lost.jsonl"
    raw = jl(*FIX)
    write_old(path, raw)
    jr.repair_file(path)
    recs = recs_for(path)
    lines = raw.split(b"\n")
    got = {r["source_line"]: r for r in recs}
    check("every removed line is in the archive (lines 3-7, five records)",
          set(got) == {3, 4, 5, 6, 7} and len(recs) == 5, f"lines={sorted(got)} records={len(recs)}")
    check("every record's content equals the removed line's text",
          all(r["content"].encode("utf-8", "surrogateescape") == lines[r["source_line"] - 1] for r in recs))


def t_blank_lines_archived():
    fresh_env("blank")
    path = jr.PROJECTS_DIR / "P" / "blank.jsonl"
    write_old(path, jl(U("p"), b"", b"  \t", U("q")))
    jr.repair_file(path)
    blank = [r for r in recs_for(path) if r["reason"] == "blank line"]
    data = path.read_bytes()
    check("blank lines are removed and each is archived with reason 'blank line'",
          sorted(r["source_line"] for r in blank) == [2, 3] and data == jl(U("p"), U("q")),
          f"blank={len(blank)}")


def t_raw_bytes_kept():
    fresh_env("raw")
    path = jr.PROJECTS_DIR / "P" / "raw.jsonl"
    write_old(path, jl(U("r"), b"not json \xff  ", U("r")))         # the duplicate forces a rewrite
    s = jr.repair_file(path)
    check("an unparseable line with \\xff and trailing spaces is kept byte-exact",
          s.get("rewritten") and b"not json \xff  \n" in path.read_bytes(), f"stats={s.get('aborted', '-')}")


def t_subagent_included():
    fresh_env("sub")
    sub = jr.PROJECTS_DIR / "P" / "sess-1" / "subagents" / "agent-x.jsonl"
    write_old(sub, jl(U("s"), U("s"), U("t")))
    rc, out = run_main(["--all", "--force"])
    check("a subagent transcript with a duplicate is repaired by the sweep",
          sub.read_bytes() == jl(U("s"), U("t")), out.strip()[-120:])


def t_live_file_skipped_and_rewrite_aborted():
    fresh_env("live")
    recent = jr.PROJECTS_DIR / "P" / "recent.jsonl"
    recent.parent.mkdir(parents=True, exist_ok=True)
    recent.write_bytes(jl(U("x"), U("x")))                          # modified now
    rc, out = run_main(["--all", "--force"])
    check("a file modified in the last hour is skipped", recent.read_bytes() == jl(U("x"), U("x"))
          and "1 recent" in out, out.strip()[-120:])
    running = jr.PROJECTS_DIR / "P" / "liveone.jsonl"
    write_old(running, jl(U("y"), U("y")))
    api.running_session_ids = lambda: {"liveone"}
    try:
        run_main(["--all", "--force"])
    finally:
        api.running_session_ids = lambda: set()
    check("a session on a running command line is skipped", running.read_bytes() == jl(U("y"), U("y")))
    changed = jr.PROJECTS_DIR / "P" / "changed.jsonl"
    raw = jl(U("z"), U("z"))
    write_old(changed, raw)
    real_backup = jr.make_backup

    def backup_then_append(p):
        b = real_backup(p)
        with open(p, "ab") as f:
            f.write(b"\n")
        return b

    jr.make_backup = backup_then_append
    try:
        s = jr.repair_file(changed)
    finally:
        jr.make_backup = real_backup
    check("a file changed between the check and the archive is aborted, and nothing is archived",
          "changed after backup" in s.get("aborted", "") and not recs_for(changed)
          and changed.read_bytes() == raw + b"\n", s.get("aborted", "-"))


def t_gate():
    fresh_env("gate")
    first = jr.PROJECTS_DIR / "P" / "g1.jsonl"
    write_old(first, jl(U("g"), U("g")))
    rc1, out1 = run_main(["--all"])
    check("the first --all runs", first.read_bytes() == jl(U("g")) and "rewritten 1" in out1, out1.strip()[-100:])
    second = jr.PROJECTS_DIR / "P" / "g2.jsonl"
    raw2 = jl(U("h"), U("h"))
    write_old(second, raw2)
    before = snap(gk.STATE_DIR.parent)
    rc2, out2 = run_main(["--all"])
    after = snap(gk.STATE_DIR.parent)
    check("a second --all within 6 hours is refused and writes nothing",
          "cooldown" in out2 and second.read_bytes() == raw2 and before == after, out2.strip()[-100:])
    rc3, out3 = run_main(["--all", "--force"])
    check("--all --force runs without the gate", second.read_bytes() == jl(U("h")) and "rewritten 1" in out3)


def t_cache_skips_clean_files():
    fresh_env("cache")
    clean = jr.PROJECTS_DIR / "P" / "clean.jsonl"
    write_old(clean, jl(U("k")))
    write_old(jr.PROJECTS_DIR / "P" / "dirty.jsonl", jl(U("d"), U("d")))
    calls = []
    real = jr.repair_file

    def spy(p, *a, **kw):
        calls.append(p.name)
        return real(p, *a, **kw)

    jr.repair_file = spy
    try:
        run_main(["--all", "--force"])
        first = list(calls)
        calls.clear()
        rc, out2 = run_main(["--all", "--force"])
        second = list(calls)
    finally:
        jr.repair_file = real
    check("a second sweep does not read the verified-clean file, and counts it as clean",
          "clean.jsonl" in first and "clean.jsonl" not in second and "1 verified clean" in out2,
          f"first={first} second={second} summary={out2.strip()[-90:]}")


def t_pool_isolation():
    fresh_env("pool")
    for name in ("ok1.jsonl", "bad.jsonl", "ok2.jsonl"):
        write_old(jr.PROJECTS_DIR / "P" / name, jl(U(name), U(name)))
    real = jr.repair_file

    def boom(p, *a, **kw):
        if p.name == "bad.jsonl":
            raise RuntimeError("injected failure")
        return real(p, *a, **kw)

    jr.repair_file = boom
    try:
        rc, out = run_main(["--all", "--force"])
    finally:
        jr.repair_file = real
    check("one file that raises is counted as an error and the others are repaired",
          "errors 1" in out and "rewritten 2" in out
          and (jr.PROJECTS_DIR / "P" / "ok1.jsonl").read_bytes() == jl(U("ok1.jsonl"))
          and (jr.PROJECTS_DIR / "P" / "ok2.jsonl").read_bytes() == jl(U("ok2.jsonl")), out.strip()[-160:])


def t_dry_run_writes_nothing():
    root = fresh_env("dry")
    path = jr.PROJECTS_DIR / "P" / "dry.jsonl"
    write_old(path, jl(U("n"), U("n")))
    before = snap(root)
    rc1, out1 = run_main(["--all", "--dry-run"])
    rc2, out2 = run_main([str(path), "--dry-run"])
    after = snap(root)
    check("--dry-run writes nothing (no new file, no change) and reports the work",
          before == after and "would rewrite 1" in out1 and path.read_bytes() == jl(U("n"), U("n")),
          out1.strip()[-100:])


TESTS = [t_equivalence_on_fixtures, t_no_line_lost, t_blank_lines_archived, t_raw_bytes_kept,
         t_subagent_included, t_live_file_skipped_and_rewrite_aborted, t_gate, t_cache_skips_clean_files,
         t_pool_isolation, t_dry_run_writes_nothing]

for fn in TESTS:
    try:
        fn()
    except Exception as exc:                  # one broken test must not hide the others
        check(fn.__name__ + " ran without an exception", False, f"{type(exc).__name__}: {exc}")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if RESULTS and all(RESULTS) else 1)
