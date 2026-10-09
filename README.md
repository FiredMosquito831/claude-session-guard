# Claude Session Guard

Lifetime retention and usage analytics for Claude Code.

Claude Code deletes session transcripts once they age past `cleanupPeriodDays`
(default 30), and any tool that rewrites a transcript in place can truncate it.
When that happens you lose the conversation *and* its token accounting, and
nothing tells you it happened.

Claude Session Guard does three things:

1. **Keeps everything.** An append-only mirror of every transcript that only
   ever grows — including subagent and workflow transcripts.
2. **Repairs without destroying.** Two repair passes — one for malformed JSONL,
   one for transcripts the API rejects on resume — both structurally incapable
   of deleting good data.
3. **Makes usage queryable.** A SQLite store of every token-usage event, plus
   continuously-refreshed CSV rollups — hourly, daily, weekly, monthly, each
   with a per-model breakdown.

## Status

The plugin's manifests are at version 1.4.2. Its changes are listed under "Unreleased" in CHANGELOG.md, which has no 1.4.2 section yet. The v2 tools (archive, repair, index, usage store and the hook dispatcher) are in this repository and tested. They are described under "Unreleased" in CHANGELOG.md.

The hooks in `hooks/hooks.json` still run the 1.4.0 tools. The v2 tools are wired into the hooks only in a later, separate step. Nothing on a user's machine changes until the user installs the plugin or makes that change themselves.

## Install

```bash
/plugin marketplace add FiredMosquito831/claude-session-guard
/plugin install session-guard@claude-session-guard
```

Then build the initial archive and database (one time, a few minutes on a large
history):

```bash
sh "$CLAUDE_PLUGIN_ROOT/scripts/run.sh" session_archive sync --full
sh "$CLAUDE_PLUGIN_ROOT/scripts/run.sh" usage_db build
sh "$CLAUDE_PLUGIN_ROOT/scripts/run.sh" usage_db export
```

After that the hooks keep it current on their own.

Requires Python 3 (stdlib only — no pip install, no dependencies).

## What you get

```
~/.claude/session-archive/
├── transcripts/          append-only mirror of the whole projects/ tree
├── usage.db              SQLite: one row per usage event
├── usage-ledger.jsonl    append-only usage rows (belt and braces)
└── reports/
    ├── hourly.csv          hourly-by-model.csv
    ├── daily.csv           daily-by-model.csv
    ├── weekly.csv          weekly-by-model.csv
    ├── monthly.csv         monthly-by-model.csv
    ├── sessions.csv        start, end, duration, models, tokens, cost
    ├── models.csv          projects.csv
    ├── blocks-5h.csv       Claude's rolling 5-hour billing windows
    └── hour-of-day.csv     when you actually work
```

## Skills

Ask in plain language — the skills trigger on their own:

- **usage-report** — "how many tokens did I use this week?", "which model is
  costing me the most?", "break down my usage by model this month"
- **session-doctor** — "is anything deleting my session data?", "my old sessions
  disappeared", "restore my lost sessions"

## CLI

```bash
R="sh $CLAUDE_PLUGIN_ROOT/scripts/run.sh"

$R usage_db stats            # headline totals
$R usage_db hourly 48        # last 48 clock hours
$R usage_db daily 30         # last 30 days
$R usage_db weekly           # all weeks
$R usage_db monthly          # all months
$R usage_db models           # per-model tokens + cost
$R usage_db sessions 40      # session timeline
$R usage_db export           # regenerate every CSV
$R usage_db sql "SELECT ..." # arbitrary read-only SQL

$R session_find              # browse/search sessions across ALL folders
$R session_find docker       # search prompts everywhere
$R session_archive verify    # is anything eating session data?
$R session_archive restore   # put lost sessions back
$R jsonl_repair --all --dry-run
```

## Resuming old sessions

Claude Code writes one assistant response as several transcript lines that
share a `message.id`, and merges them back on replay. If any of those lines
holds a thinking block whose text is empty or whitespace, resuming the session
dies with:

    API Error: 400 messages.N.content.0.thinking:
    each thinking block must contain non-whitespace thinking

`api_repair` fixes exactly this. It runs at **SessionEnd**, repairing the
transcript at the one moment it is provably not being written to — so a session
is clean the instant it closes, and there is no window in which resuming it
fails. A SessionStart sweep catches anything missed (a crash, a session that
predates the plugin). It removes only the empty block (or the line, if
that is all the line holds), re-links the `parentUuid` chain, and archives
whatever it removes. Nothing else is touched.

```bash
$R api_repair scan              # report only, never writes
$R api_repair fix <session-id>  # repair one session
$R api_repair fix --all         # repair everything eligible
```

