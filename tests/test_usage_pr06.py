"""
Tests for scripts/usage_db_v2.py (PR-06): the incremental usage store.

Run from the worktree root:  python -I tests\\test_usage_pr06.py
Every test works only under a fresh temporary SESSION_GUARD_HOME. Each test prints PASS or FAIL.
The live usage_db.py in scripts/ is imported only for its SQL text and its line parser, to build
a reference window-function result in an in-memory database. No function that opens a database
file is called on it.
"""
import contextlib
import csv
import io
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Set SESSION_GUARD_HOME before any plugin code is imported, so nothing touches real data.
HOME = tempfile.mkdtemp(prefix="pr06_test_home_")
os.environ["SESSION_GUARD_HOME"] = HOME
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import usage_db as live  # noqa: E402
import usage_db_v2 as v2  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(HOME), "test would touch real data"
assert str(v2.CLAUDE_DIR).startswith(HOME), "test would touch real data"
assert str(v2.DB_PATH).startswith(HOME), "test would touch real data"

SLUG = "C--work-pr06-test"
MODELS = ["claude-opus-4-1-20250805", "claude-sonnet-4-5-20250929",
          "claude-haiku-4-5-20251001", "mystery-local-model"]   # the last one has no price
BASE = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)


def reset():
    """Empty the archive, the projects folder and the database. All are under HOME."""
    for d in (v2.ARCHIVE_DIR, v2.PROJECTS_DIR):
        assert str(d).startswith(HOME), f"refusing to delete {d}"
        shutil.rmtree(d, ignore_errors=True)
    v2.SCHEMA_RUNS = 0


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def usage_line(uuid, sid, mid, ts, model, inp, out, cc, cr, sidechain=False,
               cwd="C:/work/proj-x"):
    cc5 = cc // 2
    obj = {"type": "assistant", "uuid": uuid, "sessionId": sid, "requestId": "req_" + uuid,
           "timestamp": ts, "cwd": cwd, "version": "2.1.0", "gitBranch": "main",
           "isSidechain": sidechain,
           "message": {"id": mid, "model": model, "stop_reason": "end_turn",
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_creation_input_tokens": cc,
                                 "cache_read_input_tokens": cr,
                                 "cache_creation": {"ephemeral_5m_input_tokens": cc5,
                                                    "ephemeral_1h_input_tokens": cc - cc5},
                                 "service_tier": "standard"}}}
    return json.dumps(obj) + "\n"


def make_lines(rng, tag, sid, n_msgs, sidechain=False, start=BASE, cwd="C:/work/proj-x"):
    """Lines for n_msgs API responses. A response is written as 1 to 3 lines sharing message.id.
    Every line repeats the same usage, except the last, which carries the larger final usage.
    Some responses tie on total, so the timestamp decides. Noise lines are mixed in."""
    lines, t = [], start
    for m in range(n_msgs):
        mid = f"msg_{tag}_{m:04d}"
        model = rng.choice(MODELS)
        t += timedelta(minutes=rng.randint(1, 240), seconds=rng.randint(0, 59))
        base = [rng.randint(1, 300), rng.randint(1, 900), rng.randint(0, 5000), rng.randint(0, 40000)]
        k = rng.choice([1, 1, 2, 3])
        if rng.random() < 0.1:                # tie: identical usage on every line
            final = base
        else:
            final = [base[0] + rng.randint(0, 50), base[1] + rng.randint(1, 400),
                     base[2], base[3] + rng.randint(0, 2000)]
        for j in range(k):
            usage = final if j == k - 1 else base
            ts = iso(t + timedelta(seconds=j))
            lines.append(usage_line(f"u-{tag}-{m:04d}-{j}", sid, mid, ts, model, *usage,
                                    sidechain=sidechain, cwd=cwd))
        if m % 7 == 0:
            lines.append(json.dumps({"type": "user", "uuid": f"usr-{tag}-{m:04d}", "sessionId": sid,
                                     "timestamp": iso(t),
                                     "message": {"role": "user", "content": "hello"}}) + "\n")
        if m % 11 == 0:
            lines.append(json.dumps({"type": "user", "uuid": f"txt-{tag}-{m:04d}", "sessionId": sid,
                                     "timestamp": iso(t),
                                     "message": {"role": "user", "content": "the usage field"}}) + "\n")
            lines.append('{"type":"assistant","uuid":"bad-' + f"{tag}-{m}" +
                         '","message":{"usage":{"input_tokens":5\n')          # broken JSON
            lines.append(json.dumps({"type": "assistant", "sessionId": sid, "timestamp": iso(t),
                                     "message": {"id": mid, "usage": {"input_tokens": 999}}}) + "\n")  # no uuid
    return lines


