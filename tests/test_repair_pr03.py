"""
Tests for scripts/api_repair_v2.py as changed in PR-03: the order of writes in repair_file, the
idempotent and lossless removed-lines archive, the running-session guard on the SessionEnd path,
the resumable sweep (cache closed in a finally block) and the relink timing.

Run from the worktree root:  python -I tests\\test_repair_pr03.py
Every test writes only under a fresh temporary SESSION_GUARD_HOME. Each check prints PASS or FAIL.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Set SESSION_GUARD_HOME before guardkit is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="pr03_test_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import api_repair_v2 as new  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"
# Read only: T6 copies transcripts out of this folder and never writes into it.
REAL_PROJECTS = Path.home() / ".claude" / "projects"

spec = importlib.util.spec_from_file_location("legacy_api", str(SCRIPTS / "api_repair.py"))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

RESULTS = []
STATE = {}


def check(name, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))


def fresh_env(tag):
    """Point every path the repair code can write at a new folder under HOME. Returns that folder."""
    root = Path(tempfile.mkdtemp(prefix=f"{tag}_", dir=HOME))
    new.PROJECTS_DIR = root / "projects"
    new.BACKUPS_DIR = root / "backups"
    new.REMOVED_LINES_ARCHIVE = new.BACKUPS_DIR / "removed-lines-archive.jsonl"
    new.SCHEDULE_FILE = root / "schedule.json"
    gk.STATE_DIR = root / "state"
    gk.LOG_DIR = root / "logs"
    gk.ARCHIVE_STATE = root / "archive-state"
    gk.USAGE_STATE = root / "usage-state"
    for p in (new.PROJECTS_DIR, new.BACKUPS_DIR, new.REMOVED_LINES_ARCHIVE, new.SCHEDULE_FILE,
              gk.STATE_DIR, gk.LOG_DIR, gk.ARCHIVE_STATE, gk.USAGE_STATE):
        assert str(p).startswith(HOME), f"write path outside the temporary home: {p}"
    return root


def jl(objs):
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode("utf-8")


def write_old(path, raw, hours=2.0):
    """Write a file and set its mtime `hours` into the past, outside the sweep's recency window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def user_obj(tag):
    return {"type": "user", "uuid": f"u-{tag}", "parentUuid": None, "message": {"content": "hi"}}


def empty_obj(tag, parent):
    return {"type": "assistant", "uuid": f"a-{tag}", "parentUuid": parent,
            "message": {"id": f"m-{tag}", "content": [{"type": "thinking", "thinking": "\n", "signature": ""}]}}


def text_obj(tag, parent):
    return {"type": "assistant", "uuid": f"b-{tag}", "parentUuid": parent,
            "message": {"id": f"t-{tag}", "content": [{"type": "text", "text": "t"}]}}


def dirty_raw(tag):
    return jl([user_obj(tag), empty_obj(tag, f"u-{tag}"), text_obj(tag, f"a-{tag}")])


def two_dirty_raw(tag):
    return jl([user_obj(tag),
               empty_obj(tag + "1", f"u-{tag}"), text_obj(tag + "1", f"a-{tag}1"),
               empty_obj(tag + "2", f"b-{tag}1"), text_obj(tag + "2", f"a-{tag}2")])


def archive_records_binary():
    """Every archive record, read as bytes and decoded with surrogateescape, one JSON object per line."""
    p = new.REMOVED_LINES_ARCHIVE
    if not p.exists():
        return []
    return [json.loads(ln.decode("utf-8", "surrogateescape"))
            for ln in p.read_bytes().split(b"\n") if ln.strip()]


def archived_for(path):
    return [r for r in archive_records_binary() if r.get("source_file") == str(path)]


def strip_cr(b):
    """Drop one trailing carriage return, for comparing legacy (text mode) output with v2 (bytes)."""
    return b[:-1] if b.endswith(b"\r") else b