## Finding sessions across folders

Claude Code's own `--resume` picker already searches every project on the
machine, and `claude --resume <id>` works from any directory. (`--continue` is
the directory-scoped one.) `session_find` is for the things the picker does not
do: full-text search across every prompt you have ever typed, and telling you
whether a session is still **live**, only in the **archive**, or **gone**
entirely — so you know whether a resume will work before you try.

```bash
$R session_find                  # 20 most recent, all folders
$R session_find "auth bug"       # search prompt text everywhere
$R session_find --project Beam   # restrict to a project
$R session_find --all            # everything
```

## Why the numbers are trustworthy

Two separate de-duplications are needed, and both are enforced:

1. **Per line.** Each transcript line is one row keyed by its `uuid`, the
   table's primary key, so re-scanning a transcript upserts rather than
   appends — ingest can run any number of times without inflating anything.
2. **Per message.** Claude Code splits one assistant API response across
   several lines (thinking, then text, then each tool_use) and repeats the
   *same* `usage` object on every one of them. Summing lines therefore
   over-counts badly — measured at **+50.7%** on a real corpus. Every rollup
   reads `v_events_dedup`, which keeps one row per `(session_id, message_id)`,
   choosing the row with the largest token total because the usage object grows
   as the response streams.

Miss the second and your totals are roughly 1.5x reality. Every rollup and CSV
is a plain `GROUP BY` over the deduplicated view, regenerated wholesale — never
appended to — so periods cannot overlap or drift apart.

That is checkable, not just claimed:

```
v_events_dedup (ref)     45,038,824,445   delta +0
v_hourly_ts              45,038,824,445   delta +0
v_daily                  45,038,824,445   delta +0
v_weekly                 45,038,824,445   delta +0
v_monthly                45,038,824,445   delta +0
v_by_model               45,038,824,445   delta +0
v_blocks_5h              45,038,824,445   delta +0
v_sessions_timeline      45,038,824,445   delta +0

729,912 transcript lines  ->  339,927 logical messages
naive per-line total: 91,341,052,299  (2.03x the truth)
```

No period view filters rows out, so nothing can be silently dropped from a
rollup — every one reconciles to the same grand total.

CSVs are written to a temp file and atomically renamed, under a lock, so a
reader never sees a half-written file and two hook fires cannot interleave.

## Costs

Claude Code does **not** record a `costUSD` field in these transcripts, so cost
is computed from token counts, like ccusage's `calculate` mode. Rates live in
the `model_pricing` table (USD per 1M tokens) and are applied inside the views,
so changing a rate retroactively corrects all history:

```sql
INSERT OR REPLACE INTO model_pricing
VALUES ('model-name', input, output, cache_write_5m, cache_write_1h, cache_read);
```

A model with no matching row costs `0`. **`$0` means "unpriced", not "free"** —
that is how free-tier and proxied third-party models appear until you add rates.

## Compared to ccusage

[ccusage](https://github.com/ccusage/ccusage) is excellent and covers more
report formats out of the box. Two deliberate differences here:

- **Source.** ccusage reads `~/.claude/projects/` — the live directory. Anything
  the retention sweep deleted, or a rewrite truncated, is simply absent from its
  reports. Claude Session Guard reads its own archive, so its numbers only ever get
  more complete.
- **Persistence.** ccusage recomputes on each run. Claude Session Guard keeps a SQLite
  table, so you get arbitrary SQL over your whole history instantly and can
  define your own rollups instead of the ones a CLI chose.

Use both. They answer different questions.

## Safety properties of the repair pass

`jsonl_repair` fixes genuinely malformed JSONL. It cannot destroy data:

- Unparseable lines are **preserved verbatim**, never deleted.
- Reads and writes use `errors='surrogateescape'`, so arbitrary bytes round-trip
  instead of being mangled into `U+FFFD` (which breaks the JSON, which is how
  such tools usually end up deleting the line).
- Live and recently-written transcripts are skipped — rewriting a file Claude
  Code is appending to truncates it out from under the writer.
- If a file changes between the scan and the write, the rewrite is **aborted**.
- Writes are atomic (temp + fsync + rename).
- Every removed line is archived before the rewrite, and the rewrite is
  abandoned if archiving fails.
- A full backup is always taken. There is no `--no-backup` option.

Only exact-duplicate and control-character-only lines are ever removed, and both
are recoverable from `backups/sessions/removed-lines-archive.jsonl`.

## Disk

The archive is a full copy of your transcripts and grows with them (a large
history runs to several GB). Hardlinks are deliberately not used: they would
share storage with the live file, so a truncation would destroy both copies.

## License

MIT
