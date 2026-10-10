"""
Tests for PR-27 (scripts/usage_db_v2.py): batched writes. Only the number of transactions may
change. The stored rows, the ingest offsets and the rollups must not.

Run from the worktree root:  python -I tests/test_usage_pr27.py
Each test prints PASS, FAIL or SKIP. Everything is written under SCRATCH_ROOT, a fresh temporary
folder created by this file. The only read outside it is T-REAL, which copies 50 real transcripts
into scratch first and never writes to the real folder.

Environment (all optional):
  PR27_NEW_MODULE   the module under test (default: scripts/usage_db_v2.py). Point it at a copy of
                    main's module to see the commit and crash tests fail (the red run).
  PR27_REF          git ref of the reference module (default: main), read with git show. Its
                    rows are the ones the new module must reproduce.
  PR27_REF_MODULE   a file to use as the reference instead of PR27_REF.
  PR27_SOURCE_HOME  the real Claude folder for T-REAL, read only (default: ~/.claude).
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = REPO / "scripts"
SCRATCH_ROOT = Path(tempfile.mkdtemp(prefix="pr27_test_"))
os.environ["SESSION_GUARD_HOME"] = str(SCRATCH_ROOT / "boot")   # before any import of guardkit
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402

assert str(gk.CLAUDE_DIR).startswith(str(SCRATCH_ROOT)), "test would touch real data"

REAL_SOURCE = Path(os.environ.get("PR27_SOURCE_HOME") or (Path.home() / ".claude"))
REF_NAME = os.environ.get("PR27_REF", "main")
BATCH_TEST = 7                 # small batch, so that every run spans several batches
PRODUCTION_BATCH = 500         # the value the commit bound is stated for
MODEL_A = "claude-opus-4-1-20250805"
MODEL_B = "claude-sonnet-4-5-20250929"
MODEL_X = "mystery-local-model"
SLUG = "C--work-pr27-test"
SID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SID_B = "bbbbbbbb-0000-4000-8000-00000000000b"
SID_C = "cccccccc-0000-4000-8000-00000000000c"
SID_D = "dddddddd-0000-4000-8000-00000000000d"
SID_H = "99999999-0000-4000-8000-000000000099"
SID_K = "77777777-0000-4000-8000-000000000077"
SID_N = "eeeeeeee-0000-4000-8000-00000000000e"
T0 = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
FIXED_NS = 1_790_000_000 * 10 ** 9       # every file gets a fixed mtime, so two homes agree
PROJECTS, MIRROR = "projects", "transcripts"
COLUMNS_STATE = "source_file, mtime_ns, size, rows_seen, byte_offset, head_sha, tail_sha"
# What digest() reads. ingested_at is left out: it is the wall clock, not stored usage.
QUERIES = [
    "SELECT * FROM usage_events ORDER BY uuid",
    "SELECT * FROM usage_dedup ORDER BY session_id, message_id",
    "SELECT * FROM usage_rollup ORDER BY session_id, date, model",
    f"SELECT {COLUMNS_STATE} FROM ingest_state ORDER BY source_file",
    "SELECT * FROM model_pricing ORDER BY model_pattern",
]
TABLE_QUERIES = {
    "usage_events": QUERIES[0],
    "usage_dedup": QUERIES[1],
    "usage_rollup": QUERIES[2],
    "ingest_state": QUERIES[3],
    "model_pricing": QUERIES[4],
}
NEW = None
REF = None


class InjectedCrash(Exception):
    """Raised inside a run to imitate an error in the middle of a batch."""


# ---------------------------------------------------------------- modules and homes

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_module_path():
    p = os.environ.get("PR27_NEW_MODULE")
    return Path(p) if p else SCRIPTS / "usage_db_v2.py"


def reference_module_path():
    p = os.environ.get("PR27_REF_MODULE")
    if p:
        return Path(p)
    out = SCRATCH_ROOT / "reference" / "usage_db_reference.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = subprocess.run(["git", "-C", str(REPO), "show", f"{REF_NAME}:scripts/usage_db_v2.py"],
                          capture_output=True, check=True).stdout
    out.write_bytes(data)
    return out


def point(mod, home: Path):
    """Point one module instance at its own home. Every path it uses is under home."""
    assert str(home).startswith(str(SCRATCH_ROOT)), f"refusing a home outside scratch: {home}"
    mod.CLAUDE_DIR = home
    mod.ARCHIVE_DIR = home / "session-archive"
    mod.TRANSCRIPTS_DIR = mod.ARCHIVE_DIR / MIRROR
    mod.PROJECTS_DIR = home / PROJECTS
    mod.REPORTS_DIR = mod.ARCHIVE_DIR / "reports"
    mod.DB_PATH = mod.ARCHIVE_DIR / "usage.db"
    mod.CATCHUP_SCHEDULE_FILE = mod.ARCHIVE_DIR / "usage-catchup-schedule.json"
    mod.SCHEMA_RUNS = 0
    return mod


def tree_dir(home: Path, tree: str) -> Path:
    return home / PROJECTS if tree == PROJECTS else home / "session-archive" / MIRROR


def materialize(home: Path, files: dict):
    """Write {(tree, rel): bytes} under home. Each file gets a fixed mtime (its index, in ns)."""
    assert str(home).startswith(str(SCRATCH_ROOT))
    for i, ((tree, rel), data) in enumerate(sorted(files.items())):
        p = tree_dir(home, tree) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, ns=(FIXED_NS + i, FIXED_NS + i))


def write(path: Path, data: bytes, k: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, ns=(FIXED_NS + k * 10 ** 9, FIXED_NS + k * 10 ** 9))


def append(path: Path, data: bytes, k: int):
    with open(path, "ab") as f:
        f.write(data)
    os.utime(path, ns=(FIXED_NS + k * 10 ** 9, FIXED_NS + k * 10 ** 9))


@contextlib.contextmanager
def batch_size(mod, n):
    """Set the batch size of a module (BATCH_FILES in the new one, COMMIT_EVERY in main's)."""
    saved = {k: getattr(mod, k) for k in ("BATCH_FILES", "COMMIT_EVERY") if hasattr(mod, k)}
    for k in saved:
        setattr(mod, k, n)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)


