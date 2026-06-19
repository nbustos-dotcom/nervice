"""Lock the self-knowledge safety boundaries (subjects A + B).

A — app/self_read.py: read-only, repo-confined. Reads a real file; refuses outside-repo, traversal,
    non-file. (No write path exists in the module; this test also asserts that by source inspection.)
B — app/self_edit.py: default-deny markdown allowlist, draft-only. Accepts ONLY allowed .md; refuses
    every .py (incl. the five guard files), traversal-to-.py, outside-repo, and non-.md in allowed dirs.
    draft_edit() never writes the live tree.

Run: PYTHONPATH=<repo> python scripts/test_self_knowledge.py   (offline; needs git on PATH). rc=0 = pass.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))   # repo root importable

from app import self_read, self_edit

_REPO = pathlib.Path(__file__).resolve().parent.parent


# ───────────────────────── A: self_read (READ-ONLY, repo-confined) ─────────────────────────
def test_self_read():
    r = self_read.read_source("app/usage.py")
    assert r["ok"] and r["path"] == "app/usage.py" and "spend guard" in r["text"], r
    assert r["bytes"] > 0 and r["truncated"] is False, r

    # outside the repo (absolute + ~), traversal out, and a dir (non-file) are all refused
    for bad in ["C:/Windows/win.ini", "~/.bashrc", "../../Windows/System32/drivers/etc/hosts"]:
        rr = self_read.read_source(bad)
        assert not rr["ok"] and "REFUSED" in rr["error"], (bad, rr)
    assert not self_read.read_source("app/..")["ok"], "a directory must not read as a file"

    # locate-then-read works
    s = self_read.search_source("free_only")
    assert s["ok"] and s["count"] > 0 and any("usage.py" in h or "api.py" in h for h in s["hits"]), s

    # by source inspection: NO write/delete/exec verb appears in the module (read-only by construction)
    src = (_REPO / "app" / "self_read.py").read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"', "'")))
    for forbidden in ["open(", ".write", "write_text", "write_bytes", "unlink", "rmtree",
                      "os.remove", "os.rename", "shell=True", "os.system", "eval(", "exec("]:
        assert forbidden not in body, f"self_read.py must have no write/exec path, found {forbidden!r}"
    print("  A self_read: reads real file; refuses outside/traversal/non-file; no write path. OK")


# ───────────────────────── B: self_edit (default-deny markdown allowlist) ─────────────────────────
def test_self_edit_allowlist():
    # ACCEPTED — exactly the allowlist
    for ok_path in ["README.md", "BACKLOG.md", ".claude/skills/SKILLS_CATALOG.md",
                    "docs/PROJECT_STATE.md", "docs/sub/deep.md"]:
        ok, info = self_edit.is_editable(ok_path)
        assert ok, f"{ok_path} should be editable, got {info}"

    # REFUSED — every .py (incl. guard files), traversal-to-.py, outside-repo, non-.md, prefix attack
    refuse = [
        "app/router.py", "app/safety.py", "app/selfmod.py", "app/persona.py", "app/agent.py",
        "app/usage.py",                       # the five guard files + router: all .py -> unreachable
        "docs/../app/agent.py",               # traversal resolving to a .py guard
        "../evil.md", "~/.bashrc", "C:/Windows/win.ini",   # outside the repo
        "docs/secret.txt", "README.txt",      # non-.md
        "docsx/foo.md",                       # prefix attack on the allowed 'docs' dir
        "", "   ",                            # empty
    ]
    for bad in refuse:
        ok, info = self_edit.is_editable(bad)
        assert not ok and "REFUSED" in info or info in ("REFUSED: empty path",), f"{bad!r} must refuse, got {(ok, info)}"

    # none of the five guard files can EVER be editable (the load-bearing guarantee)
    for guard in ["app/safety.py", "app/selfmod.py", "app/persona.py", "app/agent.py", "app/usage.py"]:
        assert not self_edit.is_editable(guard)[0], f"GUARD FILE {guard} became editable!"
    print("  B is_editable: allowlist accepts only allowed .md; every .py + traversal + outside refused. OK")


def test_self_edit_draft_only():
    # allowed file -> draft produces a diff, and the LIVE tree is never written
    sentinel = _REPO / "docs" / "_SELFEDIT_TEST_SHOULD_NOT_EXIST.md"
    assert not sentinel.exists(), "precondition: sentinel must not pre-exist"
    r = self_edit.draft_edit("docs/_SELFEDIT_TEST_SHOULD_NOT_EXIST.md", "# draft\nonly in the clone\n")
    assert r["ok"] and r["diff"].strip() and "_SELFEDIT_TEST_SHOULD_NOT_EXIST" in r["diff"], r
    assert not sentinel.exists(), "draft_edit MUST NOT write the live tree"

    # refused file -> never reaches a clone, returns an error, writes nothing
    r2 = self_edit.draft_edit("app/safety.py", "malicious")
    assert not r2["ok"] and "REFUSED" in r2["error"], r2

    # by source inspection: the ONLY write_text is gated — is_editable is called before it
    src = (_REPO / "app" / "self_edit.py").read_text(encoding="utf-8")
    assert src.count("write_text") == 1, "expected exactly one write point in self_edit.py"
    assert src.index("is_editable(path)") < src.index("write_text"), "the write must be gated by is_editable first"
    print("  B draft_edit: allowed->diff, live tree untouched; refused->error; single gated write point. OK")


if __name__ == "__main__":
    test_self_read()
    test_self_edit_allowlist()
    test_self_edit_draft_only()
    print("=== ALL SELF-KNOWLEDGE ASSERTIONS PASSED ===")
