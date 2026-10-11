"""
Tests for the PR-28 walk change: scripts/guardkit.py transcript_files and session_transcripts,
and scripts/session_archive_v2.py iter_live_transcripts. Each scandir walk is compared with the
rglob expression it replaces (the oracle), on a synthetic tree built in a temporary folder.

Run from the worktree root:  python -I -B tests/test_walk_pr28.py
Every write goes to a fresh temporary SESSION_GUARD_HOME. The live Claude folder is never touched.
Prints one line per check: PASS <name>, FAIL <name>: <detail>, or SKIP <name>: <reason>.
Exit 1 if any FAIL line is printed. Symbolic-link checks SKIP where symlink creation is not permitted.
"""
import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path

# First, before any plugin import: this run must use a temporary home, never the live folder.
HOME = tempfile.mkdtemp(prefix="pr28_walk_home_")
HOME_TEXT = os.path.normcase(os.path.abspath(HOME))
LIVE_TEXT = os.path.normcase(os.path.abspath(os.path.join(os.path.expanduser("~"), ".claude")))
assert HOME_TEXT != LIVE_TEXT and not HOME_TEXT.startswith(LIVE_TEXT + os.sep), "home must not be the live folder"
assert HOME_TEXT.startswith(os.path.normcase(os.path.abspath(tempfile.gettempdir()))), "home must be a temporary folder"
os.environ["SESSION_GUARD_HOME"] = HOME

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402
import session_archive_v2 as sa  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COUNTS = {"PASS": 0, "FAIL": 0, "SKIP": 0}

ROOT = Path(HOME) / "projects"
PROJ = ROOT / "proj"
SUB = PROJ / "s1" / "subagents"


def scrub(text):
    """Hide the temporary folder in any printed detail. Only relative names may show."""
    return str(text).replace(HOME, "<tmp>").replace(HOME.replace(os.sep, "/"), "<tmp>")


def relname(path_text):
    return scrub(os.path.relpath(str(path_text), str(ROOT))).replace(os.sep, "/")


def emit(kind, name, detail=""):
    COUNTS[kind] += 1
    print(f"{kind} {name}" + (f": {scrub(detail)}" if detail else ""), flush=True)


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:  # a check that cannot run is a failure, never a pass
        emit("FAIL", name, f"{type(exc).__name__}: {exc}")
        return
    if ok:
        emit("PASS", name)
    else:
        emit("FAIL", name, detail)