def quiet(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


# ---------------------------------------------------------------- transcript content

def ts(sec):
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def usage_line(uid, sid, mid, sec, model, inp, out, cc=0, cr=0, sidechain=False, extra=None):
    """One usage-bearing transcript line, LF terminated, as UTF-8 bytes."""
    obj = {"type": "assistant", "sessionId": sid, "requestId": "req_" + str(uid),
           "timestamp": ts(sec), "cwd": "C:/work/pr27", "version": "2.1.0",
           "gitBranch": "main", "isSidechain": sidechain,
           "message": {"id": mid, "model": model, "stop_reason": "end_turn",
                       "usage": {"input_tokens": inp, "output_tokens": out,
                                 "cache_creation_input_tokens": cc,
                                 "cache_read_input_tokens": cr,
                                 "cache_creation": {"ephemeral_5m_input_tokens": cc // 2,
                                                    "ephemeral_1h_input_tokens": 0},
                                 "service_tier": "standard"}}}
    if uid is not None:
        obj["uuid"] = uid
    if extra:
        obj.update(extra)
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sid_g(i):
    return f"{i:08x}-0000-4000-8000-{i:012x}"


def base_files() -> dict:
    """The synthetic corpus: split messages, duplicate message ids, a subagent (one in each tree),
    an empty file, a live copy bigger than its mirror copy, a mirror-only session, a file that
    the sync must skip (acompact), non-ASCII text, and 40 small sessions for several batches."""
    f = {}
    f[(PROJECTS, f"{SLUG}/{SID_A}.jsonl")] = b"".join([
        usage_line("a-1", SID_A, "m1", 0, MODEL_A, 10, 20, cc=5),      # one message, 3 lines
        usage_line("a-2", SID_A, "m1", 0, MODEL_A, 10, 20, cc=5),
        usage_line("a-3", SID_A, "m1", 0, MODEL_A, 10, 20, cc=5),
        usage_line("b-1", SID_A, "m2", 1, MODEL_A, 10, 20),            # two totals: larger wins
        usage_line("b-2", SID_A, "m2", 1, MODEL_A, 40, 40),
        usage_line("c-1", SID_A, "m3", 2, MODEL_B, 30, 40),            # tie on total: later ts
        usage_line("c-2", SID_A, "m3", 7, MODEL_B, 30, 40),
        usage_line("d-2", SID_A, "m4", 3, MODEL_B, 45, 45),            # tie on total and ts:
        usage_line("d-1", SID_A, "m4", 3, MODEL_B, 45, 45),            # smallest uuid wins
        b'note "usage" is not json\n',
        usage_line(None, SID_A, "m5", 4, MODEL_X, 7, 7),               # no uuid: not counted
        b"\n",
        b'{"type":"user","uuid":"u-9","message":{"content":"usage"}}\n',
    ])
    f[(PROJECTS, f"{SLUG}/{SID_B}.jsonl")] = b"".join(
        usage_line(f"bb-{i}", SID_B, f"n{i % 4}", 10 + i, MODEL_A, 5 + i, 9, cc=3, cr=2)
        for i in range(6))
    f[(PROJECTS, f"{SLUG}/{SID_B}/subagents/agent-1.jsonl")] = b"".join([
        usage_line("s-1", SID_B, "n1", 12, MODEL_A, 6, 9, cc=3, cr=2),   # same message as bb-1
        usage_line("s-2", SID_B, "sub1", 13, MODEL_B, 11, 12, sidechain=True)])
    f[(MIRROR, f"{SLUG}/{SID_B}/subagents/agent-2.jsonl")] = usage_line(
        "s-4", SID_B, "sub2", 14, MODEL_B, 2, 3, sidechain=True)
    f[(PROJECTS, f"{SLUG}/empty.jsonl")] = b""
    f[(PROJECTS, f"{SLUG}/x-acompact.jsonl")] = usage_line("k-1", SID_K, "k1", 1, MODEL_A, 1, 1)
    d_live = b"".join(usage_line(f"dl-{i}", SID_D, f"d{i}", i, MODEL_A, 4 + i, 5) for i in range(4))
    f[(PROJECTS, f"{SLUG}/{SID_D}.jsonl")] = d_live
    f[(MIRROR, f"{SLUG}/{SID_D}.jsonl")] = b"".join(
        usage_line(f"dl-{i}", SID_D, f"d{i}", i, MODEL_A, 4 + i, 5) for i in range(2))
    f[(MIRROR, f"{SLUG}/{SID_C}.jsonl")] = b"".join(
        usage_line(f"c{i}", SID_C, f"cm{i % 2}", 30 + i, MODEL_B, 8 + i, 2) for i in range(3))
    f[(MIRROR, f"{SLUG}/{SID_H}.jsonl")] = usage_line(
        "h-1", SID_H, "h1", 40, MODEL_A, 3, 4, extra={"note": "caf\u00e9 \u2014 ok"})
    for i in range(40):
        lines = [usage_line(f"g{i}-1", sid_g(i), f"g{i % 5}", i, MODEL_B, 3 + i, 4)]
        if i % 3 == 0:
            lines.append(usage_line(f"g{i}-2", sid_g(i), f"g{i % 5}", i, MODEL_B, 3 + i, 9))
        f[(PROJECTS, f"{SLUG}/{sid_g(i)}.jsonl")] = b"".join(lines)
    return f


# Mutations, applied identically to every home. k is the step number (it sets the mtime).

def mutate_grow(home: Path, k: int):
    append(tree_dir(home, PROJECTS) / SLUG / f"{SID_B}.jsonl", b"".join([
        usage_line("bb-6", SID_B, "n2", 20, MODEL_A, 9, 9),
        usage_line("bb-7", SID_B, "n9", 21, MODEL_B, 1, 2)]), k)
    append(tree_dir(home, PROJECTS) / SLUG / SID_B / "subagents" / "agent-1.jsonl",
           usage_line("s-3", SID_B, "n1", 22, MODEL_A, 30, 40), k)


def mutate_new(home: Path, k: int):
    write(tree_dir(home, PROJECTS) / SLUG / f"{SID_N}.jsonl", b"".join(
        usage_line(f"nw-{i}", SID_N, f"w{i % 2}", 50 + i, MODEL_A, 2 + i, 3) for i in range(3)), k)


def mutate_rewrite(home: Path, k: int):
    """The prefix of A changes (line a-2 is different), so the sync must read A from 0."""
    write(tree_dir(home, PROJECTS) / SLUG / f"{SID_A}.jsonl", b"".join([
        usage_line("a-1", SID_A, "m1", 0, MODEL_A, 10, 20, cc=5),
        usage_line("a-2", SID_A, "m1", 0, MODEL_A, 99, 1, cc=5),
        usage_line("a-3", SID_A, "m1", 0, MODEL_A, 10, 20, cc=5),
        usage_line("b-1", SID_A, "m2", 1, MODEL_A, 10, 20),
        usage_line("b-2", SID_A, "m2", 1, MODEL_A, 40, 40)]), k)


def mutate_shrink(home: Path, k: int):
    """A file that becomes shorter than what was ingested."""
    write(tree_dir(home, PROJECTS) / SLUG / f"{sid_g(3)}.jsonl", b"", k)


def mutate_many(home: Path, k: int, n: int = 20):
    """One more line in each of the first n small sessions: n changed transcripts."""
    for i in range(n):
        append(tree_dir(home, PROJECTS) / SLUG / f"{sid_g(i)}.jsonl",
               usage_line(f"g{i}-3", sid_g(i), f"g{i % 5}", 60 + i, MODEL_A, 2, 2), k)


# ---------------------------------------------------------------- what is compared

def digest(db_path) -> str:
    """One hash over the stored usage rows, the rollup, the offsets and the prices."""
    if not Path(db_path).exists():
        return "missing"
    con = sqlite3.connect(str(db_path))
    try:
        parts = [repr(con.execute(q).fetchall()) for q in QUERIES]
    finally:
        con.close()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def table_rows(db_path, name):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(TABLE_QUERIES[name]).fetchall()
    finally:
        con.close()


def compare_dbs(a, b) -> list:
    """Field-by-field comparison of the four derived and stored tables, plus the prices."""
    problems = []
    for name in TABLE_QUERIES:
        x, y = table_rows(a, name), table_rows(b, name)
        if x == y:
            continue
        n = min(len(x), len(y))
        first = next((i for i in range(n) if x[i] != y[i]), n)
        detail = ""
        if first < n:
            detail = f"; first difference at row {first}: {x[first]!r} vs {y[first]!r}"
        problems.append(f"{name}: {len(x)} vs {len(y)} rows{detail}")
    return problems


EVENT_SELECT = "SELECT {cols} FROM (SELECT u.*, ROW_NUMBER() OVER (PARTITION BY u.session_id, " \
               "u.message_id ORDER BY u.total_tokens DESC, u.ts_epoch DESC, u.uuid) AS rn " \
               "FROM usage_events u) WHERE rn = 1"
ROLLUP_SELECT = ("SELECT session_id, date, model, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                 "SUM(cache_creation_tokens), SUM(cache_read_tokens), SUM(ephemeral_5m_tokens), "
                 "SUM(ephemeral_1h_tokens), SUM(total_tokens) FROM usage_dedup "
                 "GROUP BY session_id, date, model")


def consistency_problems(db_path, columns) -> list:
    """The invariant every commit boundary must keep: usage_dedup is the winner of every message
    in usage_events, and usage_rollup is the per-session sum of usage_dedup."""
    if not Path(db_path).exists():
        return ["database missing"]
    cols = ", ".join(columns)
    win = EVENT_SELECT.format(cols=cols)
    con = sqlite3.connect(str(db_path))
    try:
        problems = []
        n1 = con.execute(f"SELECT COUNT(*) FROM ({win} EXCEPT SELECT {cols} FROM usage_dedup)"
                         ).fetchone()[0]
        n2 = con.execute(f"SELECT COUNT(*) FROM (SELECT {cols} FROM usage_dedup EXCEPT {win})"
                         ).fetchone()[0]
        if n1 or n2:
            problems.append(f"usage_dedup differs from the winners: {n1} missing, {n2} extra")
        r1 = con.execute(f"SELECT COUNT(*) FROM ({ROLLUP_SELECT} EXCEPT SELECT * FROM usage_rollup)"
                         ).fetchone()[0]
        r2 = con.execute(f"SELECT COUNT(*) FROM (SELECT * FROM usage_rollup EXCEPT {ROLLUP_SELECT})"
                         ).fetchone()[0]
        if r1 or r2:
            problems.append(f"usage_rollup differs from usage_dedup: {r1} missing, {r2} extra")
        return problems
    finally:
        con.close()


# ---------------------------------------------------------------- instrumentation

class Log:
    def __init__(self):
        self.commits = 0
        self.digests = []      # digest of the database after each COMMIT, in order
        self.pre = None        # digest before the run


class CountingConn:
    def __init__(self, conn, log, db_path):
        self._c = conn
        self._log = log
        self._db_path = db_path

    def execute(self, sql, *args):
        cur = self._c.execute(sql, *args)
        if sql.strip().upper() == "COMMIT":
            self._log.commits += 1
            self._log.digests.append(digest(self._db_path()))
        return cur

    def executemany(self, sql, seq):
        return self._c.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self._c, name)


