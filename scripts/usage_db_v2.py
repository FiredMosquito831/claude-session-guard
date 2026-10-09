#!/usr/bin/env python3
"""
Usage DB v2 (PR-06): the same tables, views and output as usage_db.py, built
incrementally. usage_db.py stays as it is; this file is the replacement.

Differences from usage_db.py:
  - sync reads only the bytes appended since the last ingest, per transcript.
    A head/tail SHA-256 check (as in session_archive_v2.py) confirms the prefix
    that was ingested; if it changed, or the file shrank, the file is read from 0.
  - the schema and the pricing seed are created once per SCHEMA_VERSION, in one
    transaction, not on every connection.
  - usage_dedup holds one row per (session_id, message_id): the winning line.
    It is maintained on ingest, and every period view reads it.
  - stats prints the deduplicated total and the raw per-line sum, both labelled.
  - export is a separate command. sync never writes CSV files.
  - sql opens the database read-only (mode=ro).

Paths come from guardkit.CLAUDE_DIR, so SESSION_GUARD_HOME redirects everything.

Storage: <CLAUDE_DIR>/session-archive/usage.db
  usage_events    one row per usage-bearing transcript line (uuid PK)
  usage_dedup     one row per (session_id, message_id): the winning line
  model_pricing   per-model USD rates per 1M tokens (INSERT OR IGNORE seed)
  ingest_state    per-source size, mtime, byte offset and head/tail checksums
  schema_version  the schema version this file was built with

Commands (same meaning as usage_db.py):
  build            full rebuild from the archive (derived rows are dropped first)
  sync             incremental: only appended bytes of changed transcripts
  stats            headline totals, deduplicated and raw, both labelled
  models           per-model token and cost breakdown
  export           regenerate every CSV rollup into session-archive/reports/
  hourly [N]       last N clock hours (default 48)
  daily [N]        last N days (default 30)
  weekly           all weeks
  monthly          all months
  sessions [N]     session timeline
  sql "<query>"    read-only SQL (mode=ro), tab-separated output
  schema           print the schema

No external deps -- stdlib sqlite3 only.
"""

import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import guardkit

CLAUDE_DIR = guardkit.CLAUDE_DIR
ARCHIVE_DIR = CLAUDE_DIR / "session-archive"
TRANSCRIPTS_DIR = ARCHIVE_DIR / "transcripts"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REPORTS_DIR = ARCHIVE_DIR / "reports"
DB_PATH = ARCHIVE_DIR / "usage.db"

SCHEMA_VERSION = 1
HASH_WINDOW = 4096          # bytes hashed at each end of the ingested prefix (as session_archive_v2)
COMMIT_EVERY = 200          # changed transcripts per transaction
BUSY_TIMEOUT_MS = 60000
SCHEMA_RUNS = 0             # how often create_schema() actually ran in this process (tests read it)

# USD per 1,000,000 tokens. Copied exactly from usage_db.py SEED_PRICING.
SEED_PRICING = [
    # model_pattern,            input,  output, cache_write_5m, cache_write_1h, cache_read
    ("claude-opus-5",            15.00,  75.00,  18.75,          30.00,          1.50),
    ("claude-opus-4",            15.00,  75.00,  18.75,          30.00,          1.50),
    ("claude-sonnet-5",           3.00,  15.00,   3.75,           6.00,          0.30),
    ("claude-sonnet-4",           3.00,  15.00,   3.75,           6.00,          0.30),
    ("claude-fable-5",            3.00,  15.00,   3.75,           6.00,          0.30),
    ("claude-haiku-4-5",          1.00,   5.00,   1.25,           2.00,          0.10),
    ("claude-3-5-haiku",          0.80,   4.00,   1.00,           1.60,          0.08),
]