# ---- T2: invalid UTF-8 in a removed line is repaired and archived byte for byte ----
def t2_invalid_utf8():
    root = fresh_env("t2")
    path = new.PROJECTS_DIR / "P" / "bad.jsonl"
    good = json.dumps(user_obj("bad")).encode("utf-8")
    bad_line = (b'{"type":"assistant","uuid":"a-bad","parentUuid":"u-bad","note":"caf\xe9 \xff",'
                b'"message":{"id":"m-bad","content":[{"type":"thinking","thinking":"\\n","signature":""}]}}')
    tail = json.dumps(text_obj("bad", "a-bad")).encode("utf-8")
    raw = good + b"\n" + bad_line + b"\n" + tail + b"\n"
    write_old(path, raw)
    stats = new.repair_file(path, dry_run=False, force=False)
    check("T2 repair succeeds on a line with invalid UTF-8",
          not stats.get("aborted") and not stats.get("error") and stats.get("lines_removed") == 1,
          f"lines_removed={stats.get('lines_removed')} aborted={stats.get('aborted', '-')}")
    now = path.read_bytes()
    # The line after the removed one is re-pointed at its kept parent (the existing relink rule).
    relinked_tail = json.dumps(text_obj("bad", "u-bad")).encode("utf-8")
    check("T2 the empty-thinking line is gone; the other lines are kept, the child re-pointed to u-bad",
          bad_line not in now and good in now and relinked_tail in now)
    mine = archived_for(path)
    check("T2 archive record round-trips the exact original bytes",
          len(mine) == 1 and mine[0]["content"].encode("utf-8", "surrogateescape") == bad_line,
          f"records={len(mine)}")
    STATE["t2"] = {"path": path, "bad_line": bad_line, "root": root}