class ShimSqlite:
    def __init__(self, log, db_path):
        self._log = log
        self._db_path = db_path

    def connect(self, *args, **kwargs):
        return CountingConn(sqlite3.connect(*args, **kwargs), self._log, self._db_path)

    def __getattr__(self, name):
        return getattr(sqlite3, name)


@contextlib.contextmanager
def instrumented(mod, log, fail_after=None):
    """Count the COMMITs of mod's connections and record the digest after each one. With
    fail_after = N, raise InjectedCrash once N changed transcripts have been ingested."""
    real_sqlite, orig_one = mod.sqlite3, mod._ingest_one
    count = [0]

    def ingest_one(*args, **kwargs):
        result = orig_one(*args, **kwargs)
        count[0] += 1
        if fail_after is not None and count[0] == fail_after:
            raise InjectedCrash(f"after transcript {count[0]}")
        return result

    mod.sqlite3 = ShimSqlite(log, lambda: mod.DB_PATH)
    mod._ingest_one = ingest_one
    try:
        yield log
    finally:
        mod.sqlite3 = real_sqlite
        mod._ingest_one = orig_one


def prepare(mod, home: Path, files: dict, mutate=False):
    """A home holding files, with a finished full build (and optionally 20 changed transcripts)."""
    shutil.rmtree(home, ignore_errors=True)
    materialize(home, files)
    point(mod, home)
    with batch_size(mod, BATCH_TEST):
        rc, _ = quiet(mod.ingest, full=True)
    assert rc == 0
    if mutate:
        mutate_many(home, 1)


