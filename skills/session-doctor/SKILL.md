---
name: session-doctor
description: Diagnose Claude Session Guard health — check that the transcript archive is in sync with live sessions, detect whether anything is deleting or truncating Claude Code session data, verify the usage database, and restore missing sessions from the archive. Use when the user suspects session data or usage/token history is being lost, pruned, or corrupted, when sessions have disappeared from the resume list, or when they ask whether their Claude Code data is safe.
---

# Session Doctor

Diagnoses whether Claude Code session data is being lost, and repairs it from
the archive when it has been.

Run tools through the bundled launcher:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" <tool> <command>
```

## 1. The one number that matters

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_archive verify
```

Read the line **"live files missing archived lines"**.

- `0` — nothing is eating session data.
- Anything above `0` — **something is actively destroying transcripts.** Each
  count is a live transcript that has fewer lines than its archived copy. This
  is the regression detector; treat a non-zero value as a real incident, not a
  glitch.

Also reported: sessions present only in the archive. Those are sessions whose
live file was deleted — normally by Claude Code's own `cleanupPeriodDays`
retention sweep, not by a hook. They are recoverable (step 3).

`verify` reads every archived and live file pair, so it takes a few minutes on
a large corpus. It is an on-demand audit, never a hook.

## 2. Find what is doing the damage

If the count is non-zero, investigate in this order:

1. **Any tool that rewrites transcripts in place.** Rewriting a file Claude
   Code is appending to truncates it out from under the writer. This is the
   single most destructive pattern and the usual culprit. Check every hook in
   `~/.claude/settings.json` and `settings.local.json` for scripts that open a
   session `.jsonl` for writing.
2. **Deletion on parse failure.** A "repair" tool that drops lines it cannot
   parse will destroy assistant messages and their token accounting. Reading
   with `errors='replace'` causes this: it mangles bytes into U+FFFD, which
   breaks the JSON, which gets the line deleted.
3. **`cleanupPeriodDays`** in settings.json. It is `integer, minimum 1` — there
   is no "never" value and `0` is rejected. Unset means 30 days. Raise it, but
   understand it can only delay deletion; the archive is what makes retention
   permanent.

The bundled `jsonl_repair` is safe by construction: it preserves unparseable
lines verbatim, skips live and recently-written files, writes atomically,
aborts if a file changes mid-scan, and archives every removed line before
rewriting. If you find a *different* repair tool, that is the suspect.

## 2b. "Each thinking block must contain non-whitespace thinking"

If a resume fails with:

    API Error: 400 messages.N.content.0.thinking:
    each thinking block must contain non-whitespace thinking

that is a DIFFERENT fault from anything `jsonl_repair` handles. The line is
valid JSON; it is the Messages API that rejects it. Claude Code writes one
assistant response as several transcript lines sharing a `message.id` and
merges them on replay, so an empty thinking block on any of those lines poisons
the whole conversation.

    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" api_repair scan
    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" api_repair fix <session-id>
    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" api_repair fix --all

`scan` never writes. `fix` drops only the empty block -- or the whole line when
the block is all the line holds -- re-links `parentUuid` so the chain stays
connected, and archives everything it removes first. Token accounting is
unaffected: the sibling lines of the same message carry the same `usage`.

A live session is refused (recent mtime, or a matching `CLAUDE_SESSION_ID`).
Close the session before repairing it.

## 3. Restore what was lost

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_archive restore
```

Copies archived-only sessions back into `~/.claude/projects/` and merges
archived lines back into any live file that lost some. Restored sessions become
resumable again.

To make them appear in the resume picker, rebuild the prompt index:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_indexer scan --full
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_indexer merge
```

Restored files must land in the correct project directory or they will not be
resumable. The directory name is the session's `cwd` with **every character
outside `[A-Za-z0-9-]` replaced by `-`**. Verify against existing directory
names rather than assuming; a hand-written character-class regex that tries to
list the separators is a common way to get this wrong and silently create a
nested tree.

## 3b. Find a session in ANY folder

Claude Code's `--resume` picker already spans every project on the machine, and
`claude --resume <id>` works from any directory; only `--continue` is scoped to
the current directory. Use `session_find` when you need what the picker does
not give you — full-text search over every prompt, and the live/archived/gone
status of a session:

    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_find              # recent
    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_find <text>       # search prompts
    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_find --project X  # one project
    sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" session_find --all

Each entry is marked `*` live (resume right now, the command is printed),
`~` archived only (run `session_archive restore` first), or `!` no transcript
anywhere -- the prompts survive in history.jsonl but the conversation itself
predates the archive and is gone.

`claude --resume <session-id>` works from any directory, so once you have the
id you do not need to cd anywhere. A session marked `!` has no transcript left
and cannot be resumed by any means -- only its prompts survive.

## 4. Check the analytics store

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" usage_db stats
```

If event counts look short, re-ingest with `usage_db sync` (incremental) or
`usage_db build` (full re-scan). Both upsert on event `uuid`, so re-running is
always safe and can never double-count.

## Reporting

Lead with the verdict — is data being lost, yes or no — then the evidence.
If you restored anything, say exactly how many sessions and lines came back.
Do not describe Session Guard as "safe" on the strength of a `verify` you did not
actually run.
