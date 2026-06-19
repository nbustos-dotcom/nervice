"""Self-read (READ-ONLY): let Nervice read and reason over its OWN source under the live repo root.

Confined to the repo root (~/nervice): every path is resolved and refused if it lands outside the root
or uses traversal. There is NO write, delete, rename, or exec path in this module — it only OPENS files
for reading and runs a read-only `git grep`. (Subject A is read-only by construction; subject B's
markdown-allowlist self-edit is the only self-write path, and it drafts into a clone, never live.)
"""
import pathlib
import subprocess

_REPO = pathlib.Path(__file__).resolve().parent.parent      # ~/nervice (the live repo root)
_MAX_BYTES = 200_000                                         # cap one read so a huge file can't blow context


def _resolve_in_repo(path):
    """Resolve `path` (relative -> repo root, ~ expanded) and CONFINE to the repo root. Traversal and
    anything outside the root are refused. Returns (True, resolved_Path) or (False, reason)."""
    try:
        p = pathlib.Path(str(path)).expanduser()
        p = (p if p.is_absolute() else (_REPO / p)).resolve()
    except Exception as e:
        return False, f"unresolvable path ({type(e).__name__})"
    try:
        p.relative_to(_REPO)                                # must be a descendant of the repo root
    except ValueError:
        return False, f"'{p}' is outside the repo root ({_REPO}) - refused"
    return True, p


def read_source(path) -> dict:
    """READ-ONLY: read a file under the repo root for the brain to reason over. Returns
    {ok, path, text, bytes, truncated} or {ok:False, error}. Never writes."""
    ok, info = _resolve_in_repo(path)
    if not ok:
        return {"ok": False, "error": f"REFUSED: {info}"}
    p = info
    if not p.is_file():
        rel = p.relative_to(_REPO).as_posix() if str(p).startswith(str(_REPO)) else str(p)
        return {"ok": False, "error": f"REFUSED: '{rel}' is not a file"}
    try:
        data = p.read_bytes()[:_MAX_BYTES + 1]
    except Exception as e:
        return {"ok": False, "error": f"could not read: {type(e).__name__}"}
    truncated = len(data) > _MAX_BYTES
    return {"ok": True, "path": p.relative_to(_REPO).as_posix(),
            "text": data[:_MAX_BYTES].decode("utf-8", "replace"),
            "bytes": p.stat().st_size, "truncated": truncated}


def search_source(query, max_hits: int = 30) -> dict:
    """READ-ONLY: `git grep` the repo (tracked files) for `query`, so the brain can LOCATE which file
    handles something before reading it. Returns {ok, query, hits:[file:line: text], count}. The query
    is passed as a literal pattern argument (`-e`), never through a shell. Never writes."""
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    try:
        r = subprocess.run(["git", "-C", str(_REPO), "grep", "-n", "-I", "--no-color", "-i",
                            "--fixed-strings", "-e", q],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {"ok": False, "error": f"search failed: {type(e).__name__}"}
    hits = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][:max_hits]
    return {"ok": True, "query": q, "hits": hits, "count": len(hits)}
