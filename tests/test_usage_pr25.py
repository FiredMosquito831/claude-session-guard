"""
Tests for PR-25 (scripts/usage_db_v2.py, schema 2): usage read at session close, a catch-up
for sessions whose close never ran, and the per-session rollup table usage_rollup.

Run from the worktree root:  python -I tests/test_usage_pr25.py
Every test works only under SCRATCH_ROOT, a fresh temporary folder created by this file.
The one exception is T-REAL, which READS real transcripts (copied with shutil.copy2 into
scratch first); nothing under the real folders is written. Each test prints PASS or FAIL.
"""
import contextlib
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

REAL_CLAUDE = Path.home() / ".claude"          # read-only source for T-REAL only
SCRATCH_ROOT = Path(tempfile.mkdtemp(prefix="pr25_test_"))
os.environ["SESSION_GUARD_HOME"] = str(SCRATCH_ROOT / "boot")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import usage_db_v2 as v2  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(str(SCRATCH_ROOT)), "test would touch real data"
assert str(v2.CLAUDE_DIR).startswith(str(SCRATCH_ROOT)), "test would touch real data"

# The planned SessionEnd order (PR-26): the usage step runs before the repair step.
# T-ORDER runs this order. Mutant M-10 reverses it.
SESSION_END_PLAN = ("usage", "repair")

EXPECTED_ROLLUP_COLS = ["session_id", "date", "model", "messages", "input_tokens", "output_tokens",
                        "cache_creation_tokens", "cache_read_tokens", "ephemeral_5m_tokens",
                        "ephemeral_1h_tokens", "total_tokens"]
COST_SQL_ROLLUP = """SELECT r.model, ROUND(SUM(COALESCE((
    SELECT (r.input_tokens * p.input_per_mtok + r.output_tokens * p.output_per_mtok
          + r.ephemeral_5m_tokens * p.cache_write_5m_per_mtok
          + r.ephemeral_1h_tokens * p.cache_write_1h_per_mtok
          + (r.cache_creation_tokens - r.ephemeral_5m_tokens - r.ephemeral_1h_tokens)
                                    * p.cache_write_5m_per_mtok
          + r.cache_read_tokens * p.cache_read_per_mtok) / 1000000.0
    FROM model_pricing p WHERE r.model LIKE p.model_pattern || '%'
    ORDER BY LENGTH(p.model_pattern) DESC LIMIT 1), 0.0)), 4)
    FROM usage_rollup r GROUP BY r.model ORDER BY r.model"""

SLUG = "C--work-pr25-test"
SID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SID_B = "bbbbbbbb-0000-4000-8000-00000000000b"
SID_C = "cccccccc-0000-4000-8000-00000000000c"     # never ends (F13)
SID_E = "eeeeeeee-0000-4000-8000-00000000000e"     # carried by a subagent of B (F5)
SID_D = "dddddddd-0000-4000-8000-00000000000d"     # T-OFFSET
SID_O = "00000000-0000-4000-8000-0000000000a0"     # T-ORDER
SID_U = "00000000-0000-4000-8000-000000000e01"     # T-UNTERM
MODEL_A = "claude-opus-4-1-20250805"
MODEL_B = "claude-sonnet-4-5-20250929"
MODEL_X = "mystery-local-model"                    # no price row
T0 = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ts(sec):
    return iso(T0 + timedelta(seconds=sec))


def ul(uid, sid, mid, sec, model, inp, out, cc=0, cr=0, e5=None, e1h=0):
    """One usage-bearing transcript line (LF terminated, bytes)."""
    obj = {"type": "assistant", "sessionId": sid, "requestId": "req_" + str(uid),
           "timestamp": ts(sec), "cwd": "C:/work/pr25", "version": "2.1.0", "gitBranch": "main",
           "message": {"id": mid, "model": model, "stop_reason": "end_turn",
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
                                 "cache_creation": {"ephemeral_5m_input_tokens":
                                                    cc // 2 if e5 is None else e5,
                                                    "ephemeral_1h_input_tokens": e1h},
                                 "service_tier": "standard"}}}
    if uid is not None:
        obj["uuid"] = uid
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def point_at(name):
    """Point every module path at a fresh scratch home. Asserts that nothing real is used."""
    home = SCRATCH_ROOT / name
    assert str(home).startswith(str(SCRATCH_ROOT)), "refusing to delete outside scratch"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    os.environ["SESSION_GUARD_HOME"] = str(home)
    v2.CLAUDE_DIR = home
    v2.ARCHIVE_DIR = home / "session-archive"
    v2.TRANSCRIPTS_DIR = v2.ARCHIVE_DIR / "transcripts"
    v2.PROJECTS_DIR = home / "projects"
    v2.REPORTS_DIR = v2.ARCHIVE_DIR / "reports"
    v2.DB_PATH = v2.ARCHIVE_DIR / "usage.db"
    v2.CATCHUP_SCHEDULE_FILE = v2.ARCHIVE_DIR / "usage-catchup-schedule.json"
    v2.SCHEMA_RUNS = 0
    for p in (v2.CLAUDE_DIR, v2.DB_PATH, v2.TRANSCRIPTS_DIR, v2.PROJECTS_DIR,
              v2.CATCHUP_SCHEDULE_FILE):
        assert str(p).startswith(str(home)), f"path outside the scratch home: {p}"
    return home


