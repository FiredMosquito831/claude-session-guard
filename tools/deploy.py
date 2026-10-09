#!/usr/bin/env python3
"""deploy -- copy allowlisted scripts from this repo to the live tools folder.

A dry run by default. Nothing is written unless --apply is given.

Before anything else, every tests/test_*.py in this repo is run with python -I.
If any test fails, or there are no tests, the deploy exits non-zero and copies nothing.

Only the names in ALLOWLIST are ever copied. The deploy never reads or writes a
settings file and never touches hooks.

  python tools/deploy.py                        dry run, default target (the live folder)
  python tools/deploy.py --target DIR           dry run, against DIR
  python tools/deploy.py --target DIR --apply   copy new and differing files to DIR
  python tools/deploy.py --check --target DIR   read only: compare repo copies with DIR (exit 0 only if all same)
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk  # noqa: E402  CLAUDE_DIR honours SESSION_GUARD_HOME, as the tools do

# Fixed allowlist. Other scripts join in their own PRs.
ALLOWLIST = ["guardkit.py", "api_repair_v2.py"]
GATE_TIMEOUT_S = 900


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_gate(repo: Path) -> bool:
    """Run every tests/test_*.py with python -I. True only if all pass and at least one exists."""
    tests = sorted((repo / "tests").glob("test_*.py"))
    if not tests:
        print("gate: no tests found; refusing to deploy")
        return False
    ok = True
    for t in tests:
        name = t.relative_to(repo).as_posix()
        try:
            r = subprocess.run([sys.executable, "-I", str(t)], cwd=str(repo),
                               capture_output=True, timeout=GATE_TIMEOUT_S)
            rc = r.returncode
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            rc, out = -1, "timed out"
        print(f"gate: {'PASS' if rc == 0 else 'FAIL'} {name} exit={rc}")
        if rc != 0:
            ok = False
            print("\n".join(out.splitlines()[-30:]))
    return ok


def plan(src_dir: Path, target: Path):
    """For each allowlisted name: (name, new|same|differs, src sha, dst sha or None)."""
    rows = []
    for name in ALLOWLIST:
        src, dst = src_dir / name, target / name
        if not src.is_file():
            raise OSError(f"source missing: {src}")
        s = sha256(src)
        if not dst.exists():
            status, d = "new", None
        elif not dst.is_file():
            raise OSError(f"target is not a file: {dst}")
        else:
            d = sha256(dst)
            status = "same" if d == s else "differs"
        rows.append((name, status, s, d))
    return rows


def write_atomic(src: Path, dst: Path) -> None:
    """Write src's bytes to a temp file in dst's folder, fsync it, then os.replace it into place."""
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            with open(src, "rb") as s:
                shutil.copyfileobj(s, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def repo_commit(repo: Path):
    """git HEAD of the repo folder, or None if git is missing or fails. Never raises."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace").strip() or None


def manifest_path(backup_root: Path, stamp: str) -> Path:
    """Manifests sit in a sibling folder of the backup root, named after the run stamp."""
    return backup_root.parent / "deploy-manifests" / f"{stamp}.json"


def write_manifest(path: Path, data: dict) -> None:
    """Write data as JSON: temp file in the same folder, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(rows, src_dir: Path, target: Path, backup_root: Path):
    """Copy every new or differing file. A differing target is backed up first. Returns counts.

    After a successful run, a manifest is written to manifest_path(), every time,
    including when nothing was copied.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = backup_root / stamp
    target.mkdir(parents=True, exist_ok=True)
    copied, backed_up = 0, 0
    for name, status, s, _d in rows:
        if status == "same":
            continue
        src, dst = src_dir / name, target / name
        if status == "differs":
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup_dir / name)
            backed_up += 1
        write_atomic(src, dst)
        if sha256(dst) != s:
            raise OSError(f"{name}: target does not match source after the copy")
        copied += 1
    files = {}
    for name, status, s, d in rows:
        files[name] = {
            "status": status,
            "src_sha256": s,
            "dst_sha256_before": d,
            "dst_sha256_after": sha256(target / name),
        }
    manifest = {
        "schema": 1,
        "deployed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_commit": repo_commit(REPO),
        "target": str(target),
        "backup_root": str(backup_root),
        "files": files,
    }
    write_manifest(manifest_path(backup_root, stamp), manifest)
    return copied, backed_up, backup_dir


def show(rows) -> None:
    for name, status, s, d in rows:
        print(f"  {name:<20} {status:<8} src={s[:12]}  dst={d[:12] if d else '-'}")


def run_check(target: Path) -> int:
    """Read only. Compare the repo copies with target. Writes nothing and creates no folder.

    Returns 0 only when every allowlisted name is same. A missing copy counts as missing.
    """
    print("mode: check (read only)")
    print(f"target: {target}")
    try:
        rows = plan(SCRIPTS, target)
    except OSError as e:
        print(f"check: {e}")
        return 1
    bad = 0
    for name, status, s, d in rows:
        if status == "new":
            status = "missing"
        if status != "same":
            bad += 1
        print(f"  {name:<20} {status:<8} repo={s[:12]}  live={d[:12] if d else '-'}")
    if bad:
        print(f"check: {bad} differs or missing")
        return 1
    print("check: all same")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Copy allowlisted scripts to the live tools folder. Dry run unless --apply.")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry run)")
    ap.add_argument("--target", type=Path, default=gk.CLAUDE_DIR / "session-tools",
                    help="destination folder (default: <CLAUDE_DIR>/session-tools)")
    ap.add_argument("--backup-root", type=Path, default=gk.CLAUDE_DIR / "backups" / "deploy",
                    help="where replaced files are kept (default: <CLAUDE_DIR>/backups/deploy)")
    ap.add_argument("--check", action="store_true",
                    help="read only: compare repo copies with --target; exit 0 only if all are same")
    args = ap.parse_args(argv)

    if args.check:
        if args.apply:
            print("refused: --check and --apply together")
            return 2
        return run_check(args.target)

    print(f"mode: {'APPLY' if args.apply else 'dry run'}")
    print(f"target: {args.target}")
    print(f"backup root: {args.backup_root}")
    if not run_gate(REPO):
        print("deploy refused: the tests did not all pass. Nothing was copied.")
        return 1
    try:
        rows = plan(SCRIPTS, args.target)
    except OSError as e:
        print(f"deploy refused: {e}")
        return 1
    show(rows)
    if not args.apply:
        print("dry run: nothing written. Re-run with --apply to copy new and differing files.")
        return 0
    try:
        copied, backed_up, backup_dir = apply(rows, SCRIPTS, args.target, args.backup_root)
    except OSError as e:
        print(f"deploy failed: {e}")
        return 1
    print(f"applied: copied={copied} backed_up={backed_up} same={sum(1 for r in rows if r[1] == 'same')}")
    if backed_up:
        print(f"backups: {backup_dir}")
    print(f"manifest: {manifest_path(args.backup_root, backup_dir.name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
