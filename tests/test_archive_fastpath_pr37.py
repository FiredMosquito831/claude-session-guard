"""
Tests for PR-37 (scripts/session_archive_v2.py, scripts/guardkit.py): an unchanged transcript takes no
lock, a busy lock is waited for 2 s, and the lock of a dead holder is reclaimed at once.

Run from the worktree root:  python -I -B tests\\test_archive_fastpath_pr37.py
Proof tier 3 (AGENTS 2.6). Each check prints PASS or FAIL:
  5.1  equivalence with the base commit bb3d353, step by step: archive, state, quarantine and lock hashes
  5.2  hard kill with the lock held, at three kill points, then a rerun: no loss, no duplicate, canary 0
  5.3  the base code reads the new output: a sync on a copy reports nothing to do and writes nothing
  5.4  A/B latency on 11,885 small transcripts: steady runs, a lock left by a dead process, live and old locks
  5.5  mutation checks, run on copies under the temporary folder (the repository files are never changed)

Every synthetic tree is under a temporary folder that must not be under the user's home folder. Set
SESSION_GUARD_TEST_TMP to choose that folder. Otherwise <drive>\\tmp\\session-guard-tests is used,
because the default Windows temp folder is under the user's profile. Exit status is 1 on any FAIL.
"""
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NEW_SCRIPTS = REPO / "scripts"
BASE_REV = "bb3d353"                    # the commit this PR starts from (its parent)
HOME_DIR = Path.home().resolve()
N_BIG = 2000                            # transcripts in the A/B run (5.4); the brief's 11,885 is scaled down for disk space
SLUGS = ["C--work-alpha", "C--work-beta", "C--work-gamma", "C--work-delta"]
HEAD_LINE = b'{"type": "user", "uuid": "head-0001-aaaa", "message": {"content": "start"}}\n'
STAMP_RE = re.compile(r"\.\d{8}-\d{6}-\d{6}\.")
RESULTS = []


def report(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
    return bool(ok)


def base_folder() -> Path:
    env = os.environ.get("SESSION_GUARD_TEST_TMP")
    return Path(env) if env else Path(os.path.abspath(os.sep)) / "tmp" / "session-guard-tests"


def assert_isolated(folder: Path) -> None:
    """Stop before any write when the folder is under the user's home folder."""
    try:
        folder.resolve().relative_to(HOME_DIR)
    except ValueError:
        return
    print(f"FAIL isolation: {folder} is under the home folder {HOME_DIR}; set SESSION_GUARD_TEST_TMP")
    sys.exit(1)


BASE_FOLDER = base_folder()
BASE_FOLDER.mkdir(parents=True, exist_ok=True)
assert_isolated(BASE_FOLDER)
ROOT = Path(tempfile.mkdtemp(prefix="pr37-", dir=str(BASE_FOLDER)))
assert_isolated(ROOT)


# ---- running the tools -------------------------------------------------------------------

def home_env(home: Path) -> dict:
    assert_isolated(home)
    env = dict(os.environ)
    env["SESSION_GUARD_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_sync(scripts: Path, home: Path, *args: str, timeout: float = 1500) -> dict:
    """Run session_archive_v2.py as a script. It imports guardkit from its own folder, so no -I here."""
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, "-B", str(scripts / "session_archive_v2.py"), *args],
                       env=home_env(home), capture_output=True, timeout=timeout)
    return {"rc": p.returncode, "out": p.stdout.decode("utf-8", "replace"),
            "err": p.stderr.decode("utf-8", "replace"), "secs": time.perf_counter() - t0}


def last_line(out: str) -> str:
    lines = out.strip().splitlines()
    return lines[-1] if lines else "(no output)"


def base_scripts() -> Path:
    """The base commit's two scripts, read with git show into the temporary folder."""
    dst = ROOT / "base" / "scripts"
    dst.mkdir(parents=True)
    for name in ("session_archive_v2.py", "guardkit.py"):
        p = subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_REV}:scripts/{name}"], capture_output=True)
        if p.returncode != 0:
            print(f"FAIL setup: git show {BASE_REV}:scripts/{name}: {p.stderr.decode('utf-8', 'replace').strip()}")
            sys.exit(1)
        (dst / name).write_bytes(p.stdout)
    return dst


