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
# own output and in `vault-doctor`.

set -u

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TOOL="${1:-}"
[ -n "$TOOL" ] || { echo "usage: run.sh <tool> [args...]" >&2; exit 0; }
shift

SCRIPT="$DIR/$TOOL.py"
[ -f "$SCRIPT" ] || { echo "session-vault: no such tool: $TOOL" >&2; exit 0; }

PY=""
for c in python3 python py; do
    if command -v "$c" >/dev/null 2>&1; then
        # confirm it is actually Python 3
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' >/dev/null 2>&1; then
            PY="$c"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "session-vault: no Python 3 interpreter found; skipping $TOOL" >&2
    exit 0
fi

"$PY" "$SCRIPT" "$@" || true
exit 0