# Column order of usage_events and usage_dedup. Same as usage_db.py EVENT_COLUMNS.
EVENT_COLUMNS = [
    "uuid", "session_id", "request_id", "message_id", "ts_iso", "ts_epoch",
    "date", "month", "hour", "model", "project", "git_branch", "cc_version",
    "is_sidechain", "service_tier", "speed", "effort", "stop_reason",
    "input_tokens", "output_tokens", "cache_creation_tokens",
    "cache_read_tokens", "ephemeral_5m_tokens", "ephemeral_1h_tokens",
    "total_tokens", "cost_usd", "source_file",
]
_EVENT_COLS = ", ".join(EVENT_COLUMNS)
_EVENT_PH = ",".join("?" * len(EVENT_COLUMNS))
_UPSERT_SET = ", ".join(f"{c}=excluded.{c}" for c in EVENT_COLUMNS if c != "uuid")
INSERT_SQL = (f"INSERT INTO usage_events ({_EVENT_COLS}) VALUES ({_EVENT_PH}) "
              f"ON CONFLICT(uuid) DO UPDATE SET {_UPSERT_SET}")
# Winner of one (session_id, message_id): the same ORDER BY as the window function
# in usage_db.py (total DESC, ts DESC, uuid), so the first row is the window's rn = 1.
WINNER_SQL = (f"SELECT {_EVENT_COLS} FROM usage_events WHERE session_id=? AND message_id=? "
              f"ORDER BY total_tokens DESC, ts_epoch DESC, uuid LIMIT 1")
DEDUP_PUT_SQL = (f"INSERT OR REPLACE INTO usage_dedup ({_EVENT_COLS}) "
                 f"VALUES ({_EVENT_PH})")
DEDUP_DEL_SQL = "DELETE FROM usage_dedup WHERE session_id=? AND message_id=?"
STATE_UPSERT_SQL = """INSERT INTO ingest_state
    (source_file, mtime_ns, size, rows_seen, ingested_at, byte_offset, head_sha, tail_sha)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(source_file) DO UPDATE SET
      mtime_ns=excluded.mtime_ns, size=excluded.size, rows_seen=excluded.rows_seen,
      ingested_at=excluded.ingested_at, byte_offset=excluded.byte_offset,
      head_sha=excluded.head_sha, tail_sha=excluded.tail_sha"""

