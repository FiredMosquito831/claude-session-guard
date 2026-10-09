#!/usr/bin/env sh
# Portable launcher: find a Python 3 interpreter, then run one of the bundled
# tools. Hooks call this rather than `python` directly, because the interpreter
# name differs across platforms (python3 on most Unix, python on Windows) and a
# hook that cannot find its interpreter fails silently.
#
#   run.sh <tool> [args...]        e.g.  run.sh usage_db sync
#
# Exits 0 even when the tool fails: a retention/analytics hook must never block
# or slow a session because of its own error. Problems surface in the tool's
# own output and in `session-doctor`.
#
# The first interpreter that passes the version check is cached as its absolute
# path, in ${CLAUDE_PLUGIN_DATA} (or in ${TMPDIR:-/tmp} when that is unset).
# Later calls use the cached path without probing, which saves one interpreter
# start per hook call. If the cached path is not executable, the cache file is
# deleted and the probe runs again.

set -u

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TOOL="${1:-}"
[ -n "$TOOL" ] || { echo "usage: run.sh <tool> [args...]" >&2; exit 0; }
shift

SCRIPT="$DIR/$TOOL.py"
[ -f "$SCRIPT" ] || { echo "claude-session-guard: no such tool: $TOOL" >&2; exit 0; }

if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    CACHE="$CLAUDE_PLUGIN_DATA/claude-session-guard-run-cache"
else
    CACHE="${TMPDIR:-/tmp}/claude-session-guard-run-cache"
fi

PY=""
if [ -f "$CACHE" ]; then
    cached=""
    IFS= read -r cached 2>/dev/null < "$CACHE" || true
    if [ -n "$cached" ] && [ -x "$cached" ]; then
        PY="$cached"
    else
        rm -f "$CACHE"
    fi
fi

if [ -z "$PY" ]; then
    for c in python3 python py; do
        if command -v "$c" >/dev/null 2>&1; then
            # confirm it is actually Python 3
            if "$c" -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' >/dev/null 2>&1; then
                PY="$(command -v "$c")"
                break
            fi
        fi
    done
    if [ -n "$PY" ]; then
        # Write to a temp file and rename it, so a reader never sees a partial path.
        # A failed write only costs one more probe on the next call.
        { mkdir -p "$(dirname -- "$CACHE")" &&
          printf '%s\n' "$PY" > "$CACHE.$$" &&
          mv -f "$CACHE.$$" "$CACHE"; } 2>/dev/null || rm -f "$CACHE.$$" 2>/dev/null
    fi
fi

if [ -z "$PY" ]; then
    echo "claude-session-guard: no Python 3 interpreter found; skipping $TOOL" >&2
    exit 0
fi

"$PY" "$SCRIPT" "$@" || true
exit 0
