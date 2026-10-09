import os, tempfile
_HOME = tempfile.mkdtemp(prefix="sg_test_home_")
os.environ["SESSION_GUARD_HOME"] = _HOME
import importlib.util, json, os, random, sys, tempfile, time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import guardkit as gk
import api_repair_v2 as new
assert str(gk.CLAUDE_DIR).startswith(_HOME), "test would touch real data"

LEGACY = str(Path(__file__).resolve().parent.parent / "scripts" / "api_repair.py")
spec = importlib.util.spec_from_file_location("legacy_api", LEGACY)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

RESULTS = []
def check(name, ok, extra=""):
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))

def write_raw(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

def jl(objs):
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode("utf-8")

def both(raw, tag):
    tmp = Path(_HOME) / f"api_eq_{tag}.jsonl"
    write_raw(tmp, raw)
    ls, lnew, ld = legacy.analyse(tmp)
    ns, nnew, nd = new.analyse_bytes(tmp, raw)
    legacy_norm = None if lnew is None else [x.encode("utf-8", "surrogateescape") for x in lnew]
    ok = (ls.get("blocks_removed"), ls.get("lines_removed"), ls.get("relinked"), legacy_norm, [(d[0], d[2]) for d in ld]) == \
         (ns.get("blocks_removed"), ns.get("lines_removed"), ns.get("relinked"), nnew, [(d[0], d[2]) for d in nd])
    return ok

U = lambda k: f"u-{k:04d}"
def msg(mid, blocks):
    return {"type": "assistant", "message": {"id": mid, "content": blocks}}

cases = {
    "A chain: drop, relink, block removal": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "\n\n", "signature": ""}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "message": {"id": "m1", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "uuid": "a3", "parentUuid": "a2", "message": {"id": "m2", "content": [{"type": "thinking", "thinking": "  ", "signature": "s"}, {"type": "text", "text": "kept"}]}},
        {"type": "assistant", "uuid": "a4", "parentUuid": "a3", "message": {"id": "m3", "content": [{"type": "thinking", "thinking": "real reasoning", "signature": "s"}]}},
    ]),
    "B consecutive drops collapse to the root": jl([
        {"type": "user", "uuid": "u0", "parentUuid": None, "message": {"content": "x"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u0", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "", "signature": ""}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": " \n", "signature": ""}]}},
        {"type": "assistant", "uuid": "a3", "parentUuid": "a2", "message": {"id": "m1", "content": [{"type": "text", "text": "t"}]}},
    ]),
    "C child that also loses a block": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "\n", "signature": ""}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "message": {"id": "m2", "content": [{"type": "thinking", "thinking": "\n", "signature": ""}, {"type": "text", "text": "y"}]}},
    ]),
    "D no thinking at all": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u1", "message": {"id": "m9", "content": [{"type": "text", "text": "ok"}]}},
    ]),
    "E blank lines, no final newline": (jl([{"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}}])
        + b"\n" + jl([{"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "", "signature": ""}]}}]).rstrip(b"\n")),
    "F invalid UTF-8 in a text line survives": (jl([{"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}}])
        + b'{"type":"assistant","uuid":"x9","parentUuid":"u1","message":{"id":"mz","content":[{"type":"text","text":"\xff\xfe"}]}}\n'
        + jl([{"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "", "signature": ""}]}}])),
    "G null thinking is treated as empty": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": None, "signature": ""}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "message": {"id": "m1", "content": [{"type": "text", "text": "z"}]}},
    ]),
    "H non-breaking space only is empty (Python strip)": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "\u00a0\u3000", "signature": ""}]}},
    ]),
    "I escaped quote inside thinking is not empty": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "\"", "signature": ""}]}},
    ]),
    "J whitespace in a long value (many newlines)": jl([
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "\n" * 3000, "signature": ""}]}},
    ]),
}
for name, raw in cases.items():
    check("equivalence: " + name, both(raw, str(abs(hash(name)))))

def strip_cr(b):  # same body as tests/test_repair_pr03.py:113-115
    return b[:-1] if b.endswith(b"\r") else b