# ---------------------------------------------------------------- tests

def run_steps(steps, homes):
    """Run the same steps on the reference and on the new module, each in its own home, and
    compare after every run step."""
    ref_home, new_home = homes
    rows = 0
    with batch_size(REF, BATCH_TEST), batch_size(NEW, BATCH_TEST):
        for n, (name, kind, fn) in enumerate(steps, start=1):
            if kind == "mutate":
                fn(ref_home, n)
                fn(new_home, n)
                continue
            for mod, home in ((REF, ref_home), (NEW, new_home)):
                point(mod, home)
                rc, _ = quiet(fn, mod, home)
                assert rc == 0, f"{name}: rc {rc}"
            problems = compare_dbs(REF.DB_PATH, NEW.DB_PATH)
            problems += [f"new: {p}" for p in consistency_problems(NEW.DB_PATH, NEW.EVENT_COLUMNS)]
            assert not problems, f"after '{name}': " + "; ".join(problems)
            rows = len(table_rows(NEW.DB_PATH, "usage_events"))
    return rows


def t_equiv_synthetic():
    base = SCRATCH_ROOT / "eq-synth"
    ref_home, new_home = base / "ref", base / "new"
    for h in (ref_home, new_home):
        shutil.rmtree(h, ignore_errors=True)
        materialize(h, base_files())

    def session_b(mod, home):
        return mod.ingest_session(tree_dir(home, PROJECTS) / SLUG / f"{SID_B}.jsonl", SID_B)

    def catch(mod, home):
        return mod.catch_up(bypass_cooldown=True)

    steps = [
        ("full build", "run", lambda m, h: m.ingest(full=True)),
        ("sync, nothing changed", "run", lambda m, h: m.ingest(full=False)),
        ("grow, new file", "mutate", mutate_grow),
        ("grow, new file (2)", "mutate", mutate_new),
        ("sync after growth", "run", lambda m, h: m.ingest(full=False)),
        ("prefix rewritten, file shrunk", "mutate", mutate_rewrite),
        ("shrink", "mutate", mutate_shrink),
        ("sync after rewrite", "run", lambda m, h: m.ingest(full=False)),
        ("session close of B", "run", session_b),
        ("catch-up", "run", catch),
        ("full build again", "run", lambda m, h: m.ingest(full=True)),
    ]
    rows = run_steps(steps, (ref_home, new_home))
    return (f"{len(steps)} steps; after each run usage_events, usage_dedup, usage_rollup, "
            f"ingest_state and model_pricing equal field by field; {rows} events at the end")