# ---- T9: archive format, read the way session_archive reads it ----
def t9_archive_format():
    info = STATE["t2"]
    lines = [ln for ln in new.REMOVED_LINES_ARCHIVE.read_bytes().split(b"\n") if ln.strip()]
    parsed = []
    parse_ok = True
    for ln in lines:
        try:
            parsed.append(json.loads(ln.decode("utf-8", "surrogateescape")))
        except ValueError:
            parse_ok = False
    check("T9 every archive line parses after surrogateescape decoding", parse_ok and len(parsed) == 1,
          f"lines={len(lines)} parsed={len(parsed)}")
    keys = set()
    with open(new.REMOVED_LINES_ARCHIVE, "r", encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if isinstance(rec.get("content"), str):
                keys.add(rec["content"])
    expected = info["bad_line"].decode("utf-8", "surrogateescape")
    check("T9 the text-mode reader returns the content string the writer used", keys == {expected},
          f"keys={len(keys)}")
    idx_bin = info["root"] / "state" / "removed_index.bin"
    check("T9 the digest index holds one 16-byte digest per archived record",
          idx_bin.exists() and idx_bin.stat().st_size == 16 * len(parsed),
          f"bytes={idx_bin.stat().st_size if idx_bin.exists() else 'missing'}")
    tricky = 'a b "q" \\ end'
    w, sk = new.archive_dropped(Path(HOME) / "fake" / "y.jsonl", [(1, "r", tricky)])
    back = [r for r in archive_records_binary() if r.get("source_file", "").endswith("y.jsonl")]
    check("T9 U+2028, quotes and backslashes round-trip through the archive",
          (w, sk) == (1, 0) and len(back) == 1 and back[0]["content"] == tricky, f"written={w} skipped={sk}")


# ---- T3: a file changed after the backup archives nothing and is left as it is ----
def t3_aborted_run_archives_nothing():
    fresh_env("t3")
    path = new.PROJECTS_DIR / "P" / "race.jsonl"
    raw = dirty_raw("t3")
    write_old(path, raw)
    real_backup = new.make_backup

    def backup_then_append(p):
        b = real_backup(p)
        with open(p, "ab") as f:
            f.write(b"\n")
        return b

    new.make_backup = backup_then_append
    try:
        stats = new.repair_file(path, dry_run=False, force=False)
    finally:
        new.make_backup = real_backup
    ab = stats.get("aborted", "")
    check("T3 the aborted run reports that the file changed", "changed" in ab, f"aborted={ab!r}")
    check("T3 the aborted run archives nothing", len(archived_for(path)) == 0)
    check("T3 the file keeps the appended line and is not rewritten", path.read_bytes() == raw + b"\n")


# ---- T4: a second run over the same dirty content archives nothing again ----
def t4_second_run_is_idempotent():
    fresh_env("t4")
    path = new.PROJECTS_DIR / "P" / "again.jsonl"
    raw = two_dirty_raw("t4")
    write_old(path, raw)
    s1 = new.repair_file(path, dry_run=False, force=False)
    n1 = s1.get("archived")
    check("T4 the first run archives both removed lines", n1 == 2 and s1.get("archive_skipped") == 0,
          f"archived={n1} skipped={s1.get('archive_skipped')}")
    write_old(path, raw)                     # the same dirty content again, with a new mtime
    s2 = new.repair_file(path, dry_run=False, force=False)
    check("T4 the second run archives 0 records", s2.get("archived") == 0, f"archived={s2.get('archived')}")
    check("T4 the second run skips what the first run archived", s2.get("archive_skipped") == n1,
          f"skipped={s2.get('archive_skipped')} first_archived={n1}")
    check("T4 each record is in the archive once", len(archived_for(path)) == n1,
          f"records={len(archived_for(path))}")


# ---- T5: running-session guard on the SessionEnd path, with no recency check ----
def t5_session_end_guard():
    fresh_env("t5")
    path = new.PROJECTS_DIR / "P" / "ending.jsonl"
    raw = dirty_raw("t5")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)                    # written just now, as at SessionEnd
    real_running = new.running_session_ids
    old_env = os.environ.get("CLAUDE_SESSION_ID")
    try:
        new.running_session_ids = lambda: {path.stem}
        s1 = new.repair_file(path, dry_run=False, force="session_end")
        check("T5 a session still on a running command line is skipped",
              s1.get("skipped") == "refusing: session is still running", f"result={s1}")
        check("T5 the skipped file is unchanged", path.read_bytes() == raw)
        new.running_session_ids = lambda: set()
        os.environ["CLAUDE_SESSION_ID"] = path.stem      # must have no effect on this path
        t = time.time() - 5
        os.utime(path, (t, t))
        s2 = new.repair_file(path, dry_run=False, force="session_end")
        check("T5 an ended session written 5 s ago is repaired (no recency check)",
              not s2.get("skipped") and not s2.get("aborted") and s2.get("lines_removed") == 1,
              f"skipped={s2.get('skipped', '-')} aborted={s2.get('aborted', '-')}")
    finally:
        new.running_session_ids = real_running
        if old_env is None:
            os.environ.pop("CLAUDE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_SESSION_ID"] = old_env


