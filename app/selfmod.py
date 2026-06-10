"""The self-modification gate. ALL enforcement here is code, not model judgment:
fail-closed path allowlist, clean-tree requirement, dry-run, compile check, safety-floor
assertion, and rollback on any failure. Nothing is applied without an explicit human approval
in the REPL. The floor (app/safety.py), the jail (app/agent.py), and this gate (app/selfmod.py)
are excluded from EDITABLE — they cannot modify themselves."""

import re
import json
import shutil
import tempfile
import pathlib
import datetime
import subprocess

from app.agent import propose_agent

# The ONLY files a proposal may touch. Anything outside this set is rejected (fail closed).
EDITABLE = {
    "app/persona.py", "app/router.py", "app/tools.py", "app/llm.py",
    "app/chat.py", "app/memory.py", "app/retrieval.py", "app/embeddings.py",
}

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROPOSALS_DIR = REPO_ROOT / "proposals"
PYEXE = REPO_ROOT / ".venv" / "Scripts" / "python.exe"


# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _slug() -> str:
    # timestamp id, microseconds for collision-safety within a REPL turn
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd or REPO_ROOT))


def stage() -> str:
    """Create a temp dir containing app/ with ONLY the EDITABLE files (byte-identical copies, so
    diffs apply cleanly at repo root). Deterministic: exactly those files, nothing else — no .env,
    no safety.py, no agent.py, no selfmod.py. Returns the staging dir path."""
    tmp = tempfile.mkdtemp(prefix="nervice-stage-")
    for rel in sorted(EDITABLE):
        src = REPO_ROOT / rel
        dst = pathlib.Path(tmp) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tmp


def extract_diff(text: str) -> str | None:
    """Pull the first fenced unified diff. Prefers a ```diff fence; falls back to a plain fence
    whose body looks like a diff (model occasionally drops the language tag)."""
    for pat in (r"```diff\s*\n(.*?)```", r"```\s*\n(diff --git .*?)```", r"```\s*\n(--- .*?)```"):
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip("\n")
    return None