def t_equiv_real():
    src_mirror = REAL_SOURCE / "session-archive" / MIRROR
    src_live = REAL_SOURCE / PROJECTS
    if not src_mirror.is_dir():
        return "SKIP: no real transcript folder at the source (set PR27_SOURCE_HOME)"
    pool = sorted((p.relative_to(src_mirror) for p in src_mirror.rglob("*.jsonl")
                   if p.is_file() and "acompact" not in str(p)
                   and p.stat().st_size <= 30 * 1024 * 1024), key=str)
    chosen = sorted(random.Random(27).sample(pool, min(50, len(pool))), key=str)
    base = SCRATCH_ROOT / "eq-real" / "stage"
    shutil.rmtree(base, ignore_errors=True)
    total = 0
    files = {}
    for rel in chosen:
        for tree, src in ((MIRROR, src_mirror), (PROJECTS, src_live)):
            s = src / rel
            if not s.is_file():
                continue
            d = base / tree / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)          # copies bytes and mtime; the source is only read
            total += d.stat().st_size
            files[(tree, str(rel))] = d.read_bytes()
    ref_home, new_home = SCRATCH_ROOT / "eq-real" / "ref", SCRATCH_ROOT / "eq-real" / "new"
    for h in (ref_home, new_home):
        shutil.rmtree(h, ignore_errors=True)
        for (tree, rel), data in files.items():
            p = tree_dir(h, tree) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.utime(p, ns=(src_stat_ns(base, tree, rel),) * 2)
    steps = [
        ("full build of 50 real transcripts", "run", lambda m, h: m.ingest(full=True)),
        ("sync, nothing changed", "run", lambda m, h: m.ingest(full=False)),
    ]
    rows = run_steps(steps, (ref_home, new_home))
    return (f"{len(chosen)} transcripts, {len(files)} files, {total:,} bytes: build and sync equal "
            f"field by field; {rows:,} events")