def fixture(seed=7):
    """Archive path (relative to TRANSCRIPTS_DIR) -> lines. Four sessions, two subagent files."""
    rng = random.Random(seed)
    files = {}
    for i, sid in enumerate(["sess-a", "sess-b", "sess-c", "sess-d"]):
        files[f"{SLUG}/{sid}.jsonl"] = make_lines(rng, f"s{i}", sid, 40)
    files[f"{SLUG}/sess-a/subagents/agent-a1.jsonl"] = make_lines(rng, "sa1", "sess-a", 25, sidechain=True)
    files[f"{SLUG}/sess-b/subagents/workflows/wf1/agent-b1.jsonl"] = make_lines(rng, "sb1", "sess-b", 20,
                                                                              sidechain=True)
    return files


def write_part(files, part, parts=3):
    """Append one third of every archive file. sess-c is written with CRLF endings on purpose."""
    for rel, lines in files.items():
        n = len(lines)
        lo, hi = part * n // parts, (part + 1) * n // parts
        eol = b"\r\n" if rel.endswith("sess-c.jsonl") else b"\n"
        dst = v2.TRANSCRIPTS_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "ab") as f:
            for line in lines[lo:hi]:
                f.write(line.rstrip("\n").encode("utf-8") + eol)


def write_file(dst, lines):
    """Write a whole file with LF endings, as Claude Code does."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        for line in lines:
            f.write(line.rstrip("\n").encode("utf-8") + b"\n")


def oracle(all_lines):
    """Expected totals from the raw lines alone, with no database: each uuid counted once, then
    the winner per (session_id, message_id) by total, then timestamp, then smallest uuid."""
    by_uuid = {}
    for raw in all_lines:
        if '"usage"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict) or not e.get("uuid"):
            continue
        total = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                 "cache_creation_input_tokens", "cache_read_input_tokens"))
        epoch = int(datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp())
        by_uuid.setdefault(e["uuid"], (e.get("sessionId") or "", msg.get("id") or "", total, epoch))
    winners = {}
    for uid, (sid, mid, total, epoch) in by_uuid.items():
        cand = (-total, -epoch, uid)
        if (sid, mid) not in winners or cand < winners[(sid, mid)][0]:
            winners[(sid, mid)] = (cand, total)
    return {"raw_lines": len(by_uuid),
            "raw_total": sum(t for _, _, t, _ in by_uuid.values()),
            "messages": len(winners),
            "dedup_total": sum(t for _, t in winners.values())}


def run(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


def sync():
    return run(v2.ingest, False)


def run_main(*argv):
    old = sys.argv
    sys.argv = ["usage_db_v2.py", *argv]
    try:
        return run(v2.main)
    finally:
        sys.argv = old


def snapshot():
    """Every table that must match between an incremental run and a full build."""
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        snap = {"usage_events": con.execute("SELECT * FROM usage_events ORDER BY uuid").fetchall(),
                "usage_dedup": con.execute(
                    "SELECT * FROM usage_dedup ORDER BY session_id, message_id").fetchall(),
                "model_pricing": con.execute(
                    "SELECT * FROM model_pricing ORDER BY model_pattern").fetchall()}
        for name in v2.VIEW_NAMES:
            snap[name] = sorted(con.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
        return snap
    finally:
        con.close()


def first_diff(x, y):
    return next(((i, a, b) for i, (a, b) in enumerate(zip(x, y)) if a != b), (min(len(x), len(y)), None, None))


# ---------------------------------------------------------------- tests

def t_dedup_matches_window_function():
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    sync()
    ref = sqlite3.connect(":memory:")
    ref.executescript(live.SCHEMA)                       # the live tool's schema and window view
    for rel in files:
        ref.executemany(live.INSERT_SQL, live.rows_from_file(v2.TRANSCRIPTS_DIR / rel, rel))
    want = ref.execute("SELECT session_id, message_id, uuid, total_tokens FROM v_events_dedup "
                       "ORDER BY 1, 2").fetchall()
    con = sqlite3.connect(str(v2.DB_PATH))
    got = con.execute("SELECT session_id, message_id, uuid, total_tokens FROM usage_dedup "
                      "ORDER BY 1, 2").fetchall()
    con.close()
    assert got == want, (f"usage_dedup has {len(got)} rows, window function {len(want)}; "
                         f"first difference at {first_diff(got, want)}")
    return f"{len(got)} rows equal"


def t_incremental_equals_full_rebuild():
    reset()
    files = fixture()
    timings = []
    for part in range(3):
        write_part(files, part)
        t0 = time.perf_counter()
        rc, _ = sync()
        timings.append(time.perf_counter() - t0)
        assert rc == 0, rc
    inc = snapshot()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(v2.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.perf_counter()
    rc, build_out = run(v2.ingest, True)
    build_s = time.perf_counter() - t0
    full = snapshot()
    print(f"TIMING three incremental syncs (s): {[round(x, 3) for x in timings]}; "
          f"full build (s): {round(build_s, 3)}; {build_out.strip()}")
    diffs = [k for k in inc if inc[k] != full.get(k)]
    assert not diffs, f"incremental and full build differ in: {diffs[:5]}"
    return f"{len(inc['usage_events'])} events, {len(inc['usage_dedup'])} messages, all views equal"


def t_period_views_sum_to_total():
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    sync()
    con = sqlite3.connect(str(v2.DB_PATH))
    total = con.execute("SELECT SUM(total_tokens) FROM usage_dedup").fetchone()[0]
    views = ["v_daily", "v_monthly", "v_weekly", "v_by_model", "v_by_session", "v_by_project",
             "v_hourly", "v_hourly_ts", "v_blocks_5h", "v_sessions_timeline",
             "v_daily_by_model", "v_monthly_by_model", "v_weekly_by_model", "v_hourly_ts_by_model"]
    deltas = {}
    for view in views:
        s = con.execute(f"SELECT SUM(total_tokens) FROM {view}").fetchone()[0]
        deltas[view] = (total or 0) - (s or 0)
    con.close()
    bad = {k: v for k, v in deltas.items() if v != 0}
    assert not bad, f"non-zero deltas against usage_dedup total {total}: {bad}"
    return f"total {total:,} tokens; {len(views)} views, all delta 0"


def t_schema_created_once():
    reset()
    files = fixture()
    write_part(files, 0)
    sync()
    sync()
    assert v2.SCHEMA_RUNS == 1, f"schema created {v2.SCHEMA_RUNS} times in two syncs"
    con = sqlite3.connect(str(v2.DB_PATH))
    ver = con.execute("SELECT version FROM schema_version").fetchall()
    con.close()
    assert ver == [(v2.SCHEMA_VERSION,)], ver
    return "schema created 1 time in 2 syncs"


def t_no_double_count_across_live_and_archive():
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    rng = random.Random(99)
    a_key = f"{SLUG}/sess-a.jsonl"
    b_key = f"{SLUG}/sess-b.jsonl"
    e_key = f"{SLUG}/sess-e.jsonl"
    # Live copy of sess-a: the archive lines plus later messages, so it is bigger and is read.
    live_a = files[a_key] + make_lines(rng, "live-a", "sess-a", 6, start=BASE + timedelta(days=3))
    # Live copy of sess-b: identical to the archive copy, so it is skipped by sources().
    # Live-only session sess-e: no archive copy, read under its own key.
    live_e = make_lines(rng, "sess-e", "sess-e", 8)
    write_file(v2.PROJECTS_DIR / a_key, live_a)
    write_file(v2.PROJECTS_DIR / b_key, files[b_key])
    write_file(v2.PROJECTS_DIR / e_key, live_e)
    sync()
    all_lines = [ln for ls in files.values() for ln in ls] + live_a + live_e
    want = oracle(all_lines)
    con = sqlite3.connect(str(v2.DB_PATH))
    got_total, got_rows = con.execute("SELECT SUM(total_tokens), COUNT(*) FROM usage_events").fetchone()
    got_dedup = con.execute("SELECT SUM(total_tokens), COUNT(*) FROM usage_dedup").fetchone()
    con.close()
    assert got_rows == want["raw_lines"], f"usage_events rows {got_rows}, unique uuids {want['raw_lines']}"
    assert got_total == want["raw_total"], f"raw total {got_total}, expected {want['raw_total']}"
    assert got_dedup == (want["dedup_total"], want["messages"]), \
        f"dedup {got_dedup}, expected {(want['dedup_total'], want['messages'])}"
    return f"{want['raw_lines']} unique lines; dedup total {want['dedup_total']:,}, no double count"


def t_stats_labels_both_totals():
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    sync()
    rc, out = run_main("stats")
    assert rc == 0, out
    assert "TOTAL tokens (deduplicated per message)" in out, out
    assert "raw per-line sum (NOT a usage total)" in out, out

    def value(label):
        line = next(ln for ln in out.splitlines() if label in ln)
        return int(line.split()[-1].replace(",", ""))

    dedup = value("TOTAL tokens (deduplicated per message)")
    raw = value("raw per-line sum (NOT a usage total)")
    want = oracle([ln for ls in files.values() for ln in ls])
    assert dedup == want["dedup_total"], (dedup, want["dedup_total"])
    assert raw == want["raw_total"], (raw, want["raw_total"])
    assert raw > dedup, (raw, dedup)
    return f"dedup {dedup:,} < raw {raw:,}"


def t_export_is_separate():
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    sync()
    csv_after_sync = list(v2.REPORTS_DIR.glob("*.csv")) if v2.REPORTS_DIR.exists() else []
    assert not csv_after_sync, f"sync wrote {len(csv_after_sync)} CSV files"
    rc, out = run_main("export")
    assert rc == 0, out
    names = [n for _, n in v2.CSV_EXPORTS]
    missing = [n for n in names if not (v2.REPORTS_DIR / n).exists()]
    assert not missing, f"export did not write {missing}"
    leftovers = [p.name for p in v2.REPORTS_DIR.iterdir() if p.name.endswith(".tmp") or p.name == ".export.lock"]
    assert not leftovers, f"left behind: {leftovers}"
    with open(v2.REPORTS_DIR / "models.csv", newline="", encoding="utf-8") as f:
        total = sum(int(r["total_tokens"]) for r in csv.DictReader(f))
    want = oracle([ln for ls in files.values() for ln in ls])["dedup_total"]
    assert total == want, (total, want)
    return f"sync wrote no CSV; export wrote {len(names)} files; models.csv total equals dedup total"


def t_sql_is_read_only():
    reset()
    files = fixture()
    write_part(files, 0)
    sync()

    def state():
        con = sqlite3.connect(str(v2.DB_PATH))
        try:
            return (con.execute("SELECT COUNT(*), SUM(total_tokens) FROM usage_events").fetchone(),
                    con.execute("SELECT COUNT(*), SUM(total_tokens) FROM usage_dedup").fetchone(),
                    con.execute("SELECT * FROM model_pricing ORDER BY model_pattern").fetchall())
        finally:
            con.close()

    before = state()
    rc1, out1 = run_main("sql", "UPDATE usage_events SET model = 'x'")
    rc2, out2 = run_main("sql", "WITH x AS (SELECT 1) DELETE FROM usage_events")
    rc3, out3 = run_main("sql", "SELECT COUNT(*) AS n FROM usage_events")
    assert rc1 == 1 and "refusing" in out1, (rc1, out1)
    assert rc2 == 1 and "sql failed" in out2, (rc2, out2)
    assert rc3 == 0 and out3.splitlines()[0] == "n", (rc3, out3)
    assert state() == before, "database changed"
    return "UPDATE refused; WITH...DELETE rejected by mode=ro; database unchanged"


def t_pricing_seed_once():
    reset()
    files = fixture()
    write_part(files, 0)
    sync()
    con = sqlite3.connect(str(v2.DB_PATH))
    con.execute("UPDATE model_pricing SET input_per_mtok = 99.5 WHERE model_pattern = 'claude-opus-4'")
    con.commit()
    con.close()
    write_part(files, 1)
    sync()
    con = sqlite3.connect(str(v2.DB_PATH))
    price = con.execute("SELECT input_per_mtok FROM model_pricing WHERE model_pattern = 'claude-opus-4'"
                        ).fetchone()[0]
    rows = con.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0]
    con.close()
    assert price == 99.5, f"hand-set price was overwritten: {price}"
    assert rows == len(v2.SEED_PRICING), rows
    return "hand-set price 99.5 survived a second sync"


def t_rewritten_prefix_forces_full_read():
    reset()
    files = fixture()
    write_part(files, 0)
    sync()
    key = f"{SLUG}/sess-a.jsonl"
    n = len(files[key])
    part0 = files[key][: n // 3]
    idx = next(i for i, ln in enumerate(part0) if '"usage"' in ln)
    uid = json.loads(part0[idx])["uuid"]
    part0[idx] = part0[idx].replace("proj-x", "proj-REWRITTEN")
    write_file(v2.TRANSCRIPTS_DIR / key, part0 + files[key][n // 3:])   # rewrite, not append
    sync()
    con = sqlite3.connect(str(v2.DB_PATH))
    proj = con.execute("SELECT project FROM usage_events WHERE uuid = ?", (uid,)).fetchone()[0]
    con.close()
    assert proj == "C:/work/proj-REWRITTEN", f"rewritten line not re-read: {proj}"
    return "rewritten prefix detected and re-read from 0"


def t_migrates_usage_db_py_file():
    """A usage.db written by usage_db.py (no schema_version, window-function views) is migrated in
    place: usage_dedup is filled from usage_events and the views are replaced. The archive has not
    changed since, so the next sync reads nothing."""
    reset()
    files = fixture()
    for part in range(3):
        write_part(files, part)
    v2.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(v2.DB_PATH))
    con.executescript(live.SCHEMA)
    con.executemany("INSERT OR IGNORE INTO model_pricing VALUES (?,?,?,?,?,?)", live.SEED_PRICING)
    ref = sqlite3.connect(":memory:")
    ref.executescript(live.SCHEMA)
    for rel in files:
        path = v2.TRANSCRIPTS_DIR / rel
        rows = list(live.rows_from_file(path, rel))
        con.executemany(live.INSERT_SQL, rows)
        ref.executemany(live.INSERT_SQL, rows)
        st = path.stat()
        con.execute("INSERT INTO ingest_state VALUES (?,?,?,?,?)",
                    (str(Path(rel)), st.st_mtime_ns, st.st_size, len(rows), "2026-10-09T00:00:00+00:00"))
    con.commit()
    con.close()
    want = ref.execute("SELECT session_id, message_id, uuid, total_tokens FROM v_events_dedup "
                       "ORDER BY 1, 2").fetchall()
    rc, out = sync()
    assert rc == 0, rc
    assert v2.SCHEMA_RUNS == 1, f"migration ran {v2.SCHEMA_RUNS} times"
    assert "0 changed" in out, f"expected no re-read after migration: {out.strip()}"
    con = sqlite3.connect(str(v2.DB_PATH))
    got = con.execute("SELECT session_id, message_id, uuid, total_tokens FROM usage_dedup "
                      "ORDER BY 1, 2").fetchall()
    view_total = con.execute("SELECT SUM(total_tokens) FROM v_daily").fetchone()[0]
    dedup_total = con.execute("SELECT SUM(total_tokens) FROM usage_dedup").fetchone()[0]
    con.close()
    assert got == want, f"migrated usage_dedup differs: {len(got)} rows vs {len(want)}"
    assert view_total == dedup_total, (view_total, dedup_total)
    return f"{len(got)} messages migrated; no re-read"


TESTS = [
    t_dedup_matches_window_function,
    t_incremental_equals_full_rebuild,
    t_period_views_sum_to_total,
    t_schema_created_once,
    t_no_double_count_across_live_and_archive,
    t_stats_labels_both_totals,
    t_export_is_separate,
    t_sql_is_read_only,
    t_pricing_seed_once,
    t_rewritten_prefix_forces_full_read,
    t_migrates_usage_db_py_file,
]


def main():
    failed = 0
    for fn in TESTS:
        name = fn.__name__[2:]
        try:
            detail = fn()
            print(f"PASS {name}" + (f" ({detail})" if detail else ""))
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 - report every failure with its type
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"{len(TESTS) - failed} passed, {failed} failed")
    shutil.rmtree(HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
