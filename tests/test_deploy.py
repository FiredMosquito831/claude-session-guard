import os, tempfile
_HOME = tempfile.mkdtemp(prefix="sg_test_deploy_home_")
os.environ["SESSION_GUARD_HOME"] = _HOME  # set before any plugin code is imported

import hashlib, json, shutil, subprocess, sys
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

# 6. manifest and read-only --check (PR-17). Each block has its own target and backup root under FIX.
# The manifest is written to deploy-manifests, a sibling of the backup root.
def manifests_in(folder):
    return sorted(folder.glob("*.json")) if folder.is_dir() else []

def load_dict(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def file_field(manifest, name, key):
    files = manifest.get("files")
    entry = files.get(name) if isinstance(files, dict) else None
    return entry.get(key) if isinstance(entry, dict) else None

def snapshot(folder):
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in [folder, *sorted(folder.rglob("*"))]}

MR = make_repo("repo_manifest")
MS = MR / "scripts"
MF = FIX / "t6"
MT, MB, MM = MF / "session-tools", MF / "backups", MF / "deploy-manifests"
MT.mkdir(parents=True)
(MT / "guardkit.py").write_bytes(OLD)  # differs; api_repair_v2.py is absent, so new
rc, out = deploy(MR, "--apply", "--target", MT, "--backup-root", MB)
m1 = manifests_in(MM)
man = load_dict(m1[0]) if len(m1) == 1 else {}
check("apply writes a manifest with both hashes",
      rc == 0 and len(m1) == 1 and man.get("schema") == 1
      and all(file_field(man, n, "src_sha256") == sha(MS / n) for n in NAMES)
      and all(file_field(man, n, "dst_sha256_after") == sha(MS / n) for n in NAMES)
      and file_field(man, "guardkit.py", "status") == "differs"
      and file_field(man, "guardkit.py", "dst_sha256_before") == hashlib.sha256(OLD).hexdigest()
      and file_field(man, "api_repair_v2.py", "status") == "new"
      and file_field(man, "api_repair_v2.py", "dst_sha256_before") is None,
      f"rc={rc} manifests={len(m1)} schema={man.get('schema')}")

S2 = MF / "same"
MT2, MB2, MM2 = S2 / "session-tools", S2 / "backups", S2 / "deploy-manifests"
MT2.mkdir(parents=True)
for n in NAMES:
    shutil.copy2(MS / n, MT2 / n)
rc2, out2 = deploy(MR, "--apply", "--target", MT2, "--backup-root", MB2)
m2 = manifests_in(MM2)
man2 = load_dict(m2[0]) if len(m2) == 1 else {}
check("apply with nothing to copy still writes a manifest",
      rc2 == 0 and len(m2) == 1 and all(file_field(man2, n, "status") == "same" for n in NAMES)
      and not MB2.exists(),
      f"rc={rc2} manifests={len(m2)} statuses={[file_field(man2, n, 'status') for n in NAMES]}")

check("repo_commit is null outside git", "repo_commit" in man and man["repo_commit"] is None,
      f"repo_commit={man.get('repo_commit')!r}")

rc4, out4 = deploy(MR, "--check", "--target", MT)
check("check exits 0 when every copy matches",
      rc4 == 0 and "check: all same" in out4 and status_of(out4, "guardkit.py") == "same",
      f"rc={rc4}")

orig = (MT / "guardkit.py").read_bytes()
(MT / "guardkit.py").write_bytes(bytes([orig[0] ^ 0x01]) + orig[1:])  # one byte changed
rc5, out5 = deploy(MR, "--check", "--target", MT)
check("check exits 1 for a differing copy",
      rc5 == 1 and status_of(out5, "guardkit.py") == "differs" and status_of(out5, "api_repair_v2.py") == "same",
      f"rc={rc5} {[status_of(out5, n) for n in NAMES]}")
(MT / "guardkit.py").write_bytes(orig)  # the good copy goes back

(MT / "api_repair_v2.py").unlink()
rc6, out6 = deploy(MR, "--check", "--target", MT)
check("check exits 1 for a missing copy",
      rc6 == 1 and status_of(out6, "api_repair_v2.py") == "missing" and status_of(out6, "guardkit.py") == "same",
      f"rc={rc6} {[status_of(out6, n) for n in NAMES]}")

before = (snapshot(MT), snapshot(MB), snapshot(MM))
rc7, out7 = deploy(MR, "--check", "--target", MT)
after = (snapshot(MT), snapshot(MB), snapshot(MM))
check("check writes nothing", before == after and rc7 == 1 and all(x is not None for x in before),
      f"rc={rc7}")

rc8, out8 = deploy(MR, "--check", "--apply", "--target", MT)
check("check refuses to combine with apply",
      rc8 == 2 and "refused: --check and --apply together" in out8 and not (MT / "api_repair_v2.py").exists(),
      f"rc={rc8}")

MA = MF / "absent"
rc9, out9 = deploy(MR, "--check", "--target", MA / "session-tools")
check("check does not create a missing target folder", rc9 == 1 and not MA.exists(), f"rc={rc9}")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if all(RESULTS) else 1)