# ---------------------------------------------------------------- fixtures

def fixture():
    """Synthetic transcripts. Returns (files, forced_cuts). files = (tree, rel, content)."""
    a_main = b"".join([
        ul("a-1", SID_A, "m1", 0, MODEL_A, 10, 20, 5, 0),     # F1: one message on three lines
        ul("a-2", SID_A, "m1", 0, MODEL_A, 10, 20, 5, 0),
        ul("a-3", SID_A, "m1", 0, MODEL_A, 10, 20, 5, 0),
        ul("b-1", SID_A, "m2", 1, MODEL_A, 10, 20),           # F2: two totals, larger wins
        ul("b-2", SID_A, "m2", 1, MODEL_A, 40, 40),
        ul("c-1", SID_A, "m3", 2, MODEL_B, 30, 40),           # F3: tie on total, later ts wins
        ul("c-2", SID_A, "m3", 7, MODEL_B, 30, 40),
        ul("d-2", SID_A, "m4", 3, MODEL_B, 45, 45),           # F4: tie on total and ts
        ul("d-1", SID_A, "m4", 3, MODEL_B, 45, 45),           #     smallest uuid wins
        b'note "usage" is not json here\n',                   # F9
        ul(None, SID_A, "m5", 4, MODEL_X, 7, 7),              # F10: no uuid
    ])
    b_first = ul("e-1", SID_B, "m6", 10, MODEL_A, 11, 22)
    b_main_m = b_first + ul("e-2", SID_B, "m7", 11, MODEL_A, 13, 24)
    b_main_l = b_main_m + ul("e-3", SID_B, "m8", 12, MODEL_B, 5, 6)   # F12: live bigger
    s1 = b"".join([ul("s1-1", SID_B, "sm1", 20, MODEL_A, 1, 2),       # F5: parent's id
                   ul("s1-2", SID_B, "sm2", 21, MODEL_A, 3, 4)])
    s2 = ul("s2-1", SID_E, "sm3", 22, MODEL_B, 6, 7)                  # F5: another id
    w1 = ul("w-1", SID_B, "wm1", 23, MODEL_X, 8, 9)                   # F6: workflow
    ac = ul("ac-1", SID_C, "acm1", 24, MODEL_A, 100, 100)             # F7: acompact
    c_main = b"".join([ul("c9-1", SID_C, "cm1", 30, MODEL_B, 12, 13, 1, 1),
                       ul("c9-2", SID_C, "cm2", 31, MODEL_B, 14, 15)])
    main_a = Path(SLUG) / f"{SID_A}.jsonl"
    main_b = Path(SLUG) / f"{SID_B}.jsonl"
    main_c = Path(SLUG) / f"{SID_C}.jsonl"
    sub = Path(SLUG) / SID_B / "subagents"
    files = [
        ("live", main_a, a_main), ("mirror", main_a, a_main),
        ("live", main_b, b_main_l), ("mirror", main_b, b_main_m),
        ("live", sub / "agent-1.jsonl", s1), ("mirror", sub / "agent-1.jsonl", s1),
        ("live", sub / "agent-2.jsonl", s2),
        ("live", sub / "workflows" / "wf1" / "agent-3.jsonl", w1),
        ("live", sub / "agent-empty.jsonl", b""),                     # F8
        ("live", main_c, c_main),
        ("live", Path(SLUG) / SID_C / "subagents" / "agent-acompact-1.jsonl", ac),
    ]
    forced = {("mirror", str(main_b)): [len(b_first) + 7]}           # F11: cut inside a line
    return files, forced


def content_size(content):
    return len(content) if isinstance(content, bytes) else content[1]


def slice_of(content, a, b):
    return content[a:b] if isinstance(content, bytes) else (content[0], a, b)


def build_events(files, rng, forced=None):
    """Split every file into three chunks (two random cuts), then interleave them round-robin."""
    forced = forced or {}
    per_file = []
    for tree, rel, content in files:
        size = content_size(content)
        cuts = set(forced.get((tree, str(rel)), []))
        if size > 1:
            cuts.update(rng.randrange(1, size) for _ in range(2))
        bounds = [0] + sorted(c for c in cuts if 0 < c < size) + [size]
        per_file.append([(tree, rel, slice_of(content, bounds[i], bounds[i + 1]))
                         for i in range(len(bounds) - 1)])
    events = []
    for i in range(max(len(p) for p in per_file)):
        for chunks in per_file:
            if i < len(chunks):
                tree, rel, payload = chunks[i]
                events.append(("write", tree, rel, payload))
    return events


def last_write_index(events, sid):
    idx = -1
    for i, ev in enumerate(events):
        if ev[0] == "write" and sid in str(ev[2]):
            idx = i
    return idx