def touched_flags(raw):
    """One flag per raw line: True when v2 drops or rewrites it, False when it copies it byte for byte."""
    lines = raw.split(b"\n")
    parsed = []
    for ln in lines:
        try:
            o = json.loads(ln.decode("utf-8", "surrogateescape").strip())
        except ValueError:
            o = None
        parsed.append(o if isinstance(o, dict) else None)

    def blocks(o):
        m = o.get("message") if o else None
        c = m.get("content") if isinstance(m, dict) else None
        return c if isinstance(c, list) else None

    dropped_uuids = set()
    for o in parsed:
        c = blocks(o)
        if c and all(new.is_empty_thinking(b) for b in c) and o.get("uuid"):
            dropped_uuids.add(o["uuid"])
    flags = []
    for o in parsed:
        if o is None:
            flags.append(False)
            continue
        c = blocks(o)
        has_empty = bool(c) and any(new.is_empty_thinking(b) for b in c)
        relinked = isinstance(o.get("parentUuid"), str) and o["parentUuid"] in dropped_uuids
        flags.append(bool(has_empty or relinked))
    return lines, flags

# Real transcripts: a random sample of files older than one hour (the product's active-file rule),
# compared with the previous implementation. Sorted first, so one population gives one sample.
random.seed(7)
REAL_PROJECTS = Path.home() / ".claude" / "projects"  # read only
now = time.time()
allf = sorted(p for _r, p, sz, _m, mt in gk.transcript_files(REAL_PROJECTS)
              if sz < 20_000_000 and now - mt > 3600)
sample = random.sample(allf, min(120, len(allf)))
print(f"population={len(allf)} sample={len(sample)}")
mism = 0
cr_bad = 0
crlf_in_sample = 0
for p in sample:
    raw = p.read_bytes()
    crlf_in_sample += b"\r\n" in raw
    ls, lnew, ld = legacy.analyse(p)
    ns, nnew, nd = new.analyse_bytes(p, raw)
    lnorm = None if lnew is None else [strip_cr(x.encode("utf-8", "surrogateescape")) for x in lnew]
    nnorm = None if nnew is None else [strip_cr(x) for x in nnew]
    left = (ls.get("blocks_removed"), ls.get("lines_removed"), ls.get("relinked"), lnorm,
            [(d[0], strip_cr(d[2].encode("utf-8", "surrogateescape"))) for d in ld])
    right = (ns.get("blocks_removed"), ns.get("lines_removed"), ns.get("relinked"), nnorm,
             [(d[0], strip_cr(d[2].encode("utf-8", "surrogateescape"))) for d in nd])
    if left != right:
        mism += 1
        print("   mismatch in", p)
    if nnew is not None:
        lines, touched = touched_flags(raw)
        out_set = set(nnew)
        if any(ln.endswith(b"\r") and not t and ln not in out_set for ln, t in zip(lines, touched)):
            cr_bad += 1
            print("   CR lost on an untouched line in", p)
check(f"equivalence on {len(sample)} real transcripts", mism == 0 and len(sample) > 0, f"mismatches={mism}")
check("v2 keeps the CR on untouched lines (real transcripts)", cr_bad == 0, f"files_with_CR_lost={cr_bad}")
print(f"crlf_in_sample={crlf_in_sample}")

# ---- sweep, isolation, caching, gate (all on a temporary corpus) ----
tmp = Path(tempfile.mkdtemp(prefix="apiv2_", dir=_HOME))
root = tmp / "projects"
bk = tmp / "backups"
new.PROJECTS_DIR = root
new.BACKUPS_DIR = bk
new.REMOVED_LINES_ARCHIVE = bk / "removed.jsonl"
new.SCHEDULE_FILE = tmp / "schedule.json"
gk.STATE_DIR = tmp / "state"
gk.LOG_DIR = tmp / "logs"
gk.ARCHIVE_STATE = tmp / "archive-state"
gk.USAGE_STATE = tmp / "usage-state"

