#!/usr/bin/env python3
"""
Usage DB — a durable, queryable SQLite store of every token-usage event.

Like `npx ccusage`, this turns Claude Code's JSONL transcripts into usage
statistics. Two deliberate differences:

  1. It reads from ~/.claude/session-archive/ (the append-only mirror), not
     from ~/.claude/projects/. ccusage reads the live directory, so anything
     Claude Code's retention sweep deletes -- or that a rewrite truncates --
     silently disappears from its reports. The archive keeps every line
     forever, so these numbers only ever get more complete.
  2. It persists one row per usage event instead of recomputing on each run.
     That means arbitrary SQL over your whole history, instantly, and rollups
     you can define yourself rather than the ones a CLI chose for you.

Storage: ~/.claude/session-archive/usage.db
  usage_events   one row per assistant message carrying token usage (uuid PK)
  model_pricing  per-model USD rates per 1M tokens; edit freely, costs are
                 computed in the views so changes apply retroactively
  ingest_state   per-file watermark so re-runs only read what changed

Views (query these, or write your own SQL):
  v_hourly_ts  v_daily  v_weekly  v_monthly          (time series)
  v_hourly_ts_by_model  v_daily_by_model
  v_weekly_by_model  v_monthly_by_model             (--breakdown equivalents)
  v_by_model  v_by_project  v_by_session
  v_sessions_timeline  v_blocks_5h  v_hourly

Commands:
  build            full rebuild from the archive (safe; upserts by uuid)
  sync             incremental -- only files whose mtime/size changed
  stats            headline totals
  models           per-model token+cost breakdown
  export           regenerate every CSV rollup (hourly/daily/weekly/monthly,
                   plain and per-model) into session-archive/reports/
  hourly [N]       last N clock hours (default 48)
  daily [N]        last N days (default 30)
  weekly           all weeks
  monthly          all months
  sessions [N]     session timeline: start, end, duration, tokens, cost
  sql "<query>"    run arbitrary read-only SQL, tab-separated output
  schema           print the schema and available views

No external deps -- stdlib sqlite3 only.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
ARCHIVE_DIR = CLAUDE_DIR / "session-archive"
TRANSCRIPTS_DIR = ARCHIVE_DIR / "transcripts"
PROJECTS_DIR = CLAUDE_DIR / "projects"
DB_PATH = ARCHIVE_DIR / "usage.db"

# USD per 1,000,000 tokens. Seeded with published Claude rates; anything not
# listed here (free/other-provider models routed through a proxy) costs 0 until
# you add a row. Costs live in the views, so edits apply to all history.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    uuid                  TEXT PRIMARY KEY,
    session_id            TEXT,
    request_id            TEXT,
    message_id            TEXT,
    ts_iso                TEXT,
    ts_epoch              INTEGER,
    date                  TEXT,      -- YYYY-MM-DD (UTC)
    month                 TEXT,      -- YYYY-MM
    hour                  INTEGER,   -- 0-23 (UTC)
    model                 TEXT,
    project               TEXT,      -- cwd
    git_branch            TEXT,
    cc_version            TEXT,
    is_sidechain          INTEGER,   -- 1 = subagent/workflow transcript
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
    cost_usd              REAL,      -- only if the transcript carried one
    source_file           TEXT
);
CREATE INDEX IF NOT EXISTS ix_ue_date    ON usage_events(date);
CREATE INDEX IF NOT EXISTS ix_ue_month   ON usage_events(month);
CREATE INDEX IF NOT EXISTS ix_ue_model   ON usage_events(model);
CREATE INDEX IF NOT EXISTS ix_ue_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS ix_ue_project ON usage_events(project);
CREATE INDEX IF NOT EXISTS ix_ue_epoch   ON usage_events(ts_epoch);

CREATE TABLE IF NOT EXISTS model_pricing (
    model_pattern   TEXT PRIMARY KEY,
    input_per_mtok  REAL NOT NULL DEFAULT 0,
    output_per_mtok REAL NOT NULL DEFAULT 0,
    cache_write_5m_per_mtok REAL NOT NULL DEFAULT 0,
    cache_write_1h_per_mtok REAL NOT NULL DEFAULT 0,
    cache_read_per_mtok     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source_file TEXT PRIMARY KEY,
    mtime_ns    INTEGER,
    size        INTEGER,
    rows_seen   INTEGER,
    ingested_at TEXT
);

-- Cost per event: longest matching pricing pattern wins; unpriced models = 0.
CREATE VIEW IF NOT EXISTS v_events_costed AS
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
FROM usage_events e;

CREATE VIEW IF NOT EXISTS v_daily AS
SELECT date,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY date ORDER BY date;

CREATE VIEW IF NOT EXISTS v_monthly AS
SELECT month,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY month ORDER BY month;

CREATE VIEW IF NOT EXISTS v_by_model AS
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
FROM v_events_costed GROUP BY model ORDER BY total_tokens DESC;

CREATE VIEW IF NOT EXISTS v_daily_by_model AS
SELECT date, model,
       COUNT(*) AS events,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY date, model ORDER BY date, total_tokens DESC;

CREATE VIEW IF NOT EXISTS v_by_session AS
SELECT session_id,
       MIN(ts_iso) AS first_activity,
       MAX(ts_iso) AS last_activity,
       COUNT(*) AS events,
       COUNT(DISTINCT model) AS models_used,
       MAX(project) AS project,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY session_id ORDER BY total_tokens DESC;

CREATE VIEW IF NOT EXISTS v_by_project AS
SELECT project,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE project IS NOT NULL AND project <> ''
GROUP BY project ORDER BY total_tokens DESC;

-- Distribution across hour-of-day (0-23), i.e. "when do I work".
CREATE VIEW IF NOT EXISTS v_hourly AS
SELECT hour,
       COUNT(*) AS events,
       SUM(total_tokens) AS total_tokens
FROM v_events_costed GROUP BY hour ORDER BY hour;

-- True hourly TIME SERIES (one row per real clock hour), for tracking.
CREATE VIEW IF NOT EXISTS v_hourly_ts AS
SELECT strftime('%Y-%m-%d %H:00', ts_iso) AS hour_bucket,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE ts_iso <> ''
GROUP BY hour_bucket ORDER BY hour_bucket;

CREATE VIEW IF NOT EXISTS v_hourly_ts_by_model AS
SELECT strftime('%Y-%m-%d %H:00', ts_iso) AS hour_bucket, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE ts_iso <> ''
GROUP BY hour_bucket, model ORDER BY hour_bucket, total_tokens DESC;

CREATE VIEW IF NOT EXISTS v_weekly AS
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
FROM v_events_costed WHERE date <> ''
GROUP BY week ORDER BY week;

CREATE VIEW IF NOT EXISTS v_weekly_by_model AS
SELECT strftime('%Y-W%W', date) AS week, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE date <> ''
GROUP BY week, model ORDER BY week, total_tokens DESC;

CREATE VIEW IF NOT EXISTS v_monthly_by_model AS
SELECT month, model,
       COUNT(*) AS events,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_creation_tokens) AS cache_creation_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed WHERE month <> ''
GROUP BY month, model ORDER BY month, total_tokens DESC;

-- Session lifecycle: when each session opened and closed, and what it cost.
CREATE VIEW IF NOT EXISTS v_sessions_timeline AS
SELECT session_id,
       MIN(ts_iso) AS started,
       MAX(ts_iso) AS ended,
       ROUND((MAX(ts_epoch) - MIN(ts_epoch)) / 60.0, 1) AS duration_min,
       MAX(project) AS project,
       COUNT(*) AS events,
       GROUP_CONCAT(DISTINCT model) AS models,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed GROUP BY session_id ORDER BY started;

-- Claude's rolling 5-hour billing windows, bucketed from the epoch.
CREATE VIEW IF NOT EXISTS v_blocks_5h AS
SELECT datetime((ts_epoch / 18000) * 18000, 'unixepoch') AS block_start,
       datetime(((ts_epoch / 18000) + 1) * 18000, 'unixepoch') AS block_end,
       COUNT(*) AS events,
       COUNT(DISTINCT session_id) AS sessions,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(calc_cost_usd), 4) AS cost_usd
FROM v_events_costed
GROUP BY ts_epoch / 18000 ORDER BY block_start DESC;
"""