def src_stat_ns(base, tree, rel):
    return (base / tree / rel).stat().st_mtime_ns


def t_crash_exception():
    """An error in the middle of a batch. The database must equal the last commit before it, keep
    its invariant, and a re-run must give the rows of a run that was never interrupted."""
    detail = []
    for mode in ("sync", "build"):
        home = SCRATCH_ROOT / f"exc-{mode}"
        prepare(NEW, home, base_files(), mutate=(mode == "sync"))
        point(NEW, home)
        pre = digest(NEW.DB_PATH)
        log = Log()
        log.pre = pre
        with batch_size(NEW, BATCH_TEST):
            crashed = False
            with instrumented(NEW, log, fail_after=10):
                try:
                    quiet(NEW.ingest, full=(mode == "build"))
                except InjectedCrash:
                    crashed = True
        assert crashed, f"{mode}: the injected error did not fire"
        expected = log.digests[-1] if log.digests else pre
        assert digest(NEW.DB_PATH) == expected, f"{mode}: database is not the last commit"
        problems = consistency_problems(NEW.DB_PATH, NEW.EVENT_COLUMNS)
        assert not problems, f"{mode}: after the crash, " + "; ".join(problems)
        with batch_size(NEW, BATCH_TEST):
            quiet(NEW.ingest, full=(mode == "build"))
        final = digest(NEW.DB_PATH)
        clean = SCRATCH_ROOT / f"exc-{mode}-clean"
        prepare(NEW, clean, base_files(), mutate=(mode == "sync"))
        point(NEW, clean)
        with batch_size(NEW, BATCH_TEST):
            quiet(NEW.ingest, full=(mode == "build"))
        assert final == digest(NEW.DB_PATH), f"{mode}: re-run differs from an uninterrupted run"
        point(NEW, home)
        detail.append(f"{mode}: rolled back to commit {len(log.digests)} of the run")
    return "; ".join(detail)