# ---- T6: real transcripts, legacy analyser against v2, plus one injected line each ----
def t6_real_transcripts():
    fresh_env("t6")
    if not REAL_PROJECTS.is_dir():
        check("T6 the real projects folder is available", False, f"no folder at {REAL_PROJECTS}")
        return
    now = time.time()
    rows = [p for _rel, p, size, _mns, mtime_s in gk.transcript_files(REAL_PROJECTS)
            if size < 2_000_000 and now - mtime_s > 3600]
    random.Random(11).shuffle(rows)
    picked = []
    for src in rows:
        if len(picked) == 20:
            break
        lines = src.read_bytes().split(b"\n")          # read only
        anchor = None
        for i, ln in enumerate(lines):
            if b'"assistant"' not in ln:
                continue
            try:
                o = json.loads(ln.decode("utf-8", "surrogateescape"))
            except ValueError:
                continue
            if isinstance(o, dict) and o.get("type") == "assistant" and isinstance(o.get("uuid"), str) \
                    and isinstance(o.get("message"), dict):
                anchor = (i, o)
                break
        if anchor is not None:
            picked.append((src, lines, anchor))
    check("T6 found 20 real transcripts under 2 MB and older than one hour", len(picked) == 20,
          f"found={len(picked)}")
    mism, once_bad, empty_left, cr_bad, cr_files = [], [], [], [], []
    for k, (src, lines, (i, o)) in enumerate(picked):
        inj = copy.deepcopy(o)
        inj["uuid"] = "inj-" + uuid.uuid4().hex
        inj["parentUuid"] = o["uuid"]
        inj["message"]["id"] = "inj-m-" + uuid.uuid4().hex
        inj["message"]["content"] = [{"type": "thinking", "thinking": "\n\n", "signature": ""}]
        inj_bytes = json.dumps(inj, ensure_ascii=False).encode("utf-8", "surrogateescape")
        raw = b"\n".join(lines[:i + 1] + [inj_bytes] + lines[i + 1:])
        dest = new.PROJECTS_DIR / "proj" / f"t6-{k:02d}.jsonl"
        write_old(dest, raw)
        ls, lnew, ld = legacy.analyse(dest)
        ns, nnew, nd = new.analyse_bytes(dest, raw)
        # Legacy reads in text mode, which drops the carriage return of a CRLF line. v2 keeps the
        # bytes. Both sides therefore drop one trailing CR before comparing. Check B below proves
        # that v2 keeps every CR that an untouched line had.
        if b"\r\n" in raw:
            cr_files.append(src.name)
        lnorm = None if lnew is None else [strip_cr(x.encode("utf-8", "surrogateescape")) for x in lnew]
        nnorm = None if nnew is None else [strip_cr(x) for x in nnew]
        legacy_view = (ls.get("blocks_removed"), ls.get("lines_removed"), ls.get("relinked"), lnorm,
                       [strip_cr(d[2].encode("utf-8", "surrogateescape")) for d in ld])
        v2_view = (ns.get("blocks_removed"), ns.get("lines_removed"), ns.get("relinked"), nnorm,
                   [strip_cr(d[2].encode("utf-8", "surrogateescape")) for d in nd])
        if legacy_view != v2_view:
            mism.append(src.name)
        in_set = set(raw.split(b"\n"))
        if nnew is not None and not all(x in in_set for x in nnew if x.endswith(b"\r")):
            cr_bad.append(src.name)
        s = new.repair_file(dest, dry_run=False, force=False)
        n_inj = sum(1 for r in archived_for(dest)
                    if r["content"].encode("utf-8", "surrogateescape") == inj_bytes)
        if n_inj != 1 or s.get("aborted") or s.get("error"):
            once_bad.append((dest.name, n_inj, s.get("aborted", s.get("error", "-"))))
        left = 0
        for ln in dest.read_bytes().split(b"\n"):
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln.decode("utf-8", "surrogateescape"))
            except ValueError:
                continue
            m = obj.get("message") if isinstance(obj, dict) else None
            if isinstance(m, dict) and isinstance(m.get("content"), list) \
                    and any(new.is_empty_thinking(b) for b in m["content"]):
                left += 1
        if left:
            empty_left.append(dest.name)
    check("T6 legacy and v2 agree on removed lines, blocks, relinks and output lines (20 transcripts)",
          not mism and len(picked) == 20,
          f"mismatches={mism[:3]} of {len(picked)}; files with CRLF lines={len(cr_files)} (CR compared as noted)")
    check("T6 v2 keeps the CR of every untouched CRLF line byte for byte", not cr_bad, f"files={cr_bad[:3]}")
    check("T6 each injected line is archived exactly once", not once_bad, f"problems={once_bad[:3]}")
    check("T6 no empty thinking block remains in any repaired copy", not empty_left,
          f"files={empty_left[:3]}")