def dirty_raw(tag):
    return jl([
        {"type": "user", "uuid": f"u-{tag}", "parentUuid": None, "message": {"content": "hi"}},
        {"type": "assistant", "uuid": f"a-{tag}", "parentUuid": f"u-{tag}", "message": {"id": f"m-{tag}", "content": [{"type": "thinking", "thinking": "\n", "signature": ""}]}},
        {"type": "assistant", "uuid": f"b-{tag}", "parentUuid": f"a-{tag}", "message": {"id": f"m-{tag}", "content": [{"type": "text", "text": "t"}]}},
    ])
def clean_raw(tag):
    return jl([{"type": "user", "uuid": f"u-{tag}", "parentUuid": None, "message": {"content": "hi"}}])

old = time.time() - 7 * 3600
files = {
    "P1/clean1.jsonl": clean_raw("c1"), "P1/dirty.jsonl": dirty_raw("d1"),
    "P1/sess/subagents/agent-x.jsonl": dirty_raw("x"), "P2/recent.jsonl": dirty_raw("r"),
    "P2/clean2.jsonl": clean_raw("c2"), "P2/boom.jsonl": dirty_raw("b"),
}
for rel, raw in files.items():
    f = root / rel
    write_raw(f, raw)
    if not rel.endswith("recent.jsonl"):
        os.utime(f, (old, old))
# sidecar that must disappear when dirty.jsonl is repaired
sc = gk.ARCHIVE_STATE / "P1" / "dirty.jsonl.json"
sc.parent.mkdir(parents=True, exist_ok=True); sc.write_text("{}")

real_repair = new.repair_file
def flaky(p, dry_run=True, force=False):
    if p.stem == "boom":
        raise RuntimeError("injected failure")
    return real_repair(p, dry_run=dry_run, force=force)
new.repair_file = flaky
r1 = new.sweep_incremental()
new.repair_file = real_repair
check("sweep 1: repairs both dirty files, skips the recent one",
      r1["repaired"] == 2 and r1["recent"] == 1, f"repaired={r1['repaired']} recent={r1['recent']}")
check("isolation: an injected failure is counted and does not stop the others", r1["errors"] == 1, f"errors={r1['errors']}")
check("repair invalidates the archive offset sidecar", not sc.exists())
rem = (bk / "removed.jsonl").read_text(encoding="utf-8").strip().splitlines()
check("removed lines were archived before the rewrite", len(rem) >= 2, f"archived={len(rem)}")
bad = [p for p in root.rglob("*.jsonl") if p.stem != "recent" and b'"thinking":"\\n"' in p.read_bytes()]
check("no empty thinking block remains in repaired files", not bad)
r2 = new.sweep_incremental()
check("sweep 2 re-checks only what changed or failed", r2["checked"] <= 4 and r2["clean"] >= 2,
      f"checked={r2['checked']} clean={r2['clean']}")
r3 = new.sweep_incremental()
check("sweep 3 is pure cache: nothing is re-read", r3["checked"] == 0 and r3["clean"] >= 2,
      f"checked={r3['checked']} clean={r3['clean']}")

# gate
T0 = 1_700_000_000.0
new.SCHEDULE_FILE.unlink(missing_ok=True)
ok, _ = new.schedule_allows(T0); check("gate: first sweep is allowed", ok)
new.schedule_record(T0)
ok, why = new.schedule_allows(T0 + 3 * 3600); check("gate: 3 h after a run is refused (cooldown)", not ok, why)
ok, _ = new.schedule_allows(T0 + 6 * 3600 + 1); check("gate: 6 h after a run is allowed", ok)
new.schedule_record(T0 + 6 * 3600 + 1)
new.SCHEDULE_FILE.write_text(json.dumps({"runs": [T0 - 7 * 3600, T0 - 13 * 3600, T0 - 19 * 3600, T0 - 23.5 * 3600]}))
ok, why = new.schedule_allows(T0); check("gate: 4 runs inside 24 h refuse a fifth even after the cooldown", not ok, why)
new.SCHEDULE_FILE.unlink(missing_ok=True)
new.schedule_record(time.time())
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = new.sweep(force=False)
check("sweep() respects the cooldown and exits quickly", rc == 0 and "cooldown" in buf.getvalue(), buf.getvalue().strip())

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if RESULTS and all(RESULTS) else 1)
