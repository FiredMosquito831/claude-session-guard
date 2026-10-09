# Changelog

## Unreleased (staged, not wired into hooks)

These changes are in the repository and tested. The hooks in `hooks/hooks.json` still run the 1.4.0 scripts. The new modules are wired only after a separate, approved change to the settings.

- `scripts/guardkit.py`: shared helpers. Lock with a takeover marker (stale locks are taken over under that marker), binary atomic writes, raw line reader, a checkpointed clean-file cache, the shared locked writer for the removed-lines archive, and a payload reader.
- `scripts/api_repair_v2.py`: repair with the write order fixed (concurrent-append check before backup, archive and replace), an idempotent lossless archive, and a SessionEnd guard that skips a session still running.
- `scripts/session_archive_v2.py`: an append-only mirror with per-transcript offsets, binary writes, and a crash path that quarantines bytes before truncating. Restore never rewrites a live file without `--merge-live`. The canary classes missing lines as excused, excused by unreadable record, or unexplained, and exits 1 when any are unexplained.
- `scripts/session_indexer_v2.py`: prompt index merge that only appends, under a lock, and refuses a rewritten history. `register` uses the SessionStart payload.
- `scripts/usage_db_v2.py`: incremental usage store with a `usage_dedup` table, export separate from sync, and read-only SQL.
- `scripts/jsonl_repair_v2.py`: archive before replace, exact bytes, blank lines archived, subagent transcripts included, incremental sweep on a pool of 8 with a 6 h / 4 per 24 h gate.
- `scripts/guard_hook.py`: one dispatcher per hook event, with per-step deadlines and detached repair sweeps.
- `scripts/run.sh`: caches the interpreter path, so a hook call starts Python once on a cache hit.
- `tools/deploy.py`: copies an allowlist of scripts to a target folder. Dry run by default; runs the tests first.
- `scripts/guard_hook.py` and `scripts/api_repair_v2.py`: the SessionEnd repair reads the documented `reason` field, skips `resume`, and falls back to `end_reason`. The live v1 tool (`scripts/api_repair.py`) still reads `end_reason` until it is replaced.

Known limits: a rewrite confined to the middle of an indexed prefix is not detected; the 2026-10-07 and 2026-10-09 usage totals are not reconciled; prices are unverified; the skills (`usage-report`, `session-doctor`) describe the 1.4.0 tools until the v2 tools are wired.

### Changed

- Plugin renamed to `session-guard`. The name `claude-session-guard` is reserved by Claude Code plugin validation, and `claude plugin validate . --strict` failed on it. Install with `/plugin install session-guard@claude-session-guard`. Manifests set to 1.4.2 in this batch.

### Tests and tools

- `tools/deploy.py`: each `--apply` writes a manifest of the deployed hashes, beside the backup root. A read-only `--check` compares the repo copies with a target folder and exits 1 when any copy differs or is missing.
- `tests/test_api_v2.py`: exits 1 when any check fails, so the deploy gate can see a failure. Its real-transcript check reads only transcripts older than one hour and strips one trailing CR from each line before it compares the two outputs. A new check confirms that untouched CRLF lines keep their CR.
- `tests/test_guardkit.py`: pins the accepted limit that a same-size rewrite with a restored mtime is reported clean (QUEUE K2).
- `tests/test_guard_hook_e2e_pr18.py`: an end-to-end test that runs `scripts/guard_hook.py` as a child process for each documented hook payload and reads back the results.
- `tools/deploy.py`: the gate also runs `tests/*.sh` with `sh`, and refuses when a `.sh` test exists and no `sh` program is found. A test fails when its output has a line starting with `FAIL`, even if its exit code is 0. `tests/test_deploy.py` checks each case with fixture repos.

## 1.4.0

- Renamed to **claude-session-guard** (was session-vault); the `vault-doctor`
  skill is now `session-doctor`.
- Corrected a documentation error: Claude Code's `--resume` picker searches
  every project on the machine, and `claude --resume <id>` works from any
  directory. Only `--continue` is scoped to the current directory. Earlier docs
  claimed the picker was directory-scoped, which overstated what `session_find`
  is for -- its real value is full-text prompt search and live/archived/gone
  status, not reaching sessions the picker cannot see.

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
- Skills: `usage-report`, `session-doctor`.
