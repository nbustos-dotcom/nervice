"""Self-edit (markdown-allowlist, DRAFT-ONLY): the ONLY path by which Nervice may propose a change to
its OWN files. It is fenced three independent ways so it can never touch code or anything sensitive:

  1. DEFAULT-DENY ALLOWLIST (in code, below). A target is editable ONLY if it (a) resolves INSIDE the
     repo root, (b) ends in `.md`, and (c) is an exact-listed file OR lives under an allowed dir. The
     default is NO — anything not explicitly allowed is refused. This is an allowlist, not a blocklist.
  2. MARKDOWN ONLY. The `.md` requirement means no `.py` can EVER match, so the five guard files
     (safety.py, selfmod.py, persona.py, agent.py, usage.py) are structurally unreachable here.
  3. DRAFT INTO A CLONE. The edit is written into a throwaway `git clone` of the repo, never the live
     tree, and the clone is deleted after the diff is captured. The output is a unified diff for Nate to
     review and apply BY HAND. There is deliberately NO auto-apply-to-live path in this module.

`is_editable(path)` is the guard (pure path logic, no I/O). `draft_edit(path, new_content)` runs the
guard, then clones + drafts + diffs. Applying a draft to the live tree is Nate's manual step, not ours.
"""
import pathlib
import shutil
import subprocess
import tempfile

_REPO = pathlib.Path(__file__).resolve().parent.parent      # ~/nervice (the live repo root)

# ── DEFAULT-DENY ALLOWLIST — the COMPLETE set of self-editable paths. Nothing not here is editable. ──
_ALLOWED_EXACT = {"README.md", "BACKLOG.md", ".claude/skills/SKILLS_CATALOG.md"}
_ALLOWED_DIRS = ("docs",)                                   # any *.md under docs/ (recursively)


def is_editable(path):
    """The guard. Returns (True, repo_relative_posix) if `path` is on the allowlist, else (False, reason).
    Default-deny: editable ONLY if it resolves inside the repo, is a `.md` file, AND is exact-listed or
    under an allowed dir. Traversal is judged by where it RESOLVES (so docs/../app/agent.py -> app/agent.py
    -> not .md -> refused). Outside-repo, non-.md (every .py, every guard file), and unlisted -> refused."""
    raw = str(path or "").strip()
    if not raw:
        return False, "REFUSED: empty path"
    try:
        p = pathlib.Path(raw).expanduser()
        p = (p if p.is_absolute() else (_REPO / p)).resolve()   # resolve FIRST, then judge
    except Exception as e:
        return False, f"REFUSED: unresolvable path ({type(e).__name__})"
    try:
        rel = p.relative_to(_REPO).as_posix()                   # must be a descendant of the repo root
    except ValueError:
        return False, f"REFUSED: '{p}' is outside the repo root - not editable"
    if p.suffix.lower() != ".md":
        return False, f"REFUSED: '{rel}' is not a .md file - only markdown is self-editable"
    if rel in _ALLOWED_EXACT:
        return True, rel
    if rel.split("/", 1)[0] in _ALLOWED_DIRS:                   # e.g. docs/anything.md  (but not docsx/..)
        return True, rel
    return False, f"REFUSED: '{rel}' is not on the self-edit allowlist"


def draft_edit(path, new_content) -> dict:
    """DRAFT-ONLY: if `path` is on the allowlist, clone the repo to a throwaway sandbox, write
    `new_content` THERE, capture a unified diff, delete the sandbox, and return the diff for Nate to
    review and apply by hand. The live tree is NEVER written; there is no auto-apply. Returns
    {ok, path, existed, diff, note} or {ok:False, error}."""
    ok, info = is_editable(path)
    if not ok:
        return {"ok": False, "error": info}                    # guard refused — never reaches the clone
    rel = info
    if new_content is None:
        return {"ok": False, "error": "REFUSED: no content to draft"}
    text = str(new_content)
    sandbox = pathlib.Path(tempfile.mkdtemp(prefix="nervice-selfedit-"))
    clone = sandbox / "repo"
    try:
        r = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(_REPO), str(clone)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"ok": False, "error": f"clone failed: {(r.stderr or '').strip()[:200]}"}
        target = clone / rel
        target.resolve().relative_to(clone.resolve())          # re-assert confinement INSIDE the clone
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(text, encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(clone), "add", "-N", "--", rel],
                       capture_output=True, text=True, timeout=30)   # intent-to-add so new files diff too
        d = subprocess.run(["git", "-C", str(clone), "diff", "--", rel],
                           capture_output=True, text=True, timeout=30)
        return {"ok": True, "path": rel, "existed": existed, "diff": d.stdout,
                "note": "DRAFT only - review this diff and apply by hand; the live tree was never touched."}
    except ValueError:
        return {"ok": False, "error": "REFUSED: target escaped the sandbox"}
    except Exception as e:
        return {"ok": False, "error": f"draft failed: {type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)             # no sandbox accumulation