def write(path, text="{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def build_tree():
    write(PROJ / "s1.jsonl")
    write(SUB / "a.jsonl")
    write(SUB / "workflows" / "w1" / "agent-b.jsonl")
    write(SUB / "agent-acompact-1.jsonl")
    write(PROJ / "acompact-x.jsonl")
    write(PROJ / "UP.JSONL")
    write(PROJ / "s2.txt", "not a transcript\n")
    (PROJ / "dir.jsonl").mkdir(parents=True)
    write(PROJ / "dir.jsonl" / "inner.jsonl")


# (link, target, target_is_directory). Created only where symlinks are permitted.
LINKS = [
    (PROJ / "ln.jsonl", PROJ / "s1.jsonl", False),           # file link named *.jsonl
    (PROJ / "dangle.jsonl", PROJ / "missing.jsonl", False),  # dangling link named *.jsonl
    (PROJ / "linkdir.jsonl", PROJ / "dir.jsonl", True),      # directory link named *.jsonl
    (PROJ / "linkdir", PROJ / "dir.jsonl", True),            # directory link, not named *.jsonl
    (SUB / "ln-sub.jsonl", SUB / "a.jsonl", False),          # site A: file link
    (SUB / "dangle-sub.jsonl", SUB / "missing.jsonl", False),  # site A: dangling link
    (SUB / "dirlink-sub.jsonl", SUB / "workflows", True),    # site A: directory link named *.jsonl
]


def create_links():
    """Return (created, error). error is None when every link was made."""
    created = []
    for link, target, is_dir in LINKS:
        try:
            os.symlink(str(target), str(link), target_is_directory=is_dir)
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            if winerr == 1314 or exc.errno in (errno.EPERM, errno.EACCES):
                return created, f"symlink not permitted here (WinError {winerr}, errno {exc.errno})"
            emit("FAIL", "symlink creation", f"unexpected error for {relname(link)}: {exc}")
            return created, "symlink creation failed unexpectedly"
        created.append((link, is_dir))
    return created, None


def remove_links(created):
    for link, _is_dir in reversed(created):
        try:
            os.unlink(str(link))
        except OSError:
            try:
                os.rmdir(str(link))
            except OSError:
                pass


def walk(**kw):
    """Relative posix names that transcript_files yields under ROOT."""
    return [r for r, _p, *_ in gk.transcript_files(ROOT, **kw)]


# --- Equivalence with the rglob oracles (section 7.2) -----------------------

def site_a_check():
    transcript = PROJ / "s1.jsonl"
    oracle = [transcript] + [p for p in SUB.rglob("*.jsonl") if p.is_file() and "acompact" not in str(p)]
    new = gk.session_transcripts(transcript)
    o_text = sorted(str(p) for p in oracle)
    n_text = sorted(str(p) for p in new)
    types_ok = all(isinstance(p, Path) for p in new)
    print(f"INFO site A: oracle {len(o_text)} items, new {len(n_text)} items: "
          + ", ".join(relname(x) for x in n_text), flush=True)
    detail = (f"only in oracle {[relname(x) for x in set(o_text) - set(n_text)]}, "
              f"only in new {[relname(x) for x in set(n_text) - set(o_text)]}, types_ok={types_ok}")
    return (o_text == n_text and types_ok and new[0] == transcript), detail


def site_b_check():
    oracle = [(p.relative_to(ROOT), p) for p in ROOT.rglob("*.jsonl") if p.is_file() and "acompact" not in str(p)]
    saved = sa.PROJECTS_DIR
    sa.PROJECTS_DIR = ROOT
    try:
        new = list(sa.iter_live_transcripts())
    finally:
        sa.PROJECTS_DIR = saved
    o_keys = sorted((str(r), str(p)) for r, p in oracle)
    n_keys = sorted((str(r), str(p)) for r, p in new)
    types_ok = all(isinstance(r, Path) and isinstance(p, Path) for r, p in new)
    print(f"INFO site B: oracle {len(o_keys)} items, new {len(n_keys)} items: "
          + ", ".join(k[0].replace(os.sep, "/") for k in n_keys), flush=True)
    detail = (f"only in oracle {[k[0] for k in set(o_keys) - set(n_keys)]}, "
              f"only in new {[k[0] for k in set(n_keys) - set(o_keys)]}, types_ok={types_ok}")
    return (o_keys == n_keys and types_ok), detail


# --- Keyword checks (section 7.2 and 15.5) -----------------------------------

def check_default_excludes_acompact():
    bad = [n for n in walk() if "acompact" in n]
    return (not bad), f"compaction names yielded: {bad}"


def check_skip_acompact_false_keeps():
    names = walk(skip_acompact=False)
    want = ["proj/acompact-x.jsonl", "proj/s1/subagents/agent-acompact-1.jsonl"]
    missing = [w for w in want if w not in names]
    return (not missing), f"missing: {missing}"


def check_default_raises_on_dangling():
    try:
        walk()
    except OSError:
        return True, ""
    return False, "the default walk did not raise OSError over a dangling link"


def check_skip_unstattable_skips_dangling():
    names = walk(skip_unstattable=True)
    bad = [n for n in ("proj/dangle.jsonl", "proj/s1/subagents/dangle-sub.jsonl") if n in names]
    ok = not bad and "proj/ln.jsonl" in names
    return ok, f"dangling links still yielded: {bad}, or proj/ln.jsonl missing"


def check_ignore_case_false_excludes_upper():
    return ("proj/UP.JSONL" not in walk()), "UP.JSONL yielded with ignore_case=False"


def check_ignore_case_true():
    present = "proj/UP.JSONL" in walk(ignore_case=True)
    if os.name == "nt":
        return present, "UP.JSONL not yielded with ignore_case=True on Windows"
    return (not present), "UP.JSONL yielded with ignore_case=True on POSIX"


def check_directory_never_yielded():
    variants = ({}, {"files_only": False}, {"files_only": True},
                {"files_only": True, "skip_unstattable": True, "ignore_case": True})
    for kw in variants:
        names = walk(**kw)
        if "proj/dir.jsonl" in names:
            return False, f"directory yielded with {kw}"
        if "proj/dir.jsonl/inner.jsonl" not in names:
            return False, f"inner transcript missing with {kw}"
    return True, ""


def check_files_only_yields_regular_file():
    return ("proj/s1.jsonl" in walk(files_only=True)), "regular file not yielded with files_only=True"


def check_site_a_links():
    names = [relname(p) for p in gk.session_transcripts(PROJ / "s1.jsonl")]
    ok = ("proj/s1/subagents/ln-sub.jsonl" in names
          and "proj/s1/subagents/dangle-sub.jsonl" not in names
          and "proj/s1/subagents/dirlink-sub.jsonl" not in names)
    return ok, f"site A names: {names}"


def check_dirlink_yielded_by_default():
    return ("proj/linkdir.jsonl" in walk()), "directory link named *.jsonl not yielded by the default walk"


def check_dirlink_not_with_files_only():
    return ("proj/linkdir.jsonl" not in walk(files_only=True)), "directory link yielded with files_only=True"


def check_filelink_with_files_only():
    return ("proj/ln.jsonl" in walk(files_only=True)), "file link not yielded with files_only=True"


class _ProxyEntry:
    """A DirEntry stand-in: delegates to the real entry, except that stat() raises for boom.jsonl."""

    def __init__(self, real):
        self._real = real
        self.name = real.name
        self.path = real.path

    def is_dir(self, follow_symlinks=True):
        return self._real.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks=True):
        if self.name == "boom.jsonl":
            raise PermissionError(13, "denied")
        return self._real.stat(follow_symlinks=follow_symlinks)


