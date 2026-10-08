import os, tempfile
_HOME = tempfile.mkdtemp(prefix="sg_test_deploy_home_")
os.environ["SESSION_GUARD_HOME"] = _HOME  # set before any plugin code is imported

import hashlib, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import guardkit as gk  # noqa: E402
assert str(gk.CLAUDE_DIR).startswith(_HOME), "test would touch real data"

# The deploy tool runs every tests/test_*.py before it copies. Running this file's
# real neighbours from here would recurse, so every deploy call in this file runs
# against a fixture copy of the repo whose tests folder holds only fast tests.
RESULTS = []
def check(name, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))

FIX = Path(tempfile.mkdtemp(prefix="deploy_fix_", dir=_HOME))
NAMES = ["guardkit.py", "api_repair_v2.py"]

def make_repo(label):
    root = FIX / label
    (root / "tools").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    shutil.copy2(REPO / "tools" / "deploy.py", root / "tools" / "deploy.py")
    for name in NAMES:
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    (root / "tests" / "test_fake_ok.py").write_text('print("fake ok")\n', encoding="utf-8")
    return root

def deploy(root, *args):
    r = subprocess.run([sys.executable, "-I", str(root / "tools" / "deploy.py"), *[str(a) for a in args]],
                       capture_output=True, cwd=str(root))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

def status_of(out, name):
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

root = make_repo("repo")
src = root / "scripts"
T = FIX / "t1" / "session-tools"
B = FIX / "t1" / "backups"

# 1. dry run writes nothing
rc, out = deploy(root, "--target", T, "--backup-root", B)
check("dry run exits 0", rc == 0, f"rc={rc}")
check("dry run runs the gate and it passes", "gate: PASS tests/test_fake_ok.py" in out)
check("dry run reports both files as new", all(status_of(out, n) == "new" for n in NAMES),
      str([status_of(out, n) for n in NAMES]))
check("dry run created nothing (no target, no backup root)", not (FIX / "t1").exists())

# 2. --apply into an empty target copies both files
rc, out = deploy(root, "--apply", "--target", T, "--backup-root", B)
check("apply into empty target exits 0", rc == 0, f"rc={rc}")
check("apply reports both files as new", all(status_of(out, n) == "new" for n in NAMES))
check("apply copied both files byte for byte", all((T / n).read_bytes() == (src / n).read_bytes() for n in NAMES))
check("apply left only allowlisted names (no temp files)", sorted(p.name for p in T.iterdir()) == sorted(NAMES),
      str(sorted(p.name for p in T.iterdir())))
check("apply into an empty target keeps no backup", not B.exists())

# 3. a second --apply copies nothing
before = {n: (T / n).stat().st_mtime_ns for n in NAMES}
rc, out = deploy(root, "--apply", "--target", T, "--backup-root", B)
check("second apply exits 0 and reports same", rc == 0 and all(status_of(out, n) == "same" for n in NAMES),
      str([status_of(out, n) for n in NAMES]))
check("second apply does not rewrite any file", {n: (T / n).stat().st_mtime_ns for n in NAMES} == before)
check("second apply makes no backup", not B.exists())

# 4. a differing target is backed up first, then replaced
OLD = b"# old copy, not the source\n"
(T / "guardkit.py").write_bytes(OLD)
rc, out = deploy(root, "--apply", "--target", T, "--backup-root", B)
check("changed target reports differs; the other file stays same",
      rc == 0 and status_of(out, "guardkit.py") == "differs" and status_of(out, "api_repair_v2.py") == "same", out.strip().splitlines()[-1])
backups = list(B.glob("*/guardkit.py"))
check("the old content is kept under the backup root", len(backups) == 1 and backups[0].read_bytes() == OLD,
      str([str(b) for b in backups]))
check("only the changed file was backed up", sorted(p.name for p in B.glob("*/*")) == ["guardkit.py"])
check("target now equals source after the replace", all(sha(T / n) == sha(src / n) for n in NAMES))

# 5. a failing test blocks the deploy: nothing is copied
bad = make_repo("repo_fail")
(bad / "tests" / "test_zz_fail.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
T5 = FIX / "t5" / "session-tools"
B5 = FIX / "t5" / "backups"
rc, out = deploy(bad, "--apply", "--target", T5, "--backup-root", B5)
check("failing test blocks the deploy (non-zero exit)", rc != 0 and "gate: FAIL tests/test_zz_fail.py" in out, f"rc={rc}")
check("nothing copied and no target created when the gate fails", not (FIX / "t5").exists())
(bad / "tests" / "test_zz_fail.py").unlink()  # the temporary failing file is removed

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if all(RESULTS) else 1)