def add_ends(events, ends):
    """ends: (sid, main_rel). Each end goes right after the last write of that session."""
    order = sorted(ends, key=lambda e: -last_write_index(events, e[0]))
    for sid, rel in order:
        events.insert(last_write_index(events, sid) + 1, ("end", sid, rel))
    return events


def apply_write(ev):
    _, tree, rel, payload = ev
    base = v2.TRANSCRIPTS_DIR if tree == "mirror" else v2.PROJECTS_DIR
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, tuple):
        src, a, b = payload
        with open(src, "rb") as f:
            f.seek(a)
            data = f.read(b - a)
    else:
        data = payload
    with open(target, "ab") as f:
        f.write(data)


def run_flow(name, events, mode, catch=True):
    """mode A: ingest(False) after every write (per-turn). mode B: ingest_session at each end,
    then catch_up (unless catch=False). Returns the snapshot; the module stays on this home."""
    point_at(name)
    if mode == "B":                             # the one-time, detached migration comes first
        rc, out = quiet(v2.migrate)
        assert rc == 0, out
    for ev in events:
        if ev[0] == "write":
            apply_write(ev)
            if mode == "A":
                rc, out = quiet(v2.ingest, False)
                assert rc == 0, out
        elif mode == "B":
            rc, out = quiet(v2.ingest_session, v2.PROJECTS_DIR / ev[2], ev[1])
            assert rc == 0, f"ingest_session rc {rc}: {out}"
    if mode == "A":
        rc, out = quiet(v2.ingest, False)
        assert rc == 0, out
    elif catch:
        rc, out = quiet(v2.catch_up, bypass_cooldown=True)
        assert rc == 0, f"catch_up rc {rc}: {out}"
    return snap()


def snap():
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        s = {
            "usage_events": con.execute("SELECT * FROM usage_events ORDER BY uuid").fetchall(),
            "usage_dedup": con.execute(
                "SELECT * FROM usage_dedup ORDER BY session_id, message_id").fetchall(),
            "usage_rollup": con.execute(
                "SELECT * FROM usage_rollup ORDER BY session_id, date, model").fetchall(),
            "model_pricing": con.execute(
                "SELECT * FROM model_pricing ORDER BY model_pattern").fetchall(),
            "ingest_state": con.execute(
                "SELECT source_file, size, byte_offset, head_sha, tail_sha FROM ingest_state "
                "ORDER BY source_file").fetchall(),
            "per_model_rollup": con.execute(
                "SELECT model, SUM(messages), SUM(input_tokens), SUM(output_tokens), "
                "SUM(cache_creation_tokens), SUM(cache_read_tokens), SUM(total_tokens) "
                "FROM usage_rollup GROUP BY model ORDER BY model").fetchall(),
            "per_model_dedup": con.execute(
                "SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                "SUM(cache_creation_tokens), SUM(cache_read_tokens), SUM(total_tokens) "
                "FROM usage_dedup GROUP BY model ORDER BY model").fetchall(),
        }
        for name in v2.VIEW_NAMES:
            s["view:" + name] = sorted(con.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
    finally:
        con.close()
    rc, out = quiet(v2.cmd_export, v2.REPORTS_DIR)
    assert rc == 0, out
    for p in sorted(v2.REPORTS_DIR.glob("*.csv")):
        s["csv:" + p.name] = p.read_bytes()
    return s


def compared(key):
    """What the flows must agree on: the usage rows, the per-model totals and the export.
    ingest_state is NOT compared (decision 2). Its stale '|live' rows are offsets kept on purpose,
    and the two flows legitimately hold different stale rows. T-STALE checks they change no total."""
    return key in ("usage_events", "usage_dedup", "usage_rollup", "per_model_rollup",
                   "per_model_dedup") or key.startswith("csv:")


def diff_snaps(a, b):
    problems = []
    for key in sorted(set(a) | set(b)):
        if not compared(key):
            continue
        x, y = a.get(key), b.get(key)
        if x == y:
            continue
        if isinstance(x, list) and isinstance(y, list):
            n = min(len(x), len(y))
            first = next((i for i in range(n) if x[i] != y[i]), n)
            problems.append(f"{key}: {len(x)} vs {len(y)} rows, first difference at row {first}")
        else:
            problems.append(f"{key}: differs")
    return problems


def oracle(lines):
    """Expected totals from the raw lines alone (as test_usage_pr06.py): each uuid once, then
    the winner per (session_id, message_id) by total, then timestamp, then smallest uuid."""
    by_uuid = {}
    for raw in lines:
        if b'"usage"' not in raw:
            continue
        try:
            e = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict) or not e.get("uuid"):
            continue
        total = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                 "cache_creation_input_tokens",
                                                 "cache_read_input_tokens"))
        epoch = int(datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp())
        by_uuid.setdefault(e["uuid"], (e.get("sessionId") or "", msg.get("id") or "", total, epoch))
    winners = {}
    for uid, (sid, mid, total, epoch) in by_uuid.items():
        cand = (-total, -epoch, uid)
        if (sid, mid) not in winners or cand < winners[(sid, mid)][0]:
            winners[(sid, mid)] = (cand, total, uid)
    return {"raw_lines": len(by_uuid),
            "raw_total": sum(t for _, _, t, _ in by_uuid.values()),
            "dedup_total": sum(t for _, t, _ in winners.values()),
            "winners": {k: v[2] for k, v in winners.items()}}


