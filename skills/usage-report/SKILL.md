---
name: usage-report
description: Report Claude Code token usage and cost from the Claude Session Guard database — hourly, daily, weekly, or monthly, with an optional per-model breakdown, plus arbitrary SQL over the full history. Use when the user asks how many tokens they have used, what Claude Code is costing them, which model or project consumes the most, when they are most active, or asks for usage stats, a usage report, a breakdown by model, or a CSV export of their usage.
---

# Usage Report

Answers questions about Claude Code token usage from the Claude Session Guard SQLite
database, which holds one row per usage-bearing assistant message across the
user's entire history.

The database lives at `~/.claude/session-archive/usage.db`. All commands below
are run through the bundled launcher:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" usage_db <command>
```

## Choosing a command

| User asks | Command |
|---|---|
| Overall totals | `stats` |
| "last N hours" | `hourly [N]` (default 48) |
| "last N days", "this week" | `daily [N]` (default 30) |
| "by week" | `weekly` |
| "by month", "this month" | `monthly` |
| "which model costs most" | `models` |
| "when did each session run" | `sessions [N]` |
| Anything else | `sql "SELECT ..."` |
| "export to CSV" | `export` |
| "find/list my sessions", "which session was X" | `session_find` (see session-doctor) |

Run `sync` first only if the user suspects the data is stale — the Stop hook
already refreshes it after every session.

## Arbitrary questions: use SQL

Most real questions are not one of the canned reports. Query the views directly
rather than post-processing canned output. `sql` accepts only `SELECT`/`WITH`.

Available views:

- `v_hourly_ts`, `v_daily`, `v_weekly`, `v_monthly` — time series
- `v_hourly_ts_by_model`, `v_daily_by_model`, `v_weekly_by_model`,
  `v_monthly_by_model` — the same, broken down by model (`--breakdown`)
- `v_by_model`, `v_by_project`, `v_by_session` — totals by dimension
- `v_sessions_timeline` — start, end, duration, models, tokens, cost per session
- `v_blocks_5h` — Claude's rolling 5-hour billing windows
- `v_hourly` — distribution across hour-of-day (0–23)
- `v_events_costed` — every raw event with its computed cost

Every view carries `total_tokens` and `cost_usd`; the period views also carry
`input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`.

Examples:

```sql
-- spend per model this month
SELECT model, total_tokens, cost_usd FROM v_monthly_by_model
WHERE month = strftime('%Y-%m','now') ORDER BY cost_usd DESC;

-- cache hit ratio by day
SELECT date, ROUND(100.0*cache_read_tokens/NULLIF(total_tokens,0),1) AS cache_pct
FROM v_daily ORDER BY date DESC LIMIT 14;

-- most expensive individual sessions
SELECT session_id, started, duration_min, total_tokens, cost_usd
FROM v_sessions_timeline ORDER BY cost_usd DESC LIMIT 10;
```

## Reporting results

Give the user the numbers, not the command you ran. Format large token counts
with thousands separators. When a cost is $0 for a model, say so explicitly and
explain why rather than implying the model was free to run — see below.

## Cost accuracy — state this when it matters

Costs are **computed from token counts**, not read from the transcripts: Claude
Code does not record a `costUSD` field in these logs. Rates live in the
`model_pricing` table (USD per 1M tokens) and are applied in the views, so
editing a rate retroactively corrects all history:

```sql
INSERT OR REPLACE INTO model_pricing VALUES ('some-model', 3.0, 15.0, 3.75, 6.0, 0.30);
```

Any model with no matching row costs `0`. That covers free-tier and
third-party models routed through a proxy. A `$0` figure therefore means
"unpriced", not "free" — tell the user which models are unpriced when it
affects the answer, and offer to add rates.

Timestamps are UTC.