# ---- T7: relink performance, with the relink result checked as well ----
def t7_relink_timing():
    fresh_env("t7")
    root_uuid = str(uuid.uuid4())
    lines = [json.dumps({"type": "user", "uuid": root_uuid, "parentUuid": None, "message": {"content": "hi"}})]
    prev = root_uuid
    expect = {}                              # kept child uuid -> the nearest kept ancestor
    for k in range(2000):
        r, c = str(uuid.uuid4()), str(uuid.uuid4())
        lines.append(json.dumps({"type": "assistant", "uuid": r, "parentUuid": prev,
                                 "message": {"id": f"m{k}",
                                             "content": [{"type": "thinking", "thinking": "\n", "signature": ""}]}}))
        lines.append(json.dumps({"type": "user", "uuid": c, "parentUuid": r, "message": {"content": f"ok {k}"}}))
        expect[c] = prev
        prev = c
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    path = new.PROJECTS_DIR / "P" / "relink.jsonl"
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        stats, out, _dropped = new.analyse_bytes(path, raw)
        times.append(time.perf_counter() - t0)
    got = {}
    for ln in out:
        o = json.loads(ln.decode("utf-8", "surrogateescape"))
        got[o["uuid"]] = o["parentUuid"]
    check("T7 relink: 2,000 removed lines, 2,000 children re-pointed to the nearest kept ancestor",
          stats["lines_removed"] == 2000 and stats["relinked"] == 2000
          and all(got.get(c) == p for c, p in expect.items()),
          f"removed={stats['lines_removed']} relinked={stats['relinked']}")
    best = min(times)
    check("T7 relink time: best of 3 runs is 2 s or less", best <= 2.0,
          "runs=" + ", ".join(f"{t:.3f}s" for t in times) + f" best={best:.3f}s")


# ---- T8: a resumed session is skipped, the file is not touched, and the log says why ----
def t8_resume_is_skipped():
    fresh_env("t8")
    path = new.PROJECTS_DIR / "P" / "reopen.jsonl"
    raw = dirty_raw("t8")
    write_old(path, raw)
    real_payload = gk.read_hook_payload
    gk.read_hook_payload = lambda: {"end_reason": "resume", "transcript_path": str(path)}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = new.cmd_from_hook()
    finally:
        gk.read_hook_payload = real_payload
    log_file = gk.LOG_DIR / "api_repair.log"
    log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    check("T8 resume: the hook returns 0", rc == 0, f"rc={rc}")
    check("T8 resume: the transcript is unchanged and nothing is archived",
          path.read_bytes() == raw and not archived_for(path))
    check("T8 resume: the hook log says the transcript is about to be reopened",
          "end_reason=resume" in log and "about to be reopened" in log,
          (log.strip().splitlines() or ["no log"])[-1])


# ---- T8b: the documented SessionEnd field `reason` is read the same way as end_reason ----
def t8b_reason_resume_is_skipped():
    fresh_env("t8b")
    path = new.PROJECTS_DIR / "P" / "reopen.jsonl"
    raw = dirty_raw("t8b")
    write_old(path, raw)
    real_payload = gk.read_hook_payload
    gk.read_hook_payload = lambda: {"reason": "resume", "transcript_path": str(path)}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = new.cmd_from_hook()
    finally:
        gk.read_hook_payload = real_payload
    log_file = gk.LOG_DIR / "api_repair.log"
    log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    check("T8b reason: the hook returns 0", rc == 0, f"rc={rc}")
    check("T8b reason: the transcript is unchanged and nothing is archived",
          path.read_bytes() == raw and not archived_for(path))
    check("T8b reason: the hook log says the transcript is about to be reopened",
          "end_reason=resume" in log and "about to be reopened" in log,
          (log.strip().splitlines() or ["no log"])[-1])


# ---- T10: the index sees records written by another writer, and rebuilds after a truncation ----
def t10_index_refresh_and_rebuild():
    fresh_env("t10")
    path = new.PROJECTS_DIR / "P" / "idx.jsonl"
    raw = dirty_raw("t10")
    write_old(path, raw)
    s1 = new.repair_file(path, dry_run=False, force=False)
    check("T10 the first repair archives the removed line", s1.get("archived") == 1,
          f"archived={s1.get('archived')}")
    content = new.analyse_bytes(path, raw)[2][0][2]
    ext = {"archived_at": "x", "source_file": "elsewhere.jsonl", "source_line": 1,
           "reason": "r", "content": "ext line"}
    with open(new.REMOVED_LINES_ARCHIVE, "a", encoding="utf-8") as f:    # the old, unlocked writer style
        f.write(json.dumps(ext, ensure_ascii=False) + "\n")
    w, sk = new.archive_dropped(Path("elsewhere.jsonl"), [(1, "r", "ext line")])
    check("T10 a record appended by another writer is seen as already archived", (w, sk) == (0, 1),
          f"written={w} skipped={sk}")
    new.REMOVED_LINES_ARCHIVE.write_bytes(b"")                          # truncate the archive
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        w2, sk2 = new.archive_dropped(path, [(1, "r", content)])
    check("T10 after a truncation the index is rebuilt and the record is archived again",
          (w2, sk2) == (1, 0), f"written={w2} skipped={sk2}")
    check("T10 the truncation prints a warning", "smaller than its index" in err.getvalue(),
          err.getvalue().strip()[:100])