def make_mutant(name: str, filename: str, old: str, new: str) -> Path:
    """Copy the new scripts and replace one line of code in the copy. The old text must occur exactly once."""
    d = ROOT / f"mut-{name}" / "scripts"
    shutil.copytree(NEW_SCRIPTS, d, ignore=shutil.ignore_patterns("__pycache__"))
    data = (d / filename).read_bytes()
    count = data.count(old.encode("utf-8"))
    if count != 1:
        print(f"FAIL setup: mutant {name}: the text occurs {count} times, expected 1")
        sys.exit(1)
    (d / filename).write_bytes(data.replace(old.encode("utf-8"), new.encode("utf-8")))
    return d


# ---- synthetic data ----------------------------------------------------------------------

def make_lines(rng: random.Random, tag: str, n: int, pad: int = 60) -> bytes:
    out = []
    for i in range(n):
        obj = {"type": "assistant", "uuid": f"{tag}-{i:05d}-{rng.getrandbits(40):010x}",
               "timestamp": "2026-10-09T12:00:00Z",
               "message": {"usage": {"input_tokens": rng.randint(0, 9999), "output_tokens": rng.randint(0, 999)},
                           "content": [{"type": "text", "text": "x" * rng.randint(0, pad)}]}}
        out.append(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    return b"".join(out)


def special_bytes(rng: random.Random) -> bytes:
    """A transcript with the awkward lines: no uuid, a non-object, CRLF, not JSON, invalid UTF-8. It is
    longer than 8 KB, so the first 4 KB (head window) and the last 4 KB (tail window) of the archived
    prefix do not overlap. A rewrite of the first line is then seen by the head check alone."""
    return (HEAD_LINE
            + b'{"type": "summary", "summary": "no uuid here"}\n'
            + b"[1, 2, 3]\n"
            + b'{"type": "user", "uuid": "crlf-0001", "message": {"content": "crlf"}}\r\n'
            + b"this line is not json\n"
            + b"bad \xff bytes\n"
            + make_lines(rng, "special", 6, pad=40)
            + make_lines(rng, "special-tail", 60, pad=200))


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot(home: Path) -> list:
    """Sorted (path, sha256) of every file in the archive folder, plus the lock folder listing.
    Quarantine stamps are removed from the names, because they differ between two runs."""
    items = []
    arch = home / "session-archive"
    if arch.exists():
        for p in sorted(arch.rglob("*")):
            if p.is_file():
                rel = STAMP_RE.sub(".", p.relative_to(arch).as_posix())
                items.append((rel, sha_file(p)))
    locks = home / "session-tools" / "state" / "locks"
    items.append(("<locks>", sorted(x.name for x in locks.iterdir()) if locks.exists() else []))
    return sorted(items, key=lambda t: t[0])


def first_difference(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"entry {i}: base={x} new={y}"
    if len(a) != len(b):
        extra = a[len(b):] or b[len(a):]
        return f"length base={len(a)} new={len(b)}; first extra entry {extra[0]}"
    return ""


def p95(xs: list) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
    p.wait()
    return p.pid


def lock_path(home: Path, rel: str) -> Path:
    return home / "session-tools" / "state" / "locks" / ("session-archive-" + rel.replace("/", "__") + ".lock")


def plant_lock(home: Path, rel: str, content: bytes, age: float = 0.0) -> Path:
    p = lock_path(home, rel)
    assert_isolated(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


# ---- 5.1 equivalence ---------------------------------------------------------------------

def equivalence(base: Path, new: Path, label: str) -> dict:
    """Run the same changes on two copies: one with the base code, one with the new code. After each
    step both run `sync --all`, and the outputs, return codes and archive/state/lock snapshots must match."""
    rng = random.Random(3701)
    bh, nh = ROOT / f"eq-{label}-base", ROOT / f"eq-{label}-new"
    for h in (bh, nh):
        assert_isolated(h)
        shutil.rmtree(h, ignore_errors=True)
        (h / "projects").mkdir(parents=True)
    roots = [bh / "projects", nh / "projects"]

    def put(rel, data):
        for r in roots:
            p = r / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    def get(rel):
        return (roots[0] / rel).read_bytes()

    def append(rel, data):
        for r in roots:
            with open(r / rel, "ab") as f:
                f.write(data)

    def seed():
        for slug in SLUGS:
            for s in range(10):
                put(f"{slug}/sess-{s}.jsonl", make_lines(rng, f"{slug}-s{s}", rng.randint(3, 25), pad=40))
            put(f"{slug}/sess-0/subagents/agent-1.jsonl", make_lines(rng, f"{slug}-sub", 4, pad=40))
        put("C--work-alpha/special.jsonl", special_bytes(rng))
        put("C--work-alpha/sess-9-acompact.jsonl", make_lines(rng, "compact", 3, pad=40))

    def append_three():
        for rel in ("C--work-beta/sess-1.jsonl", "C--work-gamma/sess-2.jsonl", "C--work-alpha/sess-3.jsonl"):
            append(rel, make_lines(rng, "app-" + rel, 2, pad=40))

    def shrink():
        rel = "C--work-gamma/sess-6.jsonl"
        lines = get(rel).splitlines(keepends=True)
        assert len(lines) >= 3, "test setup: the shrink needs at least 3 lines"
        put(rel, b"".join(lines[:2]))

    def rewrite_head():
        rel = "C--work-alpha/special.jsonl"
        data = get(rel)
        assert data.count(b"head-0001-aaaa") == 1, "test setup: head marker missing"
        assert len(data) > 8192, "test setup: the head and tail windows would overlap"
        put(rel, data.replace(b"head-0001-aaaa", b"head-0001-bbbb"))      # same size, first line only

    def remove_one():
        for r in roots:
            (r / "C--work-alpha/sess-3.jsonl").unlink()

    def drop_state():
        for h in (bh, nh):
            p = h / "session-archive" / "state" / "C--work-beta" / "sess-1.jsonl.json"
            assert p.exists(), f"test setup: no state at {p.name}"
            p.unlink()

    steps = [
        ("seed: 40 transcripts, subagents, acompact, awkward lines", seed),
        ("no change", None),
        ("append 2 lines to 3 transcripts", append_three),
        ("append a partial line (no newline)",
         lambda: append("C--work-delta/sess-4.jsonl", b'{"type": "user", "uuid": "partial-0001"')),
        ("complete the partial line", lambda: append("C--work-delta/sess-4.jsonl", b"}\n")),
        ("new top-level transcript",
         lambda: put("C--work-beta/sess-new.jsonl", make_lines(rng, "newtop", 7, pad=40))),
        ("new subagent transcript",
         lambda: put("C--work-beta/sess-1/subagents/agent-new.jsonl", make_lines(rng, "newsub", 5, pad=40))),
        ("shrink a transcript (full path)", shrink),
        ("same-size rewrite of the first line (head)", rewrite_head),
        ("remove a transcript", remove_one),
        ("state deleted for one transcript (full merge)", drop_state),
        ("no change again", None),
    ]
    results = []
    for name, action in steps:
        if action is not None:
            action()
        b = run_sync(base, bh, "sync", "--all")
        n = run_sync(new, nh, "sync", "--all")
        sb, sn = snapshot(bh), snapshot(nh)
        same = b["rc"] == n["rc"] and b["out"] == n["out"] and sb == sn
        results.append((name, same, last_line(n["out"])))
        if not same:
            return {"ok": False, "step": name, "results": results, "homes": (bh, nh),
                    "detail": (f"rc base={b['rc']} new={n['rc']}; "
                               f"out base={last_line(b['out'])!r} new={last_line(n['out'])!r}; "
                               f"first difference {first_difference(sb, sn)}; "
                               f"err base={b['err'][-200:]!r} new={n['err'][-200:]!r}")}
    return {"ok": True, "step": None, "results": results, "homes": (bh, nh), "detail": ""}


def sec_51(base: Path, new: Path):
    eq = equivalence(base, new, "main")
    for name, same, summary in eq["results"]:
        print(f"      step {'same ' if same else 'DIFF '}| {name} | {summary}")
    if eq["ok"]:
        report("5.1 equivalence with base bb3d353: every step", True,
               f"{len(eq['results'])} steps; archive, state and lock hashes equal after each step")
    else:
        report("5.1 equivalence with base bb3d353: every step", False,
               f"first differing step '{eq['step']}': {eq['detail']}")
    return eq


# ---- 5.2 hard kill -----------------------------------------------------------------------

WRAPPER = '''
import os, runpy, sys, time
scripts, kill_at, marker = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sys.path.insert(0, scripts)
sys.argv = [os.path.join(scripts, "session_archive_v2.py")] + sys.argv[4:]
calls = [0]
real_fsync = os.fsync
def counting_fsync(fd):
    real_fsync(fd)
    calls[0] += 1
    if calls[0] == kill_at:
        with open(marker, "w") as f:
            f.write("stalled")
        time.sleep(600)
os.fsync = counting_fsync
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def hard_kill_case(name: str, seed: int, kill_at: int, prepare) -> None:
    """Start a sync, stall it at its kill_at-th fsync (after the archive append, lock held), kill it, rerun."""
    home = ROOT / f"kill-{name}"
    assert_isolated(home)
    shutil.rmtree(home, ignore_errors=True)
    rng = random.Random(seed)
    rel = "C--work-kill/sess-kill.jsonl"
    live = home / "projects" / rel
    arch = home / "session-archive" / "transcripts" / rel
    state = home / "session-archive" / "state" / (rel + ".json")
    live.parent.mkdir(parents=True, exist_ok=True)
    wrapper = ROOT / "kill_wrapper.py"
    wrapper.write_text(WRAPPER, encoding="ascii")
    prepare(rng, home, live, arch)
    state_before = state.read_bytes() if state.exists() else None
    marker = home / "stalled.marker"
    proc = subprocess.Popen([sys.executable, "-B", str(wrapper), str(NEW_SCRIPTS), str(kill_at), str(marker),
                             "sync", "--transcript", str(live)],
                            env=home_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    while not marker.exists() and proc.poll() is None and time.time() - t0 < 300:
        time.sleep(0.005)
    reached = marker.exists() and proc.poll() is None
    if reached:
        proc.kill()
    out, err = proc.communicate()
    lock_held = lock_path(home, rel).exists()
    arch_at_kill = arch.stat().st_size if arch.exists() else 0
    state_at_kill = state.read_bytes() if state.exists() else None
    rerun = run_sync(NEW_SCRIPTS, home, "sync", "--all")
    verify = run_sync(NEW_SCRIPTS, home, "verify")
    live_bytes = live.read_bytes()
    arch_bytes = arch.read_bytes() if arch.exists() else b""
    lines = arch_bytes.split(b"\n")[:-1]
    quarantine = home / "session-archive" / "quarantine"
    qfiles = [q for q in quarantine.rglob("*") if q.is_file()] if quarantine.exists() else []
    ghost_kept = any(b"ghost-partial" in q.read_bytes() for q in qfiles) if qfiles else False
    no_dup = len(lines) == len(set(lines))
    no_loss = arch_bytes == live_bytes
    canary_zero = verify["rc"] == 0 and "live files missing archived lines: 0" in verify["out"]
    state_untouched_at_kill = state_at_kill == state_before
    ok = (reached and lock_held and state_untouched_at_kill and rerun["rc"] == 0
          and "skipped" not in rerun["out"] and not lock_path(home, rel).exists()
          and no_dup and no_loss and canary_zero and (name != "C" or ghost_kept))
    detail = (f"kill at fsync {kill_at}; killed inside the write: {reached}; lock held at kill: {lock_held}; "
              f"archive {arch_at_kill} bytes at kill, live {len(live_bytes)} bytes; "
              f"state unchanged at kill: {state_untouched_at_kill}; rerun rc {rerun['rc']} "
              f"({last_line(rerun['out'])}); no duplicate lines: {no_dup}; no line lost: {no_loss}; "
              f"lines {len(lines)}; quarantine files {len(qfiles)}; canary: {last_line(verify['out'])}")
    report(f"5.2 hard kill, kill point {name}", ok, detail)
    discard(home)


def prepare_full_merge(rng, home, live, arch):
    """Kill point A: a new 16 MB transcript (full merge). Kill after its first archive append."""
    live.write_bytes(make_lines(rng, "kA", 40000, pad=60))


def prepare_append(rng, home, live, arch):
    """Kill point B: an archived 16 MB transcript that grows by 16 MB (fast path append)."""
    live.write_bytes(make_lines(rng, "kB", 40000, pad=60))
    run_sync(NEW_SCRIPTS, home, "sync", "--all")
    with open(live, "ab") as f:
        f.write(make_lines(rng, "kB2", 40000, pad=60))


def prepare_after_crash(rng, home, live, arch):
    """Kill point C: as B, but a crash left a partial line beyond the recorded archive size. The sync
    must quarantine it, cut it, and then append. Kill after that re-append."""
    live.write_bytes(make_lines(rng, "kC", 40000, pad=60))
    run_sync(NEW_SCRIPTS, home, "sync", "--all")
    with open(arch, "ab") as f:
        f.write(b'{"type": "user", "uuid": "ghost-partial')
    with open(live, "ab") as f:
        f.write(make_lines(rng, "kC2", 40000, pad=60))


def sec_52():
    hard_kill_case("A", 11, 2, prepare_full_merge)     # fsync 1 = lock file, 2 = archive append
    hard_kill_case("B", 12, 2, prepare_append)         # fsync 1 = lock file, 2 = archive append
    hard_kill_case("C", 13, 4, prepare_after_crash)    # 1 lock, 2 quarantine, 3 cut, 4 re-append


# ---- 5.3 old-version readability ---------------------------------------------------------

def v1_env(fake: Path) -> dict:
    """Environment for the v1 tool. It takes its home from Path.home() and ignores SESSION_GUARD_HOME,
    so its home is moved into the temporary folder with USERPROFILE and HOME."""
    assert_isolated(fake)
    env = dict(os.environ)
    env["USERPROFILE"] = str(fake)
    env["HOME"] = str(fake)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def v1_home_is_scratch(fake: Path) -> bool:
    """Ask a child interpreter with the v1 environment where Path.home() is. Only then is v1 run."""
    p = subprocess.run([sys.executable, "-B", "-c", "from pathlib import Path; print(Path.home())"],
                       env=v1_env(fake), capture_output=True, timeout=60)
    got = p.stdout.decode("utf-8", "replace").strip()
    return p.returncode == 0 and bool(got) and Path(got).resolve() == fake.resolve()


def canary_counts(text: str):
    """(archived sessions, live files missing archived lines) from a verify printout, or None where absent."""
    a = re.search(r"archived sessions:\s+(\d+)", text)
    m = re.search(r"live files missing archived lines:\s+(\d+)", text)
    return (int(a.group(1)) if a else None, int(m.group(1)) if m else None)


def run_v1(fake: Path):
    """Run the v1 verify with its home moved to `fake`. Refuses any home outside this test's folder."""
    try:
        fake.resolve().relative_to(ROOT.resolve())
    except ValueError:
        print(f"FAIL isolation: v1 home {fake} is not under the test folder")
        sys.exit(1)
    p = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "session_archive.py"), "verify"],
                       env=v1_env(fake), capture_output=True, timeout=900)
    return p.returncode, p.stderr.decode("utf-8", "replace"), p.stdout.decode("utf-8", "replace")


def discard(folder: Path) -> None:
    """Delete one synthetic tree once its check is done. Only folders under this test's folder."""
    assert_isolated(folder)
    try:
        folder.resolve().relative_to(ROOT.resolve())
    except ValueError:
        print(f"FAIL isolation: refusing to delete {folder}, it is not under the test folder")
        sys.exit(1)
    shutil.rmtree(folder, ignore_errors=True)


def sec_53(base: Path, eq_new_home: Path):
    """The base code reads the new output: its sync on a copy reports nothing to do and writes nothing,
    and its verify counts every archived transcript. The v1 tool (session_archive.py) reads the same
    archive: on a copy without the awkward transcript, its verify must give the same counts as base verify.
    v1 has a defect of its own: line_key calls entry.get on a JSON line that is not an object, so v1
    verify crashes on the full copy. That defect is reported, not gated. PR-37 does not touch v1, and 5.1
    showed the base and new archive bytes are equal, so v1 sees the same bytes in both."""
    fake = ROOT / "v1-home"
    home = fake / ".claude"                             # one home for the base sync, base verify and v1
    assert_isolated(home)
    shutil.copytree(eq_new_home, home)
    before = snapshot(home)
    r = run_sync(base, home, "sync", "--all")
    mid = snapshot(home)
    sync_ok = r["rc"] == 0 and "archived 0 (0 new lines)" in r["out"] and before == mid
    n_arch = len(list((home / "session-archive" / "transcripts").rglob("*.jsonl")))
    base_counts = canary_counts(run_sync(base, home, "verify")["out"])

    # v1 on a copy without the awkward transcript (removed from the copy's live folder and archive only)
    reduced = ROOT / "v1-reduced"
    assert_isolated(reduced)
    shutil.rmtree(reduced, ignore_errors=True)
    shutil.copytree(home, reduced / ".claude")
    (reduced / ".claude" / "projects" / "C--work-alpha" / "special.jsonl").unlink()
    (reduced / ".claude" / "session-archive" / "transcripts" / "C--work-alpha" / "special.jsonl").unlink()
    red_counts_base = canary_counts(run_sync(base, reduced / ".claude", "verify")["out"])
    v1_guard = v1_home_is_scratch(reduced)     # Path.home() of the v1 environment is checked before v1 runs
    v1_counts, v1_rc, v1_err = (None, None), None, ""
    if v1_guard:
        v1_rc, v1_err, v1_out = run_v1(reduced)
        v1_counts = canary_counts(v1_out)
    v1_ok = v1_guard and v1_rc in (0, 1) and "Traceback" not in v1_err and v1_counts == red_counts_base \
        and v1_counts[0] == len(list((reduced / ".claude" / "session-archive" / "transcripts").rglob("*.jsonl")))
    discard(reduced)
    report("5.3 base code reads the new output (nothing to do, nothing written); v1 reads the same archive",
           sync_ok and base_counts[0] == n_arch and v1_ok,
           f"base sync rc {r['rc']}: {last_line(r['out'])}; files changed by sync: {'none' if before == mid else 'CHANGED'}; "
           f"archived transcripts {n_arch}; base verify on the copy (archived, missing) {base_counts}; "
           f"v1 on the copy without the awkward transcript rc {v1_rc} (archived, missing) {v1_counts}, "
           f"base verify on the same copy {red_counts_base}")
    discard(home.parent)


# ---- 5.4 A/B latency ---------------------------------------------------------------------

def sec_54(base: Path, new: Path):
    gen = random.Random(3704)
    files = [(f"C--ab-{i % 50:02d}/sess-{i:05d}.jsonl", make_lines(gen, f"ab{i}", 3, pad=1800))
             for i in range(N_BIG)]
    homes = {"base": ROOT / "ab-base", "new": ROOT / "ab-new"}
    scripts = {"base": base, "new": new}
    for h in homes.values():
        assert_isolated(h)
        shutil.rmtree(h, ignore_errors=True)
        for rel, data in files:
            p = h / "projects" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    avg = sum(len(d) for _, d in files) / len(files)

    seed = {v: run_sync(scripts[v], homes[v], "sync", "--all") for v in homes}
    report("5.4 seed: each version archives all transcripts once",
           all(seed[v]["rc"] == 0 and f"archived {N_BIG} " in seed[v]["out"] for v in seed),
           f"{N_BIG} transcripts, {avg / 1024:.1f} KB average; base {seed['base']['secs']:.1f} s, "
           f"new {seed['new']['secs']:.1f} s (seed times, not timed as steady)")

    pre = {v: snapshot(homes[v]) for v in homes}
    times = {"base": [], "new": []}
    steady_ok = True
    for v in ("base", "new"):                           # three steady runs of each, back to back, alone
        for _ in range(3):
            r = run_sync(scripts[v], homes[v], "sync", "--all")
            times[v].append(r["secs"])
            steady_ok = steady_ok and r["rc"] == 0 and "archived 0 (0 new lines)" in r["out"]
    unchanged = all(snapshot(homes[v]) == pre[v] for v in homes)
    report("5.4 steady runs change nothing and archive nothing", steady_ok and unchanged,
           "every steady run printed 'archived 0 (0 new lines)'; archive and state hashes unchanged")
    mb, mn = sum(times["base"]) / 3, sum(times["new"]) / 3
    ratio = mn / mb
    ratio_ok = mn <= 0.8 * mb
    report("5.4 steady run, mean and p95 (base vs new)", ratio_ok,
           f"base mean {mb:.2f} s p95 {p95(times['base']):.2f} s [{', '.join(f'{t:.2f}' for t in times['base'])}]; "
           f"new mean {mn:.2f} s p95 {p95(times['new']):.2f} s [{', '.join(f'{t:.2f}' for t in times['new'])}]; "
           f"new/base {ratio:.2f}; criterion new mean <= 0.80 x base mean")

    # X: new bytes, and a lock left by a process that has exited. Same bytes in both homes.
    extra = make_lines(gen, "stale-x", 2, pad=1800)
    rel_x = files[100][0]
    for h in homes.values():
        with open(h / "projects" / rel_x, "ab") as f:
            f.write(extra)
    dead = dead_pid()
    for h in homes.values():
        plant_lock(h, rel_x, f"{dead} {time.time()}\n".encode("ascii"))
    size_x = {v: (homes[v] / "session-archive" / "transcripts" / rel_x).stat().st_size for v in homes}
    tn = run_sync(new, homes["new"], "sync", "--transcript", str(homes["new"] / "projects" / rel_x))
    got_new = (homes["new"] / "session-archive" / "transcripts" / rel_x).stat().st_size - size_x["new"]
    report("5.4 stale lock, dead holder: new reclaims it at once and archives the new lines",
           tn["rc"] == 0 and got_new == len(extra) and tn["secs"] <= 2.5 and not lock_path(homes["new"], rel_x).exists(),
           f"new {tn['secs']:.2f} s (limit 2.5 s); {got_new} of {len(extra)} bytes archived; lock removed: "
           f"{not lock_path(homes['new'], rel_x).exists()}")
    tb = run_sync(base, homes["base"], "sync", "--transcript", str(homes["base"] / "projects" / rel_x))
    got_base = (homes["base"] / "session-archive" / "transcripts" / rel_x).stat().st_size - size_x["base"]
    report("5.4 stale lock, dead holder: base waits its 60 s and skips the transcript",
           tb["rc"] == 0 and "skipped" in tb["out"] and got_base == 0 and tb["secs"] >= 55,
           f"base {tb['secs']:.2f} s; skipped: {'skipped' in tb['out']}; bytes archived: {got_base}")

    # Y: a lock held by a live process (this one) is not reclaimed. Z: an old lock with an unreadable
    # holder is taken by the stale rule. Z2: a fresh lock with an unreadable holder is left alone.
    rel_y, rel_z, rel_z2 = files[200][0], files[300][0], files[400][0]
    new_home = homes["new"]
    for rel in (rel_y, rel_z, rel_z2):
        with open(new_home / "projects" / rel, "ab") as f:
            f.write(extra)
    plant_lock(new_home, rel_y, f"{os.getpid()} {time.time()}\n".encode("ascii"))
    y_before = lock_path(new_home, rel_y).read_bytes()
    ty = run_sync(new, new_home, "sync", "--transcript", str(new_home / "projects" / rel_y))
    y_kept = lock_path(new_home, rel_y).exists() and lock_path(new_home, rel_y).read_bytes() == y_before
    report("5.4 live holder (alive pid): not reclaimed, transcript skipped, lock intact",
           ty["rc"] == 0 and "skipped" in ty["out"] and y_kept and ty["secs"] <= 3.0,
           f"new {ty['secs']:.2f} s; skipped: {'skipped' in ty['out']}; lock intact: {y_kept}")
    lock_path(new_home, rel_y).unlink()
    plant_lock(new_home, rel_z, b"garbage\n", age=3000)
    tz = run_sync(new, new_home, "sync", "--transcript", str(new_home / "projects" / rel_z))
    report("5.4 unreadable holder, lock older than 1800 s: taken by the stale rule (unchanged)",
           tz["rc"] == 0 and "archived 1 " in tz["out"] and not lock_path(new_home, rel_z).exists(),
           f"new {tz['secs']:.2f} s: {last_line(tz['out'])}")
    plant_lock(new_home, rel_z2, b"garbage\n", age=0)
    tz2 = run_sync(new, new_home, "sync", "--transcript", str(new_home / "projects" / rel_z2))
    z2_kept = lock_path(new_home, rel_z2).exists()
    report("5.4 unreadable holder, fresh lock: not reclaimed, skipped after the 2 s wait",
           tz2["rc"] == 0 and "skipped" in tz2["out"] and z2_kept and tz2["secs"] <= 3.5,
           f"new {tz2['secs']:.2f} s; skipped: {'skipped' in tz2['out']}; lock kept: {z2_kept}")
    lock_path(new_home, rel_z2).unlink()
    for rel in (rel_y, rel_z2):                         # bring the new home back to steady
        run_sync(new, new_home, "sync", "--transcript", str(new_home / "projects" / rel))
    return {"homes": homes, "times": times, "mb": mb, "mn": mn, "ratio_ok": ratio_ok, "files": files}


# ---- 5.5 mutation checks -----------------------------------------------------------------

def sec_55(base: Path, ab: dict):
    # (a) the unlocked check removed: equivalence must still pass, and the steady runs must regress
    mut_a = make_mutant("a", "session_archive_v2.py",
                        "    if _unchanged(live, arch, state_path):",
                        "    if False and _unchanged(live, arch, state_path):")
    eq_a = equivalence(base, mut_a, "mutA")
    discard(ROOT / "eq-mutA-base")
    discard(ROOT / "eq-mutA-new")
    mut_home = ab["homes"]["new"]
    t = []
    for _ in range(3):
        r = run_sync(mut_a, mut_home, "sync", "--all")
        t.append(r["secs"])
    mean_a = sum(t) / 3
    regress = mean_a > 0.8 * ab["mb"]
    report("5.5a remove the unlocked check: 5.1 still passes, 5.4 steady regresses",
           eq_a["ok"] and regress,
           f"equivalence {'passes' if eq_a['ok'] else 'FAILS at ' + str(eq_a['step'])}; mutant steady mean "
           f"{mean_a:.2f} s vs new {ab['mn']:.2f} s and base {ab['mb']:.2f} s (criterion would pass below "
           f"{0.8 * ab['mb']:.2f} s): regression {'detected' if regress else 'NOT detected'}")

    # (b) the head comparison broken: 5.1 must fail at the same-size rewrite of the first line
    mut_b = make_mutant("b", "session_archive_v2.py",
                        '_sha(head) != state["head_sha"]', "False")
    eq_b = equivalence(base, mut_b, "mutB")
    discard(ROOT / "eq-mutB-base")
    discard(ROOT / "eq-mutB-new")
    caught = (not eq_b["ok"]) and eq_b["step"] == "same-size rewrite of the first line (head)"
    report("5.5b break the head comparison: 5.1 fails on the same-size rewrite",
           caught, f"first differing step: {eq_b['step']!r}")

    # (c) the reclaim removed: the stale-lock case must fail (no archive within the 2 s wait)
    mut_c = make_mutant("c", "guardkit.py",
                        "        return self.reclaim_dead and _lock_holder_dead(self.path)",
                        "        return False")
    small = ROOT / "c-home"
    assert_isolated(small)
    shutil.rmtree(small, ignore_errors=True)
    rng = random.Random(3705)
    rel_w = "C--work-c/sess-w.jsonl"
    (small / "projects" / "C--work-c").mkdir(parents=True)
    (small / "projects" / rel_w).write_bytes(make_lines(rng, "cw", 4, pad=40))
    run_sync(NEW_SCRIPTS, small, "sync", "--all")
    with open(small / "projects" / rel_w, "ab") as f:
        f.write(make_lines(rng, "cw2", 2, pad=40))
    plant_lock(small, rel_w, f"{dead_pid()} {time.time()}\n".encode("ascii"))
    rc_ = run_sync(mut_c, small, "sync", "--transcript", str(small / "projects" / rel_w))
    not_archived = "skipped" in rc_["out"] and lock_path(small, rel_w).exists()
    rn_ = run_sync(NEW_SCRIPTS, small, "sync", "--transcript", str(small / "projects" / rel_w))
    control = "archived 1 " in rn_["out"] and not lock_path(small, rel_w).exists()
    report("5.5c remove the reclaim: the stale-lock case fails; the new code archives the same case",
           not_archived and control,
           f"mutant {rc_['secs']:.2f} s: {last_line(rc_['out'])}; new code then: {last_line(rn_['out'])}")
    discard(small)


# ---- main --------------------------------------------------------------------------------

def main() -> int:
    print(f"[pr37] temporary folder: {ROOT}")
    try:
        base = base_scripts()
        eq = sec_51(base, NEW_SCRIPTS)
        sec_52()
        if eq["ok"]:
            sec_53(base, eq["homes"][1])
        else:
            report("5.3 base code reads the new state and archive", False, "skipped: 5.1 failed")
        discard(eq["homes"][0])
        discard(eq["homes"][1])
        ab = sec_54(base, NEW_SCRIPTS)
        sec_55(base, ab)
        discard(ab["homes"]["base"])
        discard(ab["homes"]["new"])
    finally:
        if not os.environ.get("PR37_KEEP"):
            shutil.rmtree(ROOT, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"[pr37] {len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
