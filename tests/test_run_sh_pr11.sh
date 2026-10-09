#!/bin/sh
# Tests for scripts/run.sh: the interpreter cache (PR-11).
#
# The test runs a copy of run.sh with a dummy tool, a fake python3 that counts
# its own starts, and temporary folders only. It never runs a plugin tool and
# never touches the live plugin data or the user's home folder.
#
# Run from the repository root:  sh tests/test_run_sh_pr11.sh
# Prints PASS or FAIL per check. Exits 1 if any check fails.

set -u

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUN_SRC="$ROOT/scripts/run.sh"
SH="$(command -v sh)"
CACHE_NAME="claude-session-guard-run-cache"
NOPY_PATH="/usr/bin:/bin"
FAILED=0

T="$(mktemp -d)" || { echo "FAIL setup: mktemp -d failed"; exit 1; }
trap 'rm -rf "$T"' EXIT

RUNDIR="$T/plugin/scripts"
FAKEBIN="$T/fakebin"
COUNT="$T/starts.count"
mkdir -p "$RUNDIR" "$FAKEBIN" "$T/tmp" || { echo "FAIL setup: mkdir failed"; exit 1; }
cp "$RUN_SRC" "$RUNDIR/run.sh" || { echo "FAIL setup: cannot copy $RUN_SRC"; exit 1; }
: > "$RUNDIR/dummy.py"

cat > "$FAKEBIN/python3" <<'SGFAKE'
#!/bin/sh
# Fake interpreter: records one line per start, then succeeds.
echo start >> "$FAKE_PY_COUNT"
exit 0
SGFAKE
chmod +x "$FAKEBIN/python3"

count_starts() {
    if [ -f "$COUNT" ]; then
        wc -l < "$COUNT" | tr -d ' \t'
    else
        echo 0
    fi
}

cache_text() {  # cache_text <file>: the file content, without trailing newlines
    cat "$1" 2>/dev/null
}

has_python_on() {  # has_python_on <PATH value>: exit 0 if python3, python or py is found
    (
        PATH="$1"; export PATH
        for _c in python3 python py; do
            if command -v "$_c" >/dev/null 2>&1; then exit 0; fi
        done
        exit 1
    )
}

run_launcher() {  # run_launcher <plugin data, or empty to unset it> <tool> [args...]
    _data="$1"
    shift
    (
        PATH="$FAKEBIN:$PATH"; export PATH
        FAKE_PY_COUNT="$COUNT"; export FAKE_PY_COUNT
        TMPDIR="$T/tmp"; export TMPDIR
        if [ -n "$_data" ]; then
            CLAUDE_PLUGIN_DATA="$_data"; export CLAUDE_PLUGIN_DATA
        else
            unset CLAUDE_PLUGIN_DATA
        fi
        exec "$SH" "$RUNDIR/run.sh" "$@"
    )
}

report() {  # report <check name> <0 pass or 1 fail> <reason>
    if [ "$2" -eq 0 ]; then
        echo "PASS $1"
    else
        echo "FAIL $1: ${3:-}"
        FAILED=1
    fi
}

# 1. First call: the probe runs (version check + tool run = 2 starts) and the cache is written.
D1="$T/data-main"
run_launcher "$D1" dummy 2>"$T/err1"; rc=$?
n=$(count_starts)
got="$(cache_text "$D1/$CACHE_NAME")"
if [ "$rc" -ne 0 ]; then
    report first_call_probes_and_caches 1 "exit code $rc, expected 0"
elif [ "$n" -ne 2 ]; then
    report first_call_probes_and_caches 1 "$n interpreter starts, expected 2"
elif [ "$got" != "$FAKEBIN/python3" ]; then
    report first_call_probes_and_caches 1 "cache holds '$got', expected '$FAKEBIN/python3'"
else
    report first_call_probes_and_caches 0 ""
fi

# 2. Second call with the same data folder: only the tool run starts, so no probe.
before=$(count_starts)
run_launcher "$D1" dummy 2>"$T/err2"; rc=$?
after=$(count_starts)
delta=$((after - before))
if [ "$rc" -ne 0 ]; then
    report second_call_skips_probe 1 "exit code $rc, expected 0"