KILL_DRIVER = r'''
import hashlib, importlib.util, os, sqlite3, sys
scripts, home, mode, kill_after, batch, log_path, module_path = sys.argv[1:8]
kill_after, batch = int(kill_after), int(batch)
os.environ["SESSION_GUARD_HOME"] = home
sys.path.insert(0, scripts)
import guardkit  # noqa: F401  (CLAUDE_DIR comes from SESSION_GUARD_HOME)
QUERIES = __QUERIES__
real = sqlite3

def note(tag, value):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(tag + " " + value + "\n")
        f.flush()

def digest(path):
    if not os.path.exists(path):
        return "missing"
    c = real.connect(path)
    try:
        parts = [repr(c.execute(q).fetchall()) for q in QUERIES]
    finally:
        c.close()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

spec = importlib.util.spec_from_file_location("mod_under_test", module_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name in ("BATCH_FILES", "COMMIT_EVERY"):
    if hasattr(mod, name):
        setattr(mod, name, batch)
note("pre", digest(str(mod.DB_PATH)))

class Conn:
    def __init__(self, c):
        self._c = c
    def execute(self, sql, *a):
        cur = self._c.execute(sql, *a)
        if sql.strip().upper() == "COMMIT":
            note("commit", digest(str(mod.DB_PATH)))
        return cur
    def executemany(self, sql, seq):
        return self._c.executemany(sql, seq)
    def __getattr__(self, name):
        return getattr(self._c, name)

class Shim:
    def connect(self, *a, **k):
        return Conn(real.connect(*a, **k))
    def __getattr__(self, name):
        return getattr(real, name)

mod.sqlite3 = Shim()
orig = mod._ingest_one
count = [0]

def kill_after_n(*a, **k):
    result = orig(*a, **k)
    count[0] += 1
    if count[0] == kill_after:
        os._exit(7)          # no Python cleanup, no ROLLBACK: the process just stops
    return result

mod._ingest_one = kill_after_n
mod.ingest(full=(mode == "build"))
note("finished", "1")
'''