# Added to ingest_state after the usage_db.py layout. Older files get them by ALTER TABLE.
STATE_NEW_COLUMNS = [("byte_offset", "INTEGER"), ("head_sha", "TEXT"), ("tail_sha", "TEXT")]

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS usage_events (
    uuid                  TEXT PRIMARY KEY,
    session_id            TEXT,
    request_id            TEXT,
    message_id            TEXT,
    ts_iso                TEXT,
    ts_epoch              INTEGER,
    date                  TEXT,
    month                 TEXT,
    hour                  INTEGER,
    model                 TEXT,
    project               TEXT,
    git_branch            TEXT,
    cc_version            TEXT,
    is_sidechain          INTEGER,
    service_tier          TEXT,
    speed                 TEXT,
    effort                TEXT,
    stop_reason           TEXT,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    ephemeral_5m_tokens   INTEGER DEFAULT 0,
    ephemeral_1h_tokens   INTEGER DEFAULT 0,
    total_tokens          INTEGER DEFAULT 0,
    cost_usd              REAL,
    source_file           TEXT
)""",
    "CREATE INDEX IF NOT EXISTS ix_ue_date    ON usage_events(date)",
    "CREATE INDEX IF NOT EXISTS ix_ue_month   ON usage_events(month)",
    "CREATE INDEX IF NOT EXISTS ix_ue_model   ON usage_events(model)",
    "CREATE INDEX IF NOT EXISTS ix_ue_session ON usage_events(session_id)",
    "CREATE INDEX IF NOT EXISTS ix_ue_project ON usage_events(project)",
    "CREATE INDEX IF NOT EXISTS ix_ue_epoch   ON usage_events(ts_epoch)",
    "CREATE INDEX IF NOT EXISTS ix_ue_msg     ON usage_events(session_id, message_id)",
    """CREATE TABLE IF NOT EXISTS usage_dedup (
    uuid                  TEXT,
    session_id            TEXT NOT NULL,
    request_id            TEXT,
    message_id            TEXT NOT NULL,
    ts_iso                TEXT,
    ts_epoch              INTEGER,
    date                  TEXT,
    month                 TEXT,
    hour                  INTEGER,
    model                 TEXT,
    project               TEXT,
    git_branch            TEXT,
    cc_version            TEXT,
    is_sidechain          INTEGER,
    service_tier          TEXT,
    speed                 TEXT,
    effort                TEXT,
    stop_reason           TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens     INTEGER,
    ephemeral_5m_tokens   INTEGER,
    ephemeral_1h_tokens   INTEGER,
    total_tokens          INTEGER,
    cost_usd              REAL,
    source_file           TEXT,
    PRIMARY KEY (session_id, message_id)
)""",
    """CREATE TABLE IF NOT EXISTS model_pricing (
    model_pattern   TEXT PRIMARY KEY,
    input_per_mtok  REAL NOT NULL DEFAULT 0,
    output_per_mtok REAL NOT NULL DEFAULT 0,
    cache_write_5m_per_mtok REAL NOT NULL DEFAULT 0,
    cache_write_1h_per_mtok REAL NOT NULL DEFAULT 0,
    cache_read_per_mtok     REAL NOT NULL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS ingest_state (
    source_file TEXT PRIMARY KEY,
    mtime_ns    INTEGER,
    size        INTEGER,
    rows_seen   INTEGER,
    ingested_at TEXT
)""",
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)",
]

# Views are dropped and recreated on every schema version change, so a file that
# was built by usage_db.py gets the usage_dedup versions. Base views come first.
VIEWS = [
    ("v_events_dedup", """CREATE VIEW v_events_dedup AS
SELECT uuid, session_id, request_id, message_id, ts_iso, ts_epoch, date, month,
       hour, model, project, git_branch, cc_version, is_sidechain, service_tier,
       speed, effort, stop_reason, input_tokens, output_tokens,
       cache_creation_tokens, cache_read_tokens, ephemeral_5m_tokens,
       ephemeral_1h_tokens, total_tokens, cost_usd, source_file
FROM usage_dedup"""),
    ("v_events_costed", """CREATE VIEW v_events_costed AS
SELECT e.*,
       COALESCE((
         SELECT ( e.input_tokens          * p.input_per_mtok
                + e.output_tokens         * p.output_per_mtok
                + e.ephemeral_5m_tokens   * p.cache_write_5m_per_mtok
                + e.ephemeral_1h_tokens   * p.cache_write_1h_per_mtok
                + (e.cache_creation_tokens - e.ephemeral_5m_tokens - e.ephemeral_1h_tokens)
                                          * p.cache_write_5m_per_mtok
                + e.cache_read_tokens     * p.cache_read_per_mtok ) / 1000000.0
         FROM model_pricing p
         WHERE e.model LIKE p.model_pattern || '%'
         ORDER BY LENGTH(p.model_pattern) DESC LIMIT 1
       ), 0.0) AS calc_cost_usd
FROM usage_dedup e"""),
    ("v_daily", """CREATE VIEW v_daily AS
SELECT date,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY date ORDER BY date"""),
    ("v_monthly", """CREATE VIEW v_monthly AS
SELECT month,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY month ORDER BY month"""),
    ("v_by_model", """CREATE VIEW v_by_model AS
SELECT model,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       MIN(date) AS first_used,
       MAX(date) AS last_used,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY model ORDER BY total_tokens DESC"""),
    ("v_daily_by_model", """CREATE VIEW v_daily_by_model AS
SELECT date, model,
       COUNT(*) AS events,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY date, model ORDER BY date, total_tokens DESC"""),
    ("v_by_session", """CREATE VIEW v_by_session AS
SELECT session_id,
       MIN(ts_iso) AS first_activity,
       MAX(ts_iso) AS last_activity,
       COUNT(*) AS events,
       COUNT(DISTINCT model) AS models_used,
       MAX(project) AS project,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY session_id ORDER BY total_tokens DESC"""),
    ("v_by_project", """CREATE VIEW v_by_project AS
SELECT project,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE project IS NOT NULL AND project <> ''
GROUP BY project ORDER BY total_tokens DESC"""),
    ("v_hourly", """CREATE VIEW v_hourly AS
SELECT hour,
       COUNT(*) AS events,
       SUM(total_tokens) AS total_tokens
FROM v_events_costed GROUP BY hour ORDER BY hour"""),
    ("v_hourly_ts", """CREATE VIEW v_hourly_ts AS
SELECT strftime('%Y-%m-%d %H:00', ts_iso) AS hour_bucket,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY hour_bucket ORDER BY hour_bucket"""),
    ("v_hourly_ts_by_model", """CREATE VIEW v_hourly_ts_by_model AS
SELECT strftime('%Y-%m-%d %H:00', ts_iso) AS hour_bucket, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY hour_bucket, model ORDER BY hour_bucket, total_tokens DESC"""),
    ("v_weekly", """CREATE VIEW v_weekly AS
SELECT strftime('%Y-W%W', date) AS week,
       MIN(date) AS week_start, MAX(date) AS week_end,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY week ORDER BY week"""),
    ("v_weekly_by_model", """CREATE VIEW v_weekly_by_model AS
SELECT strftime('%Y-W%W', date) AS week, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY week, model ORDER BY week, total_tokens DESC"""),
    ("v_monthly_by_model", """CREATE VIEW v_monthly_by_model AS
SELECT month, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY month, model ORDER BY month, total_tokens DESC"""),
    ("v_sessions_timeline", """CREATE VIEW v_sessions_timeline AS
SELECT session_id,
       MIN(ts_iso) AS started,
       MAX(ts_iso) AS ended,
       ROUND((MAX(ts_epoch) - MIN(ts_epoch)) / 60.0, 1) AS duration_min,
       MAX(project) AS project,
       COUNT(*) AS events,
       GROUP_CONCAT(DISTINCT model) AS models,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY session_id ORDER BY started"""),
    ("v_blocks_5h", """CREATE VIEW v_blocks_5h AS
SELECT datetime((ts_epoch / 18000) * 18000, 'unixepoch') AS block_start,
       datetime(((ts_epoch / 18000) + 1) * 18000, 'unixepoch') AS block_end,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY ts_epoch / 18000 ORDER BY block_start DESC"""),
]
VIEW_NAMES = [name for name, _ in VIEWS]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_version(conn) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.OperationalError:          # no schema_version table yet
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _connect_rw() -> sqlite3.Connection:
    """Open usage.db for reading and writing. The schema is created or migrated only when stale."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if _schema_version(conn) < SCHEMA_VERSION:
        create_schema(conn)
    return conn


def _connect_ro() -> sqlite3.Connection:
    """Open usage.db with mode=ro. Raises sqlite3.OperationalError if the file does not exist."""
    uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def create_schema(conn) -> bool:
    """Create or migrate the schema, in one transaction. False when it was already current."""
    global SCHEMA_RUNS
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = _schema_version(conn)          # read again under the write lock
        if prev >= SCHEMA_VERSION:
            conn.execute("COMMIT")
            return False
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        have = {r[1] for r in conn.execute("PRAGMA table_info(ingest_state)")}
        for col, typ in STATE_NEW_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE ingest_state ADD COLUMN {col} {typ}")
        for name, sql in VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(sql)
        conn.executemany("INSERT OR IGNORE INTO model_pricing VALUES (?,?,?,?,?,?)", SEED_PRICING)
        if prev < 1:                          # first build of this file, or a usage_db.py file
            _rebuild_dedup_all(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    SCHEMA_RUNS += 1
    return True


def parse_ts(raw: str):
    """ISO-8601 -> (iso, epoch, date, month, hour) in UTC. Same as usage_db.py."""
    if not raw:
        return ("", None, "", "", None)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except Exception:
        return (str(raw), None, "", "", None)
    return (dt.isoformat(), int(dt.timestamp()),
            dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m"), dt.hour)


def parse_line(raw: bytes, rel: str):
    """One transcript line -> one usage_events tuple, or None. Same rules as usage_db.rows_from_file."""
    if b'"usage"' not in raw:
        return None
    line = raw.decode("utf-8", errors="surrogateescape").strip()
    if not line:
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict):
        return None
    msg = e.get("message")
    if not isinstance(msg, dict):
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    uid = e.get("uuid") or ""
    if not uid:
        return None

    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    e5 = e1h = 0
    cache_detail = u.get("cache_creation")
    if isinstance(cache_detail, dict):
        e5 = int(cache_detail.get("ephemeral_5m_input_tokens") or 0)
        e1h = int(cache_detail.get("ephemeral_1h_input_tokens") or 0)

    iso, epoch, date, month, hour = parse_ts(e.get("timestamp", ""))

    cost = None
    for k in ("costUSD", "cost_usd", "costUsd"):
        if isinstance(e.get(k), (int, float)):
            cost = float(e[k])
            break

    return (
        uid,
        e.get("sessionId") or e.get("session_id") or "",
        e.get("requestId") or "",
        msg.get("id") or "",
        iso, epoch, date, month, hour,
        msg.get("model") or "",
        e.get("cwd") or "",
        e.get("gitBranch") or "",
        e.get("version") or "",
        1 if e.get("isSidechain") else 0,
        u.get("service_tier") or "",
        u.get("speed") or "",
        str(e.get("effort") or ""),
        msg.get("stop_reason") or "",
        inp, out, cc, cr, e5, e1h,
        inp + out + cc + cr,
        cost,
        rel,
    )


def sources():
    """(relative_key, path) for every transcript to ingest. Same rules as usage_db.sources():
    the archive copy of every transcript, plus each live transcript that is bigger than its
    archive copy (key 'rel|live'). A live file with no archive copy is read under its own key."""
    archived = {}
    if TRANSCRIPTS_DIR.exists():
        for p in TRANSCRIPTS_DIR.rglob("*.jsonl"):
            if p.is_file():
                rel = str(p.relative_to(TRANSCRIPTS_DIR))
                archived[rel] = p
                yield rel, p
    if PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.rglob("*.jsonl"):
            if not p.is_file() or "acompact" in str(p):
                continue
            rel = str(p.relative_to(PROJECTS_DIR))
            arch = archived.get(rel)
            if arch is not None:
                try:
                    if p.stat().st_size <= arch.stat().st_size:
                        continue
                except OSError:
                    continue
                yield rel + "|live", p
            else:
                yield rel, p


def _load_state(conn) -> dict:
    state = {}
    for src, mtime, size, off, head, tail in conn.execute(
            "SELECT source_file, mtime_ns, size, byte_offset, head_sha, tail_sha FROM ingest_state"):
        state[src] = {"mtime_ns": mtime, "size": size, "offset": off, "head": head, "tail": tail}
    return state


def _read_span(path: Path, start: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(length)


def _checksums(path: Path, off: int):
    """(head_sha, tail_sha) of the ingested prefix [0, off), defined as in session_archive_v2."""
    k = min(HASH_WINDOW, off)
    return _sha(_read_span(path, 0, k)), _sha(_read_span(path, off - k, k))


def _resume_offset(path: Path, st_size: int, prev: dict):
    """The byte offset to resume from, or None when the file must be read from 0."""
    off = prev["offset"]
    if off is None or prev["head"] is None or prev["tail"] is None:
        return None
    if off < 0 or off > st_size:              # the file shrank below what was ingested
        return None
    k = min(HASH_WINDOW, off)
    head = _read_span(path, 0, k)
    tail = _read_span(path, off - k, k)
    if len(head) != k or len(tail) != k:
        return None
    if _sha(head) != prev["head"] or _sha(tail) != prev["tail"]:
        return None                           # the prefix that was ingested has changed
    return off


def scan(path: Path, start: int, end: int, rel: str):
    """Usage rows in bytes [start, end) of path, and the new offset. The offset is just past the
    last newline-terminated line. An unterminated final line is parsed but not consumed, so a line
    still being written is read again on the next sync (its upsert is idempotent)."""
    rows = []
    off = pos = start
    with open(path, "rb") as f:
        f.seek(start)
        while pos < end:
            raw = f.readline()
            if not raw:
                break
            if pos + len(raw) > end:
                raw = raw[:end - pos]
            pos += len(raw)
            if raw.endswith(b"\n"):
                off = pos
            row = parse_line(raw, rel)
            if row is not None:
                rows.append(row)
    return rows, off


def _refresh_message(conn, sid: str, mid: str) -> None:
    """Set usage_dedup for one message to the winning usage_events row, or remove it."""
    row = conn.execute(WINNER_SQL, (sid, mid)).fetchone()
    if row is None:
        conn.execute(DEDUP_DEL_SQL, (sid, mid))
    else:
        conn.execute(DEDUP_PUT_SQL, row)


def _flush_dirty(conn, dirty: set) -> None:
    for sid, mid in sorted(dirty):
        _refresh_message(conn, sid, mid)
    dirty.clear()


def _rebuild_dedup_all(conn) -> None:
    """usage_dedup from scratch with the window function. Used on migration and at the end of build."""
    conn.execute("DELETE FROM usage_dedup")
    conn.execute(f"""INSERT INTO usage_dedup ({_EVENT_COLS})
        SELECT {_EVENT_COLS} FROM (
            SELECT u.*, ROW_NUMBER() OVER (PARTITION BY u.session_id, u.message_id
                   ORDER BY u.total_tokens DESC, u.ts_epoch DESC, u.uuid) AS _rn
            FROM usage_events u)
        WHERE _rn = 1""")


def _old_keys(conn, uuids: list) -> set:
    """The (session_id, message_id) keys these uuids hold in usage_events right now."""
    keys = set()
    for i in range(0, len(uuids), 500):
        chunk = uuids[i:i + 500]
        q = (f"SELECT session_id, message_id FROM usage_events "
             f"WHERE uuid IN ({','.join('?' * len(chunk))})")
        keys.update(conn.execute(q, chunk).fetchall())
    return keys


def _ingest_one(conn, rel: str, path: Path, st, prev, full: bool, dirty: set) -> int:
    start = None
    if not full and prev is not None:
        start = _resume_offset(path, st.st_size, prev)
    if start is None:
        start = 0
    rows, new_off = scan(path, start, st.st_size, rel)
    if rows:
        # Every message whose winner can change: the new keys, and the keys these uuids
        # held before the upsert (an upsert may move a uuid to another message).
        dirty.update((r[1], r[3]) for r in rows)
        dirty.update(_old_keys(conn, [r[0] for r in rows]))
        conn.executemany(INSERT_SQL, rows)
    head_sha, tail_sha = _checksums(path, new_off)
    conn.execute(STATE_UPSERT_SQL, (rel, st.st_mtime_ns, st.st_size, len(rows), _now(),
                                    new_off, head_sha, tail_sha))
    return len(rows)


def ingest(full: bool = False) -> int:
    """sync (full=False) or build (full=True). One transaction, committed every COMMIT_EVERY
    changed transcripts. usage_dedup is refreshed for every message that changed."""
    conn = _connect_rw()
    files = changed = total_rows = n = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        if full:
            conn.execute("DELETE FROM usage_dedup")
            conn.execute("DELETE FROM ingest_state")
        state = {} if full else _load_state(conn)
        dirty = set()
        for rel, path in sources():
            files += 1
            try:
                st = path.stat()
            except OSError:
                continue
            prev = state.get(rel)
            if prev is not None and prev["mtime_ns"] == st.st_mtime_ns and prev["size"] == st.st_size:
                continue
            changed += 1
            total_rows += _ingest_one(conn, rel, path, st, prev, full, dirty)
            if changed % COMMIT_EVERY == 0:
                _flush_dirty(conn, dirty)
                conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
        _flush_dirty(conn, dirty)
        if full:
            _rebuild_dedup_all(conn)
        conn.execute("COMMIT")
        n = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"[usage-db] scanned {files} transcripts, {changed} changed, "
          f"{total_rows} rows upserted; {n} events in db")
    return 0


# Output CSVs: view -> file name. Same list and file names as usage_db.py CSV_EXPORTS.
CSV_EXPORTS = [
    ("v_hourly_ts",           "hourly.csv"),
    ("v_hourly_ts_by_model",  "hourly-by-model.csv"),
    ("v_daily",               "daily.csv"),
    ("v_daily_by_model",      "daily-by-model.csv"),
    ("v_weekly",              "weekly.csv"),
    ("v_weekly_by_model",     "weekly-by-model.csv"),
    ("v_monthly",             "monthly.csv"),
    ("v_monthly_by_model",    "monthly-by-model.csv"),
    ("v_by_model",            "models.csv"),
    ("v_by_project",          "projects.csv"),
    ("v_sessions_timeline",   "sessions.csv"),
    ("v_blocks_5h",           "blocks-5h.csv"),
    ("v_hourly",              "hour-of-day.csv"),
]


def cmd_export(reports_dir: Path | None = None) -> int:
    """Regenerate every CSV rollup from the views, atomically. Never called by sync."""
    out = reports_dir or REPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    # Single-writer lock: two runs cannot interleave partial CSVs.
    lock = out / ".export.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age < 600:
            print("[usage-db] export already in progress; skipping")
            return 0
        lock.unlink(missing_ok=True)          # stale lock from a killed run
        fd = os.open(str(lock), os.O_CREAT | os.O_WRONLY)
        os.close(fd)

    written = []
    try:
        conn = _connect_rw()
        try:
            for view, name in CSV_EXPORTS:
                try:
                    cur = conn.execute(f"SELECT * FROM {view}")
                except sqlite3.OperationalError as e:
                    print(f"[usage-db] skip {view}: {e}")
                    continue
                cols = [d[0] for d in cur.description]
                target = out / name
                tmp = out / (name + ".tmp")
                rows = 0
                try:
                    with open(tmp, "w", encoding="utf-8", newline="") as f:
                        w = csv.writer(f)
                        w.writerow(cols)
                        for row in cur:
                            w.writerow(["" if v is None else v for v in row])
                            rows += 1
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, target)   # atomic swap; readers never see a partial file
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                written.append((name, rows))
        finally:
            conn.close()
    finally:
        lock.unlink(missing_ok=True)

    total = sum(r for _, r in written)
    print(f"[usage-db] exported {len(written)} CSVs ({total} rows) -> {out}")
    for name, rows in written:
        print(f"    {name:26} {rows:>8} rows")
    return 0


def cmd_stats() -> int:
    conn = _connect_rw()
    try:
        # Deduplicated totals come from usage_dedup (one row per message).
        q = conn.execute("""
            SELECT COUNT(*), COUNT(DISTINCT session_id), COUNT(DISTINCT model),
                   COUNT(DISTINCT date), MIN(date), MAX(date),
                   SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_creation_tokens), SUM(cache_read_tokens),
                   SUM(total_tokens)
            FROM usage_dedup""").fetchone()
        cost = conn.execute(
            "SELECT ROUND(SUM(calc_cost_usd), 2) FROM v_events_costed").fetchone()[0]
        # The raw sum is over transcript lines. One message spans several lines that repeat
        # the same usage, so this figure is NOT a usage total. It is shown only to label it.
        raw_lines, raw_sum = conn.execute(
            "SELECT COUNT(*), SUM(total_tokens) FROM usage_events").fetchone()
    finally:
        conn.close()
    rows = [
        ("messages", q[0]), ("sessions", q[1]), ("models", q[2]),
        ("active days", q[3]), ("first day", q[4]), ("last day", q[5]),
        ("input tokens", q[6] or 0), ("output tokens", q[7] or 0),
        ("cache-creation tokens", q[8] or 0), ("cache-read tokens", q[9] or 0),
        ("TOTAL tokens (deduplicated per message)", q[10] or 0),
        ("est. cost (priced only)", "$" + format(cost or 0, ",.2f")),
        ("lines (transcript lines, raw)", raw_lines),
        ("raw per-line sum (NOT a usage total)", raw_sum or 0),
    ]
    for label, val in rows:
        shown = f"{val:,}" if isinstance(val, int) else str(val)
        print(f"  {label:<42} {shown:>18}")
    return 0


def _print_rows(conn, sql: str, params=()) -> None:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    print("\t".join(cols))
    for row in cur:
        print("\t".join("" if v is None else str(v) for v in row))


def show(sql: str, params=()) -> None:
    conn = _connect_rw()
    try:
        _print_rows(conn, sql, params)
    finally:
        conn.close()


def _show_ro(sql: str, params=()) -> None:
    """Read-only: the database is opened with mode=ro, so a write fails with sqlite3.Error."""
    conn = _connect_ro()
    try:
        _print_rows(conn, sql, params)
    finally:
        conn.close()


def main() -> int:
    argv = sys.argv
    mode = argv[1] if len(argv) > 1 else "sync"
    if mode == "build":
        return ingest(full=True)
    if mode == "sync":
        return ingest(full=False)
    if mode == "stats":
        return cmd_stats()
    if mode == "export":
        return cmd_export()
    if mode == "models":
        show("SELECT * FROM v_by_model"); return 0
    if mode == "weekly":
        show("SELECT * FROM v_weekly"); return 0
    if mode == "monthly":
        show("SELECT * FROM v_monthly"); return 0
    if mode == "hourly":
        n = int(argv[2]) if len(argv) > 2 else 48
        show("SELECT * FROM (SELECT * FROM v_hourly_ts ORDER BY hour_bucket DESC LIMIT ?) "
             "ORDER BY hour_bucket", (n,)); return 0
    if mode == "daily":
        n = int(argv[2]) if len(argv) > 2 else 30
        show("SELECT * FROM (SELECT * FROM v_daily ORDER BY date DESC LIMIT ?) ORDER BY date", (n,))
        return 0
    if mode == "sessions":
        n = int(argv[2]) if len(argv) > 2 else 40
        show("SELECT * FROM (SELECT * FROM v_sessions_timeline ORDER BY started DESC LIMIT ?) "
             "ORDER BY started", (n,)); return 0
    if mode == "sql":
        if len(argv) < 3:
            print('usage: usage_db_v2.py sql "SELECT ..."'); return 1
        q = argv[2]
        low = q.lstrip().lower()
        if not (low.startswith("select") or low.startswith("with")):
            print("refusing: only SELECT/WITH queries are allowed here"); return 1
        try:
            _show_ro(q)
        except sqlite3.Error as e:
            print(f"[usage-db] sql failed: {e}"); return 1
        return 0
    if mode == "schema":
        try:
            conn = _connect_ro()
            try:
                for (s,) in conn.execute(
                        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"):
                    print(s, ";\n")
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"[usage-db] schema failed: {e}"); return 1
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
