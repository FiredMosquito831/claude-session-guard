# Session Vault

Lifetime retention and usage analytics for Claude Code.

Claude Code deletes session transcripts once they age past `cleanupPeriodDays`
(default 30), and any tool that rewrites a transcript in place can truncate it.
When that happens you lose the conversation *and* its token accounting, and
nothing tells you it happened.

Session Vault does three things:

1. **Keeps everything.** An append-only mirror of every transcript that only
   ever grows — including subagent and workflow transcripts.
2. **Repairs without destroying.** A JSONL repair pass that fixes genuinely
   broken lines and is structurally incapable of deleting good ones.
3. **Makes usage queryable.** A SQLite store of every token-usage event, plus
   continuously-refreshed CSV rollups — hourly, daily, weekly, monthly, each
   with a per-model breakdown.

## Install

```bash
/plugin marketplace add eduard-secureanu/session-vault
/plugin install session-vault@session-vault
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
- **vault-doctor** — "is anything deleting my session data?", "my old sessions
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

$R session_archive verify    # is anything eating session data?
$R session_archive restore   # put lost sessions back
$R jsonl_repair --all --dry-run
```

## Why the numbers are trustworthy

Every usage event is one row keyed by its message `uuid`, which is the table's
primary key. Re-scanning a transcript upserts the same row, so **an event can
never be counted twice** no matter how often ingest runs. Every rollup and CSV
is a plain `GROUP BY` regenerated wholesale from that one table — never
appended to — so periods cannot overlap or drift apart.

That is checkable, not just claimed:

```
events table          85,192,489,088   <-- reference
v_hourly_ts           85,192,489,088   delta +0
v_daily               85,192,489,088   delta +0
v_weekly              85,192,489,088   delta +0
v_monthly             85,192,489,088   delta +0
v_by_model            85,192,489,088   delta +0
v_blocks_5h           85,192,489,088   delta +0
697,072 rows = 697,072 distinct uuids
```

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
  reports. Session Vault reads its own archive, so its numbers only ever get
  more complete.
- **Persistence.** ccusage recomputes on each run. Session Vault keeps a SQLite
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
