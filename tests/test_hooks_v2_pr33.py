#!/usr/bin/env python3
"""PR-33 checks: hooks/hooks.json names the v2 scripts, with the exact target list.

Reads hooks/hooks.json, the base commit's copy (git show), the names in scripts/ and
scripts/run.sh. Writes nothing (run.sh is called with an unknown tool, which exits before
any cache write). Run from the repository root:  python -I -B tests/test_hooks_v2_pr33.py
Prints PASS or FAIL per check. Exits 1 if any check fails.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks" / "hooks.json"
SCRIPTS = ROOT / "scripts"
RUN_SH = SCRIPTS / "run.sh"
BASE = os.environ.get("PR33_BASE", "b7ec889")  # the commit PR-33 was cut from
PREFIX = 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" '
EVENTS = ["SessionStart", "Stop", "PreCompact", "SessionEnd"]

# Brief section 4, exact: (text after run.sh, timeout), in order within each event.
TARGET = {
    "SessionStart": [
        ("session_indexer_v2 register", 10),
        ("session_indexer_v2 scan", 60),
        ("session_detect", 10),
        ("session_archive_v2 sync", 60),
        ("api_repair_v2 fix --all", 180),
    ],
    "Stop": [
        ("session_archive_v2 sync", 60),
        ("session_indexer_v2 merge", 60),
        ("usage_db sync", 180),
        ("usage_db export", 120),
    ],
    "PreCompact": [
        ("session_archive_v2 sync", 60),
        ("jsonl_repair_v2 --all", 180),
        ("api_repair_v2 fix --all", 180),
        ("session_indexer_v2 merge", 15),
    ],
    "SessionEnd": [
        ("api_repair_v2 from-hook", 60),
    ],
}

# The two usage entries keep their v1 names until decisions D1 to D3 are made.
USAGE_ENTRIES = {("Stop", "usage_db sync"), ("Stop", "usage_db export")}

PASSES = 0
FAILS = 0


def report(name, ok, detail=""):
    global PASSES, FAILS
    if ok:
        PASSES += 1
        print(f"PASS {name}")
    else:
        FAILS += 1
        print(f"FAIL {name}: {detail}")


def run(name, fn, *args):
    try:
        problems = fn(*args)
    except Exception as exc:  # a malformed file shows up here as a FAIL, not a crash
        problems = [f"exception {type(exc).__name__}: {exc}"]
    report(name, not problems, "; ".join(problems)[:600])


def hook_list(doc):
    """(event, text after run.sh, timeout) for each hook, in file order."""
    rows = []
    for event, groups in doc["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                cmd = hook["command"]
                rows.append((event, cmd[len(PREFIX):] if cmd.startswith(PREFIX) else cmd, hook["timeout"]))
    return rows


def check_shape(doc):
    if not isinstance(doc, dict) or set(doc) != {"hooks"}:
        return ["top level must hold only the key 'hooks'"]
    events = doc["hooks"]
    if not isinstance(events, dict):
        return ["'hooks' must be an object"]
    problems = []
    if set(events) != set(EVENTS):
        problems.append(f"events are {sorted(events)}, expected {sorted(EVENTS)}")
    for event, groups in events.items():
        if not isinstance(groups, list) or not groups:
            problems.append(f"{event}: must be a non-empty list of groups")
            continue
        for i, group in enumerate(groups):
            if not isinstance(group, dict) or set(group) != {"hooks"}:
                problems.append(f"{event}[{i}]: a group must hold only 'hooks' (no matcher)")
                continue
            hooks = group["hooks"]
            if not isinstance(hooks, list) or not hooks:
                problems.append(f"{event}[{i}]: 'hooks' must be a non-empty list")
                continue
            for j, hook in enumerate(hooks):
                where = f"{event}[{i}].hooks[{j}]"
                if not isinstance(hook, dict) or set(hook) != {"type", "command", "timeout"}:
                    problems.append(f"{where}: keys must be exactly type, command, timeout")
                elif not isinstance(hook["command"], str) or not hook["command"].startswith(PREFIX):
                    problems.append(f"{where}: command must start with the run.sh form")
    return problems


def check_target(doc):
    got = {}
    for event, groups in doc["hooks"].items():
        got[event] = [(h["command"][len(PREFIX):], h["timeout"]) for g in groups for h in g["hooks"]]
    problems = []
    for event in EVENTS:
        if got.get(event) != TARGET[event]:
            problems.append(f"{event}: file {got.get(event)} != target {TARGET[event]}")
    return problems


def check_tools_exist(doc):
    problems = []
    for event, args, _timeout in hook_list(doc):
        tool = args.split()[0]
        if not (SCRIPTS / f"{tool}.py").is_file():
            problems.append(f"{event}: scripts/{tool}.py is missing")
    return problems


def check_types_timeouts(doc):
    problems = []
    for event, groups in doc["hooks"].items():
        for i, group in enumerate(groups):
            for j, hook in enumerate(group["hooks"]):
                where = f"{event}[{i}].hooks[{j}]"
                if hook.get("type") != "command":
                    problems.append(f"{where}: type is {hook.get('type')!r}, expected 'command'")
                t = hook.get("timeout")
                if type(t) is not int or t <= 0:
                    problems.append(f"{where}: timeout {t!r} is not a positive integer")
    return problems


def check_no_v1(doc):
    # A v1 name is a tool that has a *_v2.py beside it. Only the two usage entries may keep one.
    v2_stems = {p.name[: -len("_v2.py")] for p in SCRIPTS.glob("*_v2.py")}
    problems = []
    for event, args, _timeout in hook_list(doc):
        tool = args.split()[0]
        if tool in v2_stems and (event, args) not in USAGE_ENTRIES:
            problems.append(f"{event}: '{args}' still uses a v1 name")
    return problems


def check_usage_unchanged(doc):
    proc = subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE}:hooks/hooks.json"], capture_output=True)
    if proc.returncode != 0:
        return [f"git show {BASE}:hooks/hooks.json failed: {proc.stderr.decode('utf-8', 'replace').strip()}"]
    base = json.loads(proc.stdout.decode("utf-8"))

    def usage(d):
        return [(e, h["command"], h["timeout"])
                for e, groups in d["hooks"].items() for g in groups for h in g["hooks"]
                if "usage_db" in h["command"]]

    before, after = usage(base), usage(doc)
    if len(before) != 2:
        return [f"the base commit has {len(before)} usage entries, expected 2"]
    if before != after:
        return [f"base {before} != now {after}"]
    return []


def check_run_sh_unknown_tool():
    sh = shutil.which("sh")
    if not sh:
        return ["sh is not on PATH"]
    tmp = tempfile.mkdtemp(prefix="pr33-")
    try:
        env = dict(os.environ)
        for key in ("TMPDIR", "TMP", "TEMP"):
            env[key] = tmp
        env.pop("CLAUDE_PLUGIN_DATA", None)
        proc = subprocess.run([sh, RUN_SH.as_posix(), "no_such_tool"], capture_output=True, env=env, timeout=60)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit {proc.returncode}, expected 0")
    if b"no such tool: no_such_tool" not in proc.stderr:
        problems.append(f"stderr lacks the message: {proc.stderr[:200]!r}")
    return problems


def main():
    try:
        doc = json.loads(HOOKS.read_bytes().decode("utf-8"))
    except Exception as exc:
        report("json_parses", False, f"{type(exc).__name__}: {exc}")
        doc = None
    if doc is not None:
        report("json_parses", True)
        run("shape", check_shape, doc)
        run("target_list", check_target, doc)
        run("tools_exist", check_tools_exist, doc)
        run("type_and_timeouts", check_types_timeouts, doc)
        run("no_v1_names", check_no_v1, doc)
        run("usage_entries_unchanged", check_usage_unchanged, doc)
    run("run_sh_unknown_tool_exits_0", check_run_sh_unknown_tool)
    print(f"SUMMARY {PASSES} PASS, {FAILS} FAIL")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