def t_crash_hard_kill():
    """The process is killed in the middle of a batch (no rollback in Python). On the next open the
    database must equal the last commit, and a re-run must match an uninterrupted run."""
    import sys as _sys
    driver = SCRATCH_ROOT / "kill_driver.py"
    driver.write_text(KILL_DRIVER.replace("__QUERIES__", repr(QUERIES)), encoding="utf-8")
    detail = []
    for mode in ("sync", "build"):
        home = SCRATCH_ROOT / f"kill-{mode}"
        prepare(NEW, home, base_files(), mutate=(mode == "sync"))
        point(NEW, home)
        pre = digest(NEW.DB_PATH)
        log_path = SCRATCH_ROOT / f"kill-{mode}.log"
        log_path.unlink(missing_ok=True)
        proc = subprocess.run(
            [_sys.executable, "-I", str(driver), str(SCRIPTS), str(home), mode, "10",
             str(BATCH_TEST), str(log_path), str(new_module_path())],
            capture_output=True, text=True, timeout=600)
        assert proc.returncode == 7, f"{mode}: child exit {proc.returncode}: {proc.stderr[-400:]}"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert lines and lines[0] == "pre " + pre, f"{mode}: child saw a different start state"
        assert "finished 1" not in lines, f"{mode}: the child was not killed"
        commits = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("commit ")]
        expected = commits[-1] if commits else pre
        point(NEW, home)
        assert digest(NEW.DB_PATH) == expected, f"{mode}: database is not the last commit"
        problems = consistency_problems(NEW.DB_PATH, NEW.EVENT_COLUMNS)
        assert not problems, f"{mode}: after the kill, " + "; ".join(problems)
        with batch_size(NEW, BATCH_TEST):
            quiet(NEW.ingest, full=(mode == "build"))
        final = digest(NEW.DB_PATH)
        clean = SCRATCH_ROOT / f"kill-{mode}-clean"
        prepare(NEW, clean, base_files(), mutate=(mode == "sync"))
        point(NEW, clean)
        with batch_size(NEW, BATCH_TEST):
            quiet(NEW.ingest, full=(mode == "build"))
        assert final == digest(NEW.DB_PATH), f"{mode}: re-run differs from an uninterrupted run"
        detail.append(f"{mode}: killed after {len(commits)} commits, re-run equal")
    return "; ".join(detail)


def bulk_files(n):
    return {(PROJECTS, f"{SLUG}-bulk/S{i:04}.jsonl"):
            usage_line(f"u{i}", sid_g(i), f"m{i}", i % 3000, MODEL_A, 10, 20) for i in range(n)}


def t_commit_count():
    """A full run commits at most ceil(transcripts / batch) + 1 times. The count includes the
    schema creation of a new file, so the bound is checked on a fresh database."""
    n = 1203
    bound = math.ceil(n / PRODUCTION_BATCH) + 1
    counts = {}
    for label, mod in (("new", NEW), ("reference", REF)):
        home = SCRATCH_ROOT / f"count-{label}"
        shutil.rmtree(home, ignore_errors=True)
        materialize(home, bulk_files(n))
        point(mod, home)
        log = Log()
        with instrumented(mod, log):
            rc, _ = quiet(mod.ingest, full=True)
        assert rc == 0
        build = log.commits
        for i in range(n):
            append(tree_dir(home, PROJECTS) / f"{SLUG}-bulk" / f"S{i:04}.jsonl",
                   usage_line(f"v{i}", sid_g(i), f"m{i}", 5, MODEL_A, 1, 1), 2)
        log2 = Log()
        with instrumented(mod, log2):
            rc, _ = quiet(mod.ingest, full=False)
        assert rc == 0
        counts[label] = (build, log2.commits)
    new_build, new_sync = counts["new"]
    ref_build, ref_sync = counts["reference"]
    assert new_build <= bound, f"full run committed {new_build} times, bound {bound}"
    assert new_sync <= bound, f"sync of {n} changed transcripts committed {new_sync} times"
    return (f"{n} transcripts, bound {bound}: full run {new_build} commits (reference {ref_build}); "
            f"sync of all changed {new_sync} (reference {ref_sync})")


TESTS = [t_equiv_synthetic, t_equiv_real, t_crash_exception, t_crash_hard_kill, t_commit_count]


def main():
    global NEW, REF
    failed = 0
    try:
        NEW = point(load("usage_db_under_test", new_module_path()), SCRATCH_ROOT / "boot")
        REF = point(load("usage_db_reference", reference_module_path()), SCRATCH_ROOT / "boot")
        print(f"module under test: {new_module_path()}")
        for fn in TESTS:
            name = fn.__name__[2:]
            try:
                detail = fn()
                if isinstance(detail, str) and detail.startswith("SKIP"):
                    print(f"SKIP {name}: {detail[len('SKIP: '):]}")
                    continue
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
