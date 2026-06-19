"""List + prune throwaway sandboxes under ~/nervice-cc-sandbox.

DRY-RUN by default. The real prune removes ONLY directories strictly UNDER the sandbox root — a hard
default-deny guard resolves every path (symlinks / .. included) and REFUSES anything outside the root,
the root itself, or the live repo ~/nervice. No aggressive auto-delete: Nate triggers the prune after
reading a dry-run; policies are age (older than N days) and/or keep-last-N. Leaf module: stdlib only.
"""
import shutil
import pathlib
from datetime import datetime, timezone

# The ONE root this module may ever touch. Resolved so a crafted path can't slip past via .. or a symlink.
_ROOT = (pathlib.Path.home() / "nervice-cc-sandbox").resolve()


def _is_sandbox_dir(p) -> bool:
    """True ONLY for a directory strictly UNDER the sandbox root — never the root itself, never outside
    it. The path is fully resolved first, so '.../nervice-cc-sandbox/../nervice' resolves to the live
    repo and is REFUSED."""
    try:
        rp = pathlib.Path(p).resolve()
    except Exception:
        return False
    if not rp.is_dir() or rp == _ROOT:
        return False
    try:
        rp.relative_to(_ROOT)                      # must be a descendant of the root
    except ValueError:
        return False
    return True


def _dir_size(p: pathlib.Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except Exception:
                pass
    except Exception:
        pass
    return total


def list_sandboxes() -> list:
    """Every immediate child DIR of the sandbox root, newest first: {name, path, age_days, size_mb}."""
    out = []
    if not _ROOT.is_dir():
        return out
    now = datetime.now(timezone.utc).timestamp()
    for child in _ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except Exception:
            mtime = now
        out.append({"name": child.name, "path": str(child),
                    "age_days": round((now - mtime) / 86400, 2),
                    "size_mb": round(_dir_size(child) / 1e6, 2)})
    out.sort(key=lambda d: d["age_days"])          # newest first
    return out


def plan_prune(older_than_days=None, keep_last_n=None) -> dict:
    """DRY-RUN: which sandboxes WOULD be removed under the policy. A dir is selected for removal iff it
    is NOT among the newest keep_last_n AND (no age policy OR it is at least older_than_days old). With
    NEITHER policy, nothing is selected — an explicit policy is required. Returns {root, all_count,
    would_remove, would_keep, policy, dry_run}."""
    items = list_sandboxes()                       # newest first
    if older_than_days is None and keep_last_n is None:
        return {"root": str(_ROOT), "all_count": len(items), "would_remove": [], "would_keep": items,
                "policy": {}, "dry_run": True,
                "note": "no policy given — pass older_than_days and/or keep_last_n to select dirs"}
    keep = set()
    if keep_last_n is not None:
        try:
            k = max(0, int(keep_last_n))
        except (TypeError, ValueError):
            k = 0
        keep |= {d["path"] for d in items[:k]}     # newest N are kept
    thr = None
    if older_than_days is not None:
        try:
            thr = float(older_than_days)
        except (TypeError, ValueError):
            thr = None
    remove = []
    for d in items:
        if d["path"] in keep:
            continue
        if thr is not None and d["age_days"] < thr:
            continue                               # too new for the age policy -> keep
        remove.append(d)
    rm_paths = {d["path"] for d in remove}
    return {"root": str(_ROOT), "all_count": len(items),
            "would_remove": remove,
            "would_keep": [d for d in items if d["path"] not in rm_paths],
            "policy": {"older_than_days": older_than_days, "keep_last_n": keep_last_n}, "dry_run": True}


def prune(targets, confirm: bool = False) -> dict:
    """Remove sandbox dirs. DRY-RUN unless confirm is True. HARD GUARD: only paths that pass
    _is_sandbox_dir (strictly under the sandbox root) are ever removed; anything else (e.g. ~/nervice)
    is REFUSED and reported, never touched. Returns {confirmed, removed, refused, errors, root}."""
    removed, refused, errors = [], [], []
    for t in (targets or []):
        if not _is_sandbox_dir(t):
            refused.append({"path": str(t), "reason": "REFUSED — outside the sandbox root (or not a dir)"})
            continue
        if not confirm:
            continue                               # dry-run: eligible, but nothing removed
        rp = pathlib.Path(t).resolve()
        try:
            shutil.rmtree(rp)
            removed.append(str(rp))
        except Exception as e:
            errors.append({"path": str(rp), "error": type(e).__name__})
    return {"confirmed": bool(confirm), "removed": removed, "refused": refused, "errors": errors,
            "root": str(_ROOT)}