def diff_paths(diff: str) -> set[str]:
    """Target paths from +++/--- lines, stripping a/ b/ prefixes and ignoring /dev/null."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            p = line[4:].split("\t")[0].strip()
            if p in ("/dev/null", ""):
                continue
            if p.startswith(("a/", "b/")):
                p = p[2:]
            paths.add(p.replace("\\", "/"))
    return paths


def _after_fence(text: str) -> str:
    """The explanatory text following the first diff fence."""
    m = re.search(r"```(?:diff)?\s*\n.*?```(.*)", text, re.DOTALL)
    tail = (m.group(1).strip() if m else "")
    return tail or text.strip()[:400]


def _write_record(rec: dict, diff: str | None = None) -> None:
    PROPOSALS_DIR.mkdir(exist_ok=True)
    (PROPOSALS_DIR / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    if diff is not None:
        (PROPOSALS_DIR / f"{rec['id']}.patch").write_text(diff.rstrip("\n") + "\n", encoding="utf-8")


# ---------- proposal lifecycle ----------

async def propose(instruction: str) -> dict:
    """Stage editable files -> read-only proposer -> validate -> persist a PENDING proposal.
    NEVER applies. Returns the proposal record (status pending) or a rejection record."""
    PROPOSALS_DIR.mkdir(exist_ok=True)
    pid = _slug()
    staging = stage()
    try:
        response = await propose_agent(instruction, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    diff = extract_diff(response)
    if not diff:
        rec = {"id": pid, "instruction": instruction, "summary": _after_fence(response),
               "paths": [], "created": _now_iso(), "status": "rejected",
               "reason": "no diff block found in the proposer's output"}
        _write_record(rec)
        return rec

    paths = diff_paths(diff)
    if not paths or not paths.issubset(EDITABLE):
        bad = sorted(paths - EDITABLE) or ["(empty path set)"]
        rec = {"id": pid, "instruction": instruction, "summary": _after_fence(response),
               "paths": sorted(paths), "created": _now_iso(), "status": "rejected",
               "reason": f"diff touches non-editable or unknown paths: {bad}"}
        _write_record(rec, diff)  # keep the patch for forensic inspection
        return rec

    rec = {"id": pid, "instruction": instruction, "summary": _after_fence(response),
           "paths": sorted(paths), "created": _now_iso(), "status": "pending"}
    _write_record(rec, diff)
    return rec


def list_proposals() -> list[dict]:
    out = []
    if PROPOSALS_DIR.exists():
        for jf in sorted(PROPOSALS_DIR.glob("*.json")):
            try:
                out.append(json.loads(jf.read_text(encoding="utf-8")))
            except Exception:
                continue
    return out


def get(pid: str) -> dict | None:
    jf = PROPOSALS_DIR / f"{pid}.json"
    if not jf.exists():
        return None
    return json.loads(jf.read_text(encoding="utf-8"))


def reject(pid: str) -> tuple[bool, str]:
    rec = get(pid)
    if rec is None:
        return False, f"no proposal {pid}"
    rec["status"] = "rejected"
    rec["reason"] = rec.get("reason") or "rejected by Nate"
    _write_record(rec)
    return True, f"rejected {pid}"


def apply(pid: str) -> tuple[bool, str]:
    """Apply a pending proposal under full enforcement. Any failure rolls the tree back.
    Returns (ok, message)."""
    rec = get(pid)
    if rec is None:
        return False, f"no proposal {pid}"
    if rec.get("status") != "pending":
        return False, f"proposal {pid} is '{rec.get('status')}', not pending"
    patch = PROPOSALS_DIR / f"{pid}.patch"
    if not patch.exists():
        return False, f"patch file missing for {pid}"

    # 1. clean working tree required — but tolerate proposals/, the gate's own untracked
    #    bookkeeping (step 7 sweeps it into the commit as audit history). Any uncommitted CODE
    #    change anywhere else aborts, so the self-update lands as an isolated, reversible commit.
    st = _git("status", "--porcelain")
    dirty = [ln for ln in st.stdout.splitlines()
             if ln.strip() and not ln[3:].strip().startswith("proposals/")]
    if dirty:
        return False, "working tree not clean (code changes outside proposals/) — commit or stash first:\n" + "\n".join(dirty)

    # 2. re-validate paths from BOTH the record and the patch itself (defense in depth)
    rec_paths = set(rec.get("paths", []))
    patch_paths = diff_paths(patch.read_text(encoding="utf-8"))
    for label, ps in (("record", rec_paths), ("patch", patch_paths)):
        if not ps or not ps.issubset(EDITABLE):
            return False, f"path re-validation failed ({label}): {sorted(ps - EDITABLE) or '(empty)'}"

    # 3. dry-run
    chk = _git("apply", "--check", str(patch))
    if chk.returncode != 0:
        return False, f"git apply --check failed (not applied):\n{chk.stderr.strip()}"

    # 4. apply
    ap = _git("apply", str(patch))
    if ap.returncode != 0:
        return False, f"git apply failed (not applied):\n{ap.stderr.strip()}"

    # 5. compile every changed file
    for rel in sorted(patch_paths):
        cp = subprocess.run([str(PYEXE), "-m", "py_compile", str(REPO_ROOT / rel)],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
        if cp.returncode != 0:
            _git("checkout", "--", ".")
            return False, f"py_compile failed for {rel} — rolled back:\n{cp.stderr.strip()}"

    # 6. safety-floor assertion (the floor must survive any persona edit)
    fc = subprocess.run(
        [str(PYEXE), "-c",
         "from app.persona import PERSONA; from app.safety import SAFETY_FLOOR; assert SAFETY_FLOOR in PERSONA"],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    if fc.returncode != 0:
        _git("checkout", "--", ".")
        return False, f"safety-floor assertion failed — rolled back:\n{fc.stderr.strip()}"

    # 7. commit
    summary_first = (rec.get("summary") or "").strip().splitlines()
    summary_line = summary_first[0] if summary_first else pid
    _git("add", "-A")
    msg = f"self-update: {summary_line} (approved by Nate)"
    cm = _git("commit", "-m", msg)
    if cm.returncode != 0:
        _git("checkout", "--", ".")
        return False, f"git commit failed — rolled back:\n{cm.stderr.strip()}"

    # 8. mark applied
    rec["status"] = "applied"
    rec["applied"] = _now_iso()
    _write_record(rec)
    return True, (f"applied {pid} and committed: \"{msg}\". "
                  "Restart the REPL to load the change — the running process still holds the old modules.")