elif [ "$delta" -ne 1 ]; then
    report second_call_skips_probe 1 "$delta interpreter starts, expected 1 (tool run only)"
else
    report second_call_skips_probe 0 ""
fi

# 3. Cache points to a missing file: the probe runs again and the cache is rewritten.
D2="$T/data-stale"
mkdir -p "$D2"
printf '%s\n' "$T/gone/python3" > "$D2/$CACHE_NAME"
before=$(count_starts)
run_launcher "$D2" dummy 2>"$T/err3"; rc=$?
after=$(count_starts)
delta=$((after - before))
got="$(cache_text "$D2/$CACHE_NAME")"
if [ "$rc" -ne 0 ]; then
    report stale_cache_reprobes_and_rewrites 1 "exit code $rc, expected 0"
elif [ "$delta" -ne 2 ]; then
    report stale_cache_reprobes_and_rewrites 1 "$delta interpreter starts, expected 2 (probe + tool run)"
elif [ "$got" != "$FAKEBIN/python3" ]; then
    report stale_cache_reprobes_and_rewrites 1 "cache holds '$got', expected '$FAKEBIN/python3'"
else
    report stale_cache_reprobes_and_rewrites 0 ""
fi

# 4. No Python 3 on PATH: exit 0 and the message on stderr.
D3="$T/data-nopy"
if has_python_on "$NOPY_PATH"; then
    report no_interpreter_exits_0_with_message 1 "precondition: $NOPY_PATH already has python3, python or py"
else
    (
        PATH="$NOPY_PATH"; export PATH
        CLAUDE_PLUGIN_DATA="$D3"; export CLAUDE_PLUGIN_DATA
        exec "$SH" "$RUNDIR/run.sh" dummy
    ) 2>"$T/err4"; rc=$?
    if [ "$rc" -ne 0 ]; then
        report no_interpreter_exits_0_with_message 1 "exit code $rc, expected 0"
    elif ! grep -q 'no Python 3 interpreter found' "$T/err4"; then
        report no_interpreter_exits_0_with_message 1 "stderr lacks 'no Python 3 interpreter found': $(cat "$T/err4")"
    else
        report no_interpreter_exits_0_with_message 0 ""
    fi
fi

# 5. Tool file does not exist: exit 0, the message on stderr, and no probe.
before=$(count_starts)
run_launcher "$T/data-tool" no_such_tool 2>"$T/err5"; rc=$?
after=$(count_starts)
if [ "$rc" -ne 0 ]; then
    report missing_tool_exits_0_with_message 1 "exit code $rc, expected 0"
elif ! grep -q 'no such tool' "$T/err5"; then
    report missing_tool_exits_0_with_message 1 "stderr lacks 'no such tool': $(cat "$T/err5")"
elif [ "$((after - before))" -ne 0 ]; then
    report missing_tool_exits_0_with_message 1 "$((after - before)) interpreter starts, expected 0"
else
    report missing_tool_exits_0_with_message 0 ""
fi

# 6. No CLAUDE_PLUGIN_DATA: the cache goes to ${TMPDIR} (a folder of the test).
before=$(count_starts)
run_launcher "" dummy 2>"$T/err6"; rc=$?
after=$(count_starts)
delta=$((after - before))
got="$(cache_text "$T/tmp/$CACHE_NAME")"
if [ "$rc" -ne 0 ]; then
    report fallback_cache_in_tmpdir 1 "exit code $rc, expected 0"
elif [ "$delta" -ne 2 ]; then
    report fallback_cache_in_tmpdir 1 "$delta interpreter starts, expected 2"
elif [ "$got" != "$FAKEBIN/python3" ]; then
    report fallback_cache_in_tmpdir 1 "cache at TMPDIR holds '$got', expected '$FAKEBIN/python3'"
else
    report fallback_cache_in_tmpdir 0 ""
fi

exit "$FAILED"
