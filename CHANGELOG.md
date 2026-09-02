# Changelog

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