def fixture_lines(files):
    lines = []
    for _, rel, content in files:
        if "acompact" in str(rel) or not isinstance(content, bytes):
            continue
        lines.extend(content.splitlines(keepends=True))
    return lines


def rollup_matches_group_by(con):
    want = con.execute(
        "SELECT session_id, date, model, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
        "SUM(cache_creation_tokens), SUM(cache_read_tokens), SUM(ephemeral_5m_tokens), "
        "SUM(ephemeral_1h_tokens), SUM(total_tokens) FROM usage_dedup "
        "GROUP BY session_id, date, model ORDER BY 1, 2, 3").fetchall()
    got = con.execute(
        "SELECT session_id, date, model, messages, input_tokens, output_tokens, "
        "cache_creation_tokens, cache_read_tokens, ephemeral_5m_tokens, ephemeral_1h_tokens, "
        "total_tokens FROM usage_rollup ORDER BY 1, 2, 3").fetchall()
    assert got == want, (f"usage_rollup differs from GROUP BY over usage_dedup: "
                         f"{len(got)} rows vs {len(want)}")


# ---------------------------------------------------------------- tests

def t_equiv():
    """Per-turn flow (A) and session-close flow (B) give the same usage rows, per-model totals and
    export, field by field (compared()). Also the oracle's uuids, totals and winners.
    ingest_state is left out on purpose (decision 2; see compared() and T-STALE)."""
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    snap_a = run_flow("equiv_a", events, "A")
    snap_b = run_flow("equiv_b", events, "B")
    problems = diff_snaps(snap_a, snap_b)
    assert not problems, "; ".join(problems)
    want = oracle(fixture_lines(files))
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        raw_lines = con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        dedup_total = con.execute("SELECT SUM(total_tokens) FROM usage_dedup").fetchone()[0]
        winners = {(s, m): u for s, m, u in
                   con.execute("SELECT session_id, message_id, uuid FROM usage_dedup")}
    finally:
        con.close()
    assert raw_lines == want["raw_lines"], f"events {raw_lines} != oracle {want['raw_lines']}"
    assert dedup_total == want["dedup_total"], f"dedup {dedup_total} != oracle {want['dedup_total']}"
    bad = [k for k in want["winners"] if winners.get(k) != want["winners"][k]]
    assert not bad, f"winner uuid differs from oracle for {len(bad)} messages, e.g. {bad[:2]}"
    return (f"{len(snap_b['usage_events'])} events, {len(snap_b['usage_dedup'])} messages, "
            f"{len(snap_b['usage_rollup'])} rollup rows, {len(snap_b['csv:models.csv'])} csv bytes "
            f"in models.csv; A and B equal field by field, oracle equal")