# ---- T11: a sweep that stops early still saves the clean-cache it has built ----
def t11_sweep_saves_cache_on_stop():
    fresh_env("t11")
    clean = jl([user_obj("c11")])
    for name in ("c1.jsonl", "c2.jsonl"):
        write_old(new.PROJECTS_DIR / "P" / name, clean)
    real_pool = gk.run_pool

    def stop_after_first(fn, items, workers=gk.POOL_WORKERS):
        items = list(items)
        item = items[0]
        yield item, ("clean", item[0], item[1].stat()), None
        raise RuntimeError("injected stop")

    gk.run_pool = stop_after_first
    raised = False
    try:
        new.sweep_incremental()
    except RuntimeError:
        raised = True
    finally:
        gk.run_pool = real_pool
    saved = gk.load_json(gk.STATE_DIR / "api_repair_clean.json", {})
    check("T11 a sweep that stops early still saves the clean-cache", raised and len(saved) == 1,
          f"raised={raised} saved={sorted(saved)}")


# ---- T12: the index streams the archive in small blocks; lines that cross a block edge are kept ----
def t12_index_across_blocks():
    fresh_env("t12")
    new.REMOVED_LINES_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    recs = []
    with open(new.REMOVED_LINES_ARCHIVE, "wb") as f:
        for k in range(300):
            rec = {"archived_at": "x", "source_file": str(Path("/p") / ("f%d.jsonl" % (k % 7))),
                   "source_line": k, "reason": "r", "content": f"line {k} " + "x" * (k % 50)}
            recs.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False).encode("utf-8") + b"\n")
    old_block = new.INDEX_READ_BLOCK
    new.INDEX_READ_BLOCK = 61                # odd size: most lines cross a block edge
    try:
        w, sk = new.archive_dropped(Path(recs[100]["source_file"]),
                                    [(1, "r", recs[100]["content"]), (2, "r", "brand new")])
    finally:
        new.INDEX_READ_BLOCK = old_block
    check("T12 a record in the middle of the archive is seen across 61-byte blocks",
          (w, sk) == (1, 1), f"written={w} skipped={sk}")
    idx_bin = gk.STATE_DIR / "removed_index.bin"
    idx_json = gk.load_json(gk.STATE_DIR / "removed_index.json", {})
    size = new.REMOVED_LINES_ARCHIVE.stat().st_size
    check("T12 the index holds one digest per record (301) and its offset is the archive size",
          idx_bin.stat().st_size == 16 * 301 and idx_json.get("archive_offset") == size,
          f"bytes={idx_bin.stat().st_size} offset={idx_json.get('archive_offset')} size={size}")


ALL = [t2_invalid_utf8, t9_archive_format, t3_aborted_run_archives_nothing, t4_second_run_is_idempotent,
       t5_session_end_guard, t6_real_transcripts, t7_relink_timing, t8_resume_is_skipped,
       t8b_reason_resume_is_skipped, t10_index_refresh_and_rebuild, t11_sweep_saves_cache_on_stop, t12_index_across_blocks]

for fn in ALL:
    try:
        fn()
    except Exception as exc:                 # one broken test must not hide the others
        check(fn.__name__ + " ran without an exception", False, f"{type(exc).__name__}: {exc}")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if RESULTS and all(RESULTS) else 1)