def connect() -> sqlite3.Connection:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    # busy_timeout so a hook-triggered sync waits for a running one instead of
    # dying with "database is locked"; WAL keeps readers unblocked throughout.
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    for row in SEED_PRICING:
        conn.execute(
            "INSERT OR IGNORE INTO model_pricing VALUES (?,?,?,?,?,?)", row)
    conn.commit()
    return conn


def parse_ts(raw: str):
    """ISO-8601 -> (iso, epoch, date, month, hour) in UTC."""
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


def rows_from_file(path: Path, rel: str):
    """Yield one tuple per usage-bearing assistant message."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="surrogateescape")
    except Exception:
        return
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue

            uid = e.get("uuid") or ""
            if not uid:
                continue

            inp = int(u.get("input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
            cc = int(u.get("cache_creation_input_tokens") or 0)
            cr = int(u.get("cache_read_input_tokens") or 0)
            cache_detail = u.get("cache_creation")
            e5 = e1h = 0
            if isinstance(cache_detail, dict):
                e5 = int(cache_detail.get("ephemeral_5m_input_tokens") or 0)
                e1h = int(cache_detail.get("ephemeral_1h_input_tokens") or 0)

            iso, epoch, date, month, hour = parse_ts(e.get("timestamp", ""))

            # cost is absent in this corpus, but honour it if a future
            # transcript carries one under any of the known spellings
            cost = None
            for k in ("costUSD", "cost_usd", "costUsd"):
                if isinstance(e.get(k), (int, float)):
                    cost = float(e[k]); break

            yield (
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


# Placeholder count is derived from EVENT_COLUMNS, never hand-counted.
EVENT_COLUMNS = [
    "uuid", "session_id", "request_id", "message_id", "ts_iso", "ts_epoch",
    "date", "month", "hour", "model", "project", "git_branch", "cc_version",
    "is_sidechain", "service_tier", "speed", "effort", "stop_reason",
    "input_tokens", "output_tokens", "cache_creation_tokens",
    "cache_read_tokens", "ephemeral_5m_tokens", "ephemeral_1h_tokens",
    "total_tokens", "cost_usd", "source_file",
]

INSERT_SQL = """INSERT INTO usage_events VALUES
 (__PLACEHOLDERS__)
 ON CONFLICT(uuid) DO UPDATE SET
   session_id=excluded.session_id, request_id=excluded.request_id,
   message_id=excluded.message_id, ts_iso=excluded.ts_iso,
   ts_epoch=excluded.ts_epoch, date=excluded.date, month=excluded.month,
   hour=excluded.hour, model=excluded.model, project=excluded.project,
   git_branch=excluded.git_branch, cc_version=excluded.cc_version,
   is_sidechain=excluded.is_sidechain, service_tier=excluded.service_tier,
   speed=excluded.speed, effort=excluded.effort,
   stop_reason=excluded.stop_reason,
   input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
   cache_creation_tokens=excluded.cache_creation_tokens,
   cache_read_tokens=excluded.cache_read_tokens,
   ephemeral_5m_tokens=excluded.ephemeral_5m_tokens,
   ephemeral_1h_tokens=excluded.ephemeral_1h_tokens,
   total_tokens=excluded.total_tokens, cost_usd=excluded.cost_usd,
   source_file=excluded.source_file""".replace(
    "__PLACEHOLDERS__", ",".join("?" * len(EVENT_COLUMNS)))


def sources(include_live: bool = True):
    """(relative_key, absolute_path) for every transcript to ingest.

    The archive is authoritative. Live transcripts are also scanned so
    in-flight sessions show up before the next archive sync.
    """
    archived = {}
    if TRANSCRIPTS_DIR.exists():
        for p in TRANSCRIPTS_DIR.rglob("*.jsonl"):
            if p.is_file():
                rel = str(p.relative_to(TRANSCRIPTS_DIR))
                archived[rel] = p
                yield rel, p
    if include_live and PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.rglob("*.jsonl"):
            if not p.is_file() or "acompact" in str(p):
                continue
            rel = str(p.relative_to(PROJECTS_DIR))
            arch = archived.get(rel)
            if arch is not None:
                # The archive is a superset of the live file except for lines
                # written since the last archive sync. Re-reading a live file
                # no bigger than its archived copy can only yield rows we just
                # ingested, so skip it — this halves the scan.
                try:
                    if p.stat().st_size <= arch.stat().st_size:
                        continue
                except OSError:
                    continue
                yield rel + "|live", p
            else:
                yield rel, p


def ingest(full: bool = False) -> int:
    conn = connect()
    state = {}
    if not full:
        for r in conn.execute("SELECT source_file, mtime_ns, size FROM ingest_state"):
            state[r[0]] = (r[1], r[2])

    files = changed = total_rows = 0
    for rel, path in sources():
        files += 1
        try:
            st = path.stat()
        except OSError:
            continue
        if not full and state.get(rel) == (st.st_mtime_ns, st.st_size):
            continue
        changed += 1
        batch = list(rows_from_file(path, rel))
        if batch:
            conn.executemany(INSERT_SQL, batch)
            total_rows += len(batch)
        conn.execute(
            "INSERT INTO ingest_state VALUES (?,?,?,?,?) ON CONFLICT(source_file) "
            "DO UPDATE SET mtime_ns=excluded.mtime_ns, size=excluded.size, "
            "rows_seen=excluded.rows_seen, ingested_at=excluded.ingested_at",
            (rel, st.st_mtime_ns, st.st_size, len(batch),
             datetime.now(timezone.utc).isoformat()))
        if changed % 200 == 0:
            conn.commit()
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    print(f"[usage-db] scanned {files} transcripts, {changed} changed, "
          f"{total_rows} rows upserted; {n} events in db")
    conn.close()
    return 0


def show(sql: str, params=()):
    conn = connect()
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    print("\t".join(cols))
    for row in cur:
        print("\t".join("" if v is None else str(v) for v in row))
    conn.close()


# view -> output csv name. Each is REGENERATED WHOLESALE from the DB, never
# appended to. That is what makes overlaps and double-counting impossible:
# every usage event is one row keyed by uuid, so no matter how many times a
# transcript is re-scanned it is counted exactly once, and every CSV is a pure
# GROUP BY over that single source of truth.
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
    """Regenerate every CSV rollup from the DB, atomically."""
    import csv
    out = reports_dir or (ARCHIVE_DIR / "reports")
    out.mkdir(parents=True, exist_ok=True)

    # Single-writer lock: two hook fires can't interleave partial CSVs.
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
        lock.unlink(missing_ok=True)   # stale lock from a killed run
        fd = os.open(str(lock), os.O_CREAT | os.O_WRONLY)
        os.close(fd)

    try:
        conn = connect()
        written = []
        for view, name in CSV_EXPORTS:
            try:
                cur = conn.execute(f"SELECT * FROM {view}")
            except sqlite3.OperationalError as e:
                print(f"[usage-db] skip {view}: {e}")
                continue
            cols = [d[0] for d in cur.description]
            target = out / name
            tmp = target.with_suffix(".csv.tmp")
            rows = 0
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for row in cur:
                    w.writerow(["" if v is None else v for v in row])
                    rows += 1
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)     # atomic swap; readers never see a partial file
            written.append((name, rows))
        conn.close()
    finally:
        lock.unlink(missing_ok=True)

    total = sum(r for _, r in written)
    print(f"[usage-db] exported {len(written)} CSVs ({total} rows) -> {out}")
    for name, rows in written:
        print(f"    {name:26} {rows:>8} rows")
    return 0


def cmd_stats() -> int:
    conn = connect()
    q = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT session_id), COUNT(DISTINCT model),
               COUNT(DISTINCT date), MIN(date), MAX(date),
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_creation_tokens), SUM(cache_read_tokens),
               SUM(total_tokens)
        FROM usage_events""").fetchone()
    cost = conn.execute("SELECT ROUND(SUM(calc_cost_usd),2) FROM v_events_costed").fetchone()[0]
    labels = ["events", "sessions", "models", "active days", "first day", "last day",
              "input tokens", "output tokens", "cache-creation tokens",
              "cache-read tokens", "TOTAL tokens"]
    for label, val in zip(labels, q):
        if isinstance(val, int):
            print(f"  {label:24} {val:>18,}")
        else:
            print(f"  {label:24} {str(val):>18}")
    print(f"  {'est. cost (priced only)':24} {('$' + format(cost or 0, ',.2f')):>18}")
    conn.close()
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "build":
        return ingest(full=True)
    if mode == "sync":
        return ingest(full=False)
    if mode == "stats":
        return cmd_stats()
    if mode == "export":
        return cmd_export()
    if mode == "weekly":
        show("SELECT * FROM v_weekly"); return 0
    if mode == "hourly":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 48
        show("SELECT * FROM (SELECT * FROM v_hourly_ts ORDER BY hour_bucket DESC LIMIT ?) "
             "ORDER BY hour_bucket", (n,)); return 0
    if mode == "monthly":
        show("SELECT * FROM v_monthly"); return 0
    if mode == "sessions":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
        show("SELECT * FROM (SELECT * FROM v_sessions_timeline ORDER BY started DESC LIMIT ?) "
             "ORDER BY started", (n,)); return 0
    if mode == "models":
        show("SELECT * FROM v_by_model"); return 0
    if mode == "daily":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        show("SELECT * FROM (SELECT * FROM v_daily ORDER BY date DESC LIMIT ?) ORDER BY date", (n,))
        return 0
    if mode == "sql":
        if len(sys.argv) < 3:
            print("usage: usage_db.py sql \"SELECT ...\""); return 1
        q = sys.argv[2]
        low = q.lstrip().lower()
        if not (low.startswith("select") or low.startswith("with")):
            print("refusing: only SELECT/WITH queries are allowed here"); return 1
        show(q); return 0
    if mode == "schema":
        conn = connect()
        for (s,) in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"):
            print(s, ";\n")
        conn.close(); return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
