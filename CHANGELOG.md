# Changelog

## 1.3.0

- **api_repair `from-hook`** — repair driven by the **SessionEnd** hook, which
  passes `transcript_path` on stdin. That is the one moment a transcript is
  provably finished, so no timestamp heuristic is needed and a session is clean
  the instant it closes. Skips `end_reason: resume` (that transcript is about
  to be reopened). The TOCTOU guard still applies.
- **Process-based liveness** — the sweep now also excludes sessions named by a
  running `claude` process, rather than relying on file age alone.
- Liveness guards no longer apply to `scan`, which never writes.

## 1.2.0

- **session_find** (new) — browse and search every session across every folder.
  Claude Code's resume picker is scoped to the current directory's project, so
  sessions started elsewhere are otherwise invisible. Merges history.jsonl, the
  live transcripts and the archive; marks each session live / archived-only /
  gone; prints the `claude --resume <id>` command (which works from any
  directory). Forces UTF-8 output so Unicode in prompts cannot crash the
  listing on a cp1252 Windows console.

## 1.1.0

- **api_repair** (new) — repairs transcripts that are valid JSON but that the
  Messages API rejects on resume: an assistant line whose only content is an
  empty/whitespace thinking block, which makes `claude --resume` fail with
  "each thinking block must contain non-whitespace thinking". Drops only the
  empty block (or the line, when that is all it holds), re-links the
  `parentUuid` chain, archives everything it removes, and refuses to touch a
  live session. Wired to SessionStart and PreCompact.
- **usage_db** — FIXED a significant over-count. Claude Code splits one
  assistant response across several transcript lines and repeats the same
  `usage` object on each, so summing lines inflated every total by ~2x. All
  rollups now read `v_events_dedup`, one row per `(session_id, message_id)`,
  keeping the most complete row. Period views no longer filter any rows, so
  every rollup reconciles exactly.
- **session_archive verify** — the "live files missing archived lines" alarm
  now excuses lines a repair tool removed deliberately (they stay in the
  archive by design), so the canary keeps meaning something.

## 1.0.0

Initial release.

- **session_archive** — append-only mirror of the full `projects/` tree,
  including subagent and workflow transcripts. Merges on line identity, so the
  archive only ever grows. `verify` audits archive vs live; `restore` puts lost
  sessions back so they are resumable again.
- **jsonl_repair** — non-destructive JSONL repair. Preserves unparseable lines,
  round-trips bytes losslessly, skips live files, aborts on concurrent
  modification, writes atomically, archives every removed line first.
- **session_indexer** — rebuilds `history.jsonl` per prompt rather than per
  session, so multi-prompt sessions are not collapsed. Refuses any merge that
  would drop a distinct entry.
- **usage_db** — SQLite store of every token-usage event (uuid primary key, so
  double-counting is structurally impossible), with hourly/daily/weekly/monthly
  views, per-model breakdowns, session timelines, 5-hour billing blocks, and
  editable per-model pricing applied inside the views.
- CSV rollups regenerated wholesale under a lock, written atomically.
- Skills: `usage-report`, `vault-doctor`.