def t_rollup():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    run_flow("rollup", events, "B")
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        rollup_matches_group_by(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(usage_rollup)")]
        assert cols == EXPECTED_ROLLUP_COLS, f"usage_rollup columns {cols}"
        v = {r[0]: r[1:] for r in con.execute(
            "SELECT model, events, input_tokens, output_tokens, cache_creation_tokens, "
            "cache_read_tokens, total_tokens FROM v_by_model")}
        roll = {r[0]: r[1:] for r in con.execute(
            "SELECT model, SUM(messages), SUM(input_tokens), SUM(output_tokens), "
            "SUM(cache_creation_tokens), SUM(cache_read_tokens), SUM(total_tokens) "
            "FROM usage_rollup GROUP BY model")}
        assert v.keys() == roll.keys(), f"models differ: {set(v) ^ set(roll)}"
        for m in v:
            assert tuple(v[m]) == tuple(roll[m]), f"{m}: view {v[m]} != rollup {roll[m]}"
        cost_view = dict(con.execute("SELECT model, cost_usd FROM v_by_model").fetchall())
        cost_roll = dict(con.execute(COST_SQL_ROLLUP).fetchall())
        assert cost_view == cost_roll, f"cost differs after ROUND 4: {cost_view} vs {cost_roll}"
    finally:
        con.close()
    return f"{len(v)} models: tokens and cost equal the views; rollup equals GROUP BY"


def t_subset():
    files, _ = fixture()
    run_flow("subset", build_events(files, random.Random(25)), "A")
    all_keys = [k for k, _ in v2.sources()]

    def belongs(key, rel, sub):
        base = Path(key.split("|")[0])
        return base == rel or sub in base.parents

    total = 0
    for sid, rel in [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                     (SID_B, Path(SLUG) / f"{SID_B}.jsonl"),
                     (SID_C, Path(SLUG) / f"{SID_C}.jsonl")]:
        sub = rel.parent / sid / "subagents"
        want = sorted(k for k in all_keys if belongs(k, rel, sub))
        real_sources = v2.sources

        def no_walk():
            raise AssertionError("session step called the full tree walk sources()")
        v2.sources = no_walk
        try:
            got = sorted(k for k, _ in v2.session_files(v2.PROJECTS_DIR / rel, sid))
        finally:
            v2.sources = real_sources
        assert got == want, f"{sid}: session set {got} != sources() subset {want}"
        total += len(got)
    assert v2.session_files(SCRATCH_ROOT / "outside.jsonl", SID_A) is None, "out of tree accepted"
    assert v2.session_files(v2.PROJECTS_DIR / SLUG / "missing.jsonl", "missing") is None, \
        "missing transcript accepted"
    return f"{total} session files equal the sources() subsets; no full walk; out-of-tree skipped"


def t_live():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    snap_nc = run_flow("live", events, "B", catch=False)
    key = str(Path(SLUG) / f"{SID_B}.jsonl") + "|live"
    assert any(r[0] == key for r in snap_nc["ingest_state"]), f"no state row for {key}"
    assert any(r[0] == "e-3" for r in snap_nc["usage_events"]), \
        "the session step did not read the bigger live copy (e-3 missing before catch-up)"
    return f"{key} read at session close, before any catch-up"


def t_acompact():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    snap_a = run_flow("acompact_a", events, "A")
    assert (v2.PROJECTS_DIR / SLUG / SID_C / "subagents" / "agent-acompact-1.jsonl").is_file()
    snap_b = run_flow("acompact_b", events, "B")
    for name, s in (("A", snap_a), ("B", snap_b)):
        assert not any(r[0] == "ac-1" for r in s["usage_events"]), f"flow {name} read acompact"
        assert not any("acompact" in r[0] for r in s["ingest_state"]), f"flow {name} state"
    return "acompact file skipped by both flows; no state row"


def t_catchup():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    snap_nc = run_flow("catch_none", events, "B", catch=False)
    missing = [u for u in ("c9-1", "c9-2") if any(r[0] == u for r in snap_nc["usage_events"])]
    assert not missing, f"session C rows present before catch-up: {missing}"
    rc, out = quiet(v2.catch_up, bypass_cooldown=True)
    assert rc == 0, out
    after = snap()
    assert all(any(r[0] == u for r in after["usage_events"]) for u in ("c9-1", "c9-2")), \
        "catch-up did not read the session that never ended"
    snap_a = run_flow("catch_a", events, "A")
    problems = diff_snaps(snap_a, after)
    assert not problems, "; ".join(problems)
    return "session C (never ended) read by catch-up; result equals the per-turn flow"


def t_offset():
    home = point_at("offset")
    assert quiet(v2.migrate)[0] == 0          # the one-time migration, as in production
    path = v2.PROJECTS_DIR / SLUG / f"{SID_D}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(ul(f"d-{i}", SID_D, f"dm{i}", i, MODEL_A, 2 + i, 3) for i in range(3)))
    rc, out = quiet(v2.ingest_session, path, SID_D)
    assert rc == 0, out
    size1 = path.stat().st_size
    with open(path, "ab") as f:
        f.write(b"".join(ul(f"d-{i}", SID_D, f"dm{i}", i, MODEL_A, 2 + i, 3) for i in (7, 8)))
    size2 = path.stat().st_size
    calls = []
    real_scan = v2.scan

    def spy(p, start, end, rel):
        calls.append((start, end))
        return real_scan(p, start, end, rel)
    v2.scan = spy
    try:
        rc, out = quiet(v2.ingest_session, path, SID_D)
    finally:
        v2.scan = real_scan
    assert rc == 0, out
    read = sum(e - s for s, e in calls)
    assert calls and calls[0][0] == size1, f"second session step started at {calls[:1]}, not {size1}"
    assert read == size2 - size1, f"second session step read {read} bytes, expected {size2 - size1}"
    assert str(home).startswith(str(SCRATCH_ROOT))
    return f"second session step read {read} of {size2} bytes (from offset {size1})"


def t_price():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    run_flow("price", events, "B")
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(usage_rollup)")]
        assert cols == EXPECTED_ROLLUP_COLS, f"rollup stores a cost or other column: {cols}"
        before = dict(con.execute("SELECT model, cost_usd FROM v_by_model").fetchall())
        con.execute("UPDATE model_pricing SET input_per_mtok = input_per_mtok * 2 "
                    "WHERE model_pattern = 'claude-opus-4'")
        con.commit()
        after = dict(con.execute("SELECT model, cost_usd FROM v_by_model").fetchall())
        derived = dict(con.execute(COST_SQL_ROLLUP).fetchall())
    finally:
        con.close()
    assert after[MODEL_A] != before[MODEL_A], "price change did not reach the view"
    assert after == derived, f"view {after} != cost from the rollup tokens {derived}"
    return f"{MODEL_A}: cost {before[MODEL_A]} -> {after[MODEL_A]}; equals rollup tokens x price"


def t_build():
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    before = run_flow("build_inc", events, "A")
    rc, out = quiet(v2.ingest, True)
    assert rc == 0, out
    after = snap()
    problems = diff_snaps(before, after)
    assert not problems, "build differs from incremental: " + "; ".join(problems)
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        rollup_matches_group_by(con)
    finally:
        con.close()
    return "full build equals the incremental result, rollup included"


def t_nomigrate():
    home = point_at("v1file")
    path = v2.PROJECTS_DIR / SLUG / f"{SID_A}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ul("v-1", SID_A, "vm1", 0, MODEL_A, 1, 2))
    real_version = v2.SCHEMA_VERSION
    v2.SCHEMA_VERSION = 1                      # build a schema-1 file (no usage_rollup)
    try:
        rc, out = quiet(v2.ingest, False)
    finally:
        v2.SCHEMA_VERSION = real_version
    assert rc == 0, out
    con = sqlite3.connect(str(v2.DB_PATH))
    con.execute("DROP TABLE IF EXISTS usage_rollup")
    con.execute("DELETE FROM schema_version")
    con.execute("INSERT INTO schema_version (version) VALUES (1)")
    con.commit()
    con.close()
    v2.SCHEMA_RUNS = 0

    def schema_state():
        c = sqlite3.connect(str(v2.DB_PATH))
        try:
            return (c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
                    c.execute("SELECT version FROM schema_version").fetchall())
        finally:
            c.close()
    before = schema_state()
    rc, out = quiet(v2.ingest_session, path, SID_A)
    assert rc == 1 and "needs migrate" in out, f"session step on a v1 file: rc {rc} {out!r}"
    rc, out = quiet(v2.catch_up, True)
    assert rc == 1 and "needs migrate" in out, f"catch-up on a v1 file: rc {rc} {out!r}"
    assert schema_state() == before, "a session step changed the schema of a v1 file"
    assert v2.SCHEMA_RUNS == 0, f"schema ran {v2.SCHEMA_RUNS} times on the new path"
    rc, out = quiet(v2.migrate)
    assert rc == 0, out
    assert v2.SCHEMA_RUNS == 1, f"migrate ran the schema {v2.SCHEMA_RUNS} times"
    assert schema_state()[1] == [(2,)], "migrate did not write schema version 2"
    rc, out = quiet(v2.ingest_session, path, SID_A)
    assert rc == 0, out
    point_at("nodb")
    nodb_path = v2.PROJECTS_DIR / SLUG / f"{SID_A}.jsonl"
    nodb_path.parent.mkdir(parents=True, exist_ok=True)
    nodb_path.write_bytes(ul("n-1", SID_A, "nm1", 0, MODEL_A, 1, 2))
    rc, out = quiet(v2.ingest_session, nodb_path, SID_A)
    assert rc == 1 and not v2.DB_PATH.exists(), f"the new path created a database: rc {rc} {out!r}"
    assert str(home).startswith(str(SCRATCH_ROOT))
    return "v1 file: session and catch-up refused, schema unchanged, migrate once, then works"


def t_cooldown():
    point_at("cooldown")
    path = v2.PROJECTS_DIR / SLUG / f"{SID_A}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ul("k-1", SID_A, "km1", 0, MODEL_A, 1, 2))
    quiet(v2.ingest, False)
    now = [1_800_000_000.0]
    real_clock = v2._epoch_now
    v2._epoch_now = lambda: now[0]

    def runs():
        return gk.load_json(v2.CATCHUP_SCHEDULE_FILE, {}).get("runs", [])
    try:
        T = now[0]
        rc, out = quiet(v2.catch_up)
        assert rc == 0 and runs() == [T], f"first run: rc {rc} runs {runs()} {out!r}"
        now[0] = T + 3600
        rc, out = quiet(v2.catch_up)
        assert runs() == [T], f"ran inside the 6 h cooldown: {runs()} {out!r}"
        now[0] = T + 6 * 3600
        rc, out = quiet(v2.catch_up)
        assert runs() == [T, T + 6 * 3600], f"not allowed at 6 h: {runs()} {out!r}"
        now[0] = T + 100 * 3600
        gk.atomic_write_json(v2.CATCHUP_SCHEDULE_FILE,
                             {"runs": [now[0] - h * 3600 for h in (23, 22, 21, 20)]})
        before = runs()
        rc, out = quiet(v2.catch_up)
        assert runs() == before and "24 hours" in out, f"4 runs in 24 h not capped: {out!r}"
    finally:
        v2._epoch_now = real_clock
    return "6 h cooldown and 4-per-24 h cap hold on a fake clock"


def t_order():
    l1 = ul("o-1", SID_O, "om1", 0, MODEL_A, 1, 2)
    l2 = ul("o-2", SID_O, "om2", 1, MODEL_A, 3, 4)

    def scenario(name, order):
        point_at(name)
        assert quiet(v2.migrate)[0] == 0
        mirror = v2.TRANSCRIPTS_DIR / SLUG / f"{SID_O}.jsonl"
        live = v2.PROJECTS_DIR / SLUG / f"{SID_O}.jsonl"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        live.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_bytes(l1)                  # the mirror is stale: it lacks the second line
        live.write_bytes(l1 + l2)
        for step in order:
            if step == "usage":
                rc, out = quiet(v2.ingest_session, live, SID_O)
                assert rc == 0, out
            else:
                live.write_bytes(l1)            # repair removes the second line from the live file
        con = sqlite3.connect(str(v2.DB_PATH))
        try:
            return con.execute("SELECT COUNT(*) FROM usage_events WHERE uuid='o-2'").fetchone()[0] == 1
        finally:
            con.close()
    assert scenario("order_planned", SESSION_END_PLAN), \
        f"planned order {SESSION_END_PLAN} lost a removed usage line"
    assert not scenario("order_reversed", ("repair", "usage")), \
        "reversed order kept the line; the rule would not matter"
    return f"planned order {SESSION_END_PLAN} keeps a line that repair removes; reversed loses it"


def t_unterm():
    point_at("unterm")
    assert quiet(v2.migrate)[0] == 0
    path = v2.PROJECTS_DIR / SLUG / f"{SID_U}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    l1 = ul("u-1", SID_U, "um1", 0, MODEL_A, 1, 2)
    l2 = ul("u-2", SID_U, "um2", 1, MODEL_A, 3, 4)
    path.write_bytes(l1 + l2[:20])
    rc, out = quiet(v2.ingest_session, path, SID_U)
    assert rc == 0, out
    key = str(Path(SLUG) / f"{SID_U}.jsonl")
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        off = con.execute("SELECT byte_offset FROM ingest_state WHERE source_file=?",
                          (key,)).fetchone()[0]
        assert off == len(l1), f"offset {off} passed an unterminated line (expected {len(l1)})"
        assert con.execute("SELECT COUNT(*) FROM usage_events WHERE uuid='u-2'").fetchone()[0] == 0
    finally:
        con.close()
    with open(path, "ab") as f:
        f.write(l2[20:])
    rc, out = quiet(v2.ingest_session, path, SID_U)
    assert rc == 0, out
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        assert con.execute("SELECT COUNT(*) FROM usage_events WHERE uuid='u-2'").fetchone()[0] == 1
        off = con.execute("SELECT byte_offset FROM ingest_state WHERE source_file=?",
                          (key,)).fetchone()[0]
        assert off == len(l1 + l2)
    finally:
        con.close()
    return "unterminated line not consumed; completed by a later append and counted once"


def t_real():
    src_mirror = REAL_CLAUDE / "session-archive" / "transcripts"
    src_live = REAL_CLAUDE / "projects"
    assert src_mirror.is_dir(), "real transcript folder not found"
    pool = sorted((p.relative_to(src_mirror) for p in src_mirror.rglob("*.jsonl")
                   if p.is_file()), key=str)
    size_of = {p: (src_mirror / p).stat().st_size for p in pool}
    rng = random.Random(25)
    small = [p for p in pool if size_of[p] < 50 * 1024 * 1024]
    largest = max(small, key=lambda p: size_of[p])
    subs = [p for p in pool if "subagents" in p.parts]
    assert len(subs) >= 2, "fewer than two subagent transcripts"
    chosen = set(rng.sample(pool, 14)) | {largest} | set(rng.sample(subs, 3))
    while len(chosen) < 20:
        chosen.add(rng.choice(pool))
    chosen = sorted(chosen, key=str)
    assert sum(1 for p in chosen if "subagents" in p.parts) >= 2
    stage = SCRATCH_ROOT / "real_stage"
    shutil.rmtree(stage, ignore_errors=True)
    files = []
    total_bytes = 0
    for rel in chosen:
        for tree, base in (("live", src_live), ("mirror", src_mirror)):
            src = base / rel
            if not src.is_file():
                continue
            dst = stage / tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            size = dst.stat().st_size
            total_bytes += size
            files.append((tree, rel, (dst, size)))
    ends = [(p.stem, p) for p in chosen if len(p.parts) == 2]
    events = add_ends(build_events(files, random.Random(25)), ends)
    snap_a = run_flow("real_a", events, "A")
    snap_b = run_flow("real_b", events, "B")
    problems = diff_snaps(snap_a, snap_b)
    assert not problems, "; ".join(problems)
    return (f"{len(chosen)} transcripts, {len(files)} files, {total_bytes:,} bytes: "
            f"per-turn and session-close flows equal, {len(snap_b['usage_events']):,} events")


def usage_rows():
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        return con.execute("SELECT * FROM usage_events ORDER BY uuid").fetchall()
    finally:
        con.close()


def t_source():
    """Decision 1: the same transcript gives the same source_file whichever copy is read first.
    Home 1 reads the mirror first (equal size, so only the mirror is read), then the live copy grows.
    Home 2 reads the live copy first (the mirror does not exist yet), then the mirror appears.
    In both, source_file names the live file, and every usage_events column agrees."""
    base_lines = ul("g-1", SID_A, "gm1", 0, MODEL_A, 1, 2) + ul("g-2", SID_A, "gm2", 1, MODEL_A, 3, 4)
    extra = ul("g-3", SID_A, "gm3", 2, MODEL_B, 5, 6)
    rel = Path(SLUG) / f"{SID_A}.jsonl"
    point_at("src_mirror_first")
    assert quiet(v2.migrate)[0] == 0
    live = v2.PROJECTS_DIR / rel
    mirror = v2.TRANSCRIPTS_DIR / rel
    live.parent.mkdir(parents=True, exist_ok=True)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(base_lines)
    mirror.write_bytes(base_lines)
    assert quiet(v2.ingest, False)[0] == 0          # equal size: the mirror is read
    with open(live, "ab") as f:
        f.write(extra)
    assert quiet(v2.ingest, False)[0] == 0          # live is now bigger: the live copy is read
    rows1 = usage_rows()
    point_at("src_live_first")
    assert quiet(v2.migrate)[0] == 0
    live = v2.PROJECTS_DIR / rel
    mirror = v2.TRANSCRIPTS_DIR / rel
    live.parent.mkdir(parents=True, exist_ok=True)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(base_lines + extra)
    assert quiet(v2.ingest, False)[0] == 0          # the live copy alone is read first
    mirror.write_bytes(base_lines)
    assert quiet(v2.ingest, False)[0] == 0          # then the mirror copy is read
    rows2 = usage_rows()
    assert rows1 == rows2, "usage_events differ by the order in which the copies were read"
    want = f"{rel}|live"
    bad = [r[0] for r in rows2 if r[-1] != want]
    assert len(rows2) == 3 and not bad, f"source_file is not the live file for {bad or rows2}"
    return f"3 rows, every source_file = {want}, both read orders identical in all columns"


def t_stale():
    """Decision 2: stale ingest_state rows are kept (nothing is deleted). They are offsets, not
    usage. Adding stale '|live' rows for copies that do not exist must change no usage row or total,
    and must not be removed by a later run."""
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    before = run_flow("stale", events, "B")
    stale_a = str(Path(SLUG) / f"{SID_A}.jsonl") + "|live"
    stale_ghost = "ghost-session.jsonl|live"
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        for src, off in ((stale_a, 0), (stale_ghost, 17)):
            con.execute("INSERT INTO ingest_state (source_file, mtime_ns, size, rows_seen, "
                        "ingested_at, byte_offset, head_sha, tail_sha) VALUES (?,?,?,?,?,?,?,?)",
                        (src, 0, 10, 0, "", off, None, None))
        con.commit()
    finally:
        con.close()
    rc, out = quiet(v2.ingest, False)
    assert rc == 0, out
    rc, out = quiet(v2.catch_up, bypass_cooldown=True)
    assert rc == 0, out
    after = snap()
    problems = diff_snaps(before, after)
    assert not problems, "stale state rows changed usage: " + "; ".join(problems)
    keys = {r[0] for r in after["ingest_state"]}
    assert stale_ghost in keys and stale_a in keys, "a stale state row was deleted"
    return "2 stale |live rows kept; usage rows, per-model totals and export unchanged"


def t_export():
    """Decision 1 check: the CSV export does not depend on source_file. Every CSV view must avoid
    source_file, and rewriting every source_file in a copy must leave the export bytes unchanged."""
    files, forced = fixture()
    events = add_ends(build_events(files, random.Random(25), forced),
                      [(SID_A, Path(SLUG) / f"{SID_A}.jsonl"),
                       (SID_B, Path(SLUG) / f"{SID_B}.jsonl")])
    before = run_flow("export", events, "B")
    views = dict(v2.VIEWS)
    for view, _ in v2.CSV_EXPORTS:
        assert "source_file" not in views[view], f"{view} reads source_file"
    con = sqlite3.connect(str(v2.DB_PATH))
    try:
        con.execute("UPDATE usage_events SET source_file = 'changed|live'")
        con.execute("UPDATE usage_dedup SET source_file = 'changed|live'")
        con.commit()
    finally:
        con.close()
    rc, out = quiet(v2.cmd_export, v2.REPORTS_DIR)
    assert rc == 0, out
    after = {"csv:" + p.name: p.read_bytes() for p in v2.REPORTS_DIR.glob("*.csv")}
    want = {k for k in before if k.startswith("csv:")}
    assert set(after) == want and want, f"export files differ: {sorted(set(after) ^ want)}"
    diffs = [k for k in after if after[k] != before[k]]
    assert not diffs, f"export changed when source_file changed: {diffs}"
    return f"{len(after)} CSV files byte-identical after rewriting every source_file"


TESTS = [t_equiv, t_rollup, t_subset, t_live, t_acompact, t_catchup, t_offset, t_price,
         t_build, t_nomigrate, t_cooldown, t_order, t_unterm, t_source, t_stale, t_export,
         t_real]


def main():
    failed = 0
    try:
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
    finally:
        shutil.rmtree(SCRATCH_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