class _ProxyScan:
    """What the patched os.scandir returns: a context manager whose iteration yields proxy entries."""

    def __init__(self, real_cm):
        self._cm = real_cm

    def __enter__(self):
        self._it = self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def __iter__(self):
        for e in self._it:
            yield _ProxyEntry(e)


def check_stat_permission_error_propagates():
    perm = Path(HOME) / "perm-walk"
    write(perm / "boom.jsonl")
    write(perm / "ok.jsonl")
    real_scandir = os.scandir
    results = {}

    def fake_scandir(path):
        return _ProxyScan(real_scandir(path))

    os.scandir = fake_scandir
    try:
        for flag in (True, False):
            try:
                list(gk.transcript_files(perm, skip_unstattable=flag))
                results[flag] = "no error"
            except PermissionError as exc:
                results[flag] = f"PermissionError errno {exc.errno}"
            except Exception as exc:
                results[flag] = type(exc).__name__
    finally:
        os.scandir = real_scandir
    ok = all(v.startswith("PermissionError") for v in results.values())
    return ok, f"outcome by skip_unstattable: {results}"


# (name, function, needs symbolic links)
CHECKS = [
    ("site A equivalence: session_transcripts matches the rglob oracle", site_a_check, False),
    ("site B equivalence: iter_live_transcripts matches the rglob oracle", site_b_check, False),
    ("helper: default call excludes acompact", check_default_excludes_acompact, False),
    ("helper: skip_acompact=False keeps compaction files", check_skip_acompact_false_keeps, False),
    ("helper: skip_unstattable=False raises OSError on a dangling link", check_default_raises_on_dangling, True),
    ("helper: skip_unstattable=True skips a dangling link", check_skip_unstattable_skips_dangling, True),
    ("helper: a stat PermissionError propagates (skip_unstattable True and False)",
     check_stat_permission_error_propagates, False),
    ("helper: ignore_case=False excludes UP.JSONL", check_ignore_case_false_excludes_upper, False),
    ("helper: ignore_case=True includes UP.JSONL on Windows only", check_ignore_case_true, False),
    ("helper: a directory named dir.jsonl is never yielded", check_directory_never_yielded, False),
    ("helper: files_only=True yields a regular file", check_files_only_yields_regular_file, False),
    ("site A: excludes dangling and directory links, keeps file link", check_site_a_links, True),
    ("helper: directory link named *.jsonl is yielded by default", check_dirlink_yielded_by_default, True),
    ("helper: directory link named *.jsonl is not yielded with files_only=True",
     check_dirlink_not_with_files_only, True),
    ("helper: file link named *.jsonl is yielded with files_only=True", check_filelink_with_files_only, True),
]


def main():
    created = []
    try:
        build_tree()
        created, link_error = create_links()
        for name, fn, needs_links in CHECKS:
            if needs_links and link_error:
                emit("SKIP", name, link_error)
            else:
                check(name, fn)
        if link_error is None:
            print("INFO symbolic links created: all checks above ran", flush=True)
        else:
            print(f"INFO symbolic link checks skipped: {link_error}", flush=True)
    finally:
        remove_links(created)
        shutil.rmtree(HOME, ignore_errors=True)
    print(f"TOTAL PASS={COUNTS['PASS']} FAIL={COUNTS['FAIL']} SKIP={COUNTS['SKIP']}", flush=True)
    return 1 if COUNTS["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
