"""Nate's favorite artists — STORED DATA set by talking to Nervice, never hardcoded.

Persisted at data/music_favorites.json (gitignored, single-user). Deterministic conversational
management (no LLM, works capped): set / add / remove / list. The play capability
(computer.play_youtube) consults this for bare "play some music" requests — a random favorite,
or an honest ask when the list is empty. Leaf module: stdlib only."""
import re
import json
import random
import pathlib

_STORE = pathlib.Path(__file__).resolve().parent.parent / "data" / "music_favorites.json"
MAX_ARTISTS = 25


def load_artists() -> list[str]:
    try:
        if _STORE.exists():
            d = json.loads(_STORE.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return [str(a) for a in d if str(a).strip()][:MAX_ARTISTS]
    except Exception:
        pass
    return []


def _save(artists: list[str]) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(artists[:MAX_ARTISTS], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def random_artist() -> str | None:
    artists = load_artists()
    return random.choice(artists) if artists else None


def _parse_names(blob: str) -> list[str]:
    """'X, Y and Z' -> ['X','Y','Z']. Conservative cleanup, dedup, length caps."""
    blob = re.sub(r"[.!?]+\s*$", "", (blob or "").strip())
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*|\s*;\s*", blob, flags=re.I)
    out, seen = [], set()
    for p in parts:
        p = p.strip().strip("\"'").strip()
        if p and 1 < len(p) <= 40 and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out[:MAX_ARTISTS]


def apply(op: str, artists: list[str] | None = None) -> str:
    """Execute a structured favorites operation from the ROUTER ({op, artists}). The router (LLM)
    decides intent; this only validates and writes."""
    names = []
    for a in (artists or []):
        a = str(a).strip().strip("\"'").strip()
        if a and 1 < len(a) <= 40:
            names.append(a)
    op = (op or "").strip().lower()
    cur = load_artists()
    if op == "set":
        if not names:
            return "Which artists? Try \"my favorite artists are X, Y, and Z.\""
        _save(names[:MAX_ARTISTS])
        return f"Saved {len(names)} artist{'s' if len(names) != 1 else ''}: {', '.join(names)}."
    if op == "add":
        added = [n for n in names if n.lower() not in {a.lower() for a in cur}]
        if not added:
            return f"Already on the list. You've got: {', '.join(cur) if cur else '(nothing yet)'}."
        cur.extend(added)
        _save(cur)
        return f"Added {', '.join(added)}. That's {len(cur)} now."
    if op == "remove":
        low = {n.lower() for n in names}
        keep = [a for a in cur if a.lower() not in low]
        if len(keep) == len(cur):
            return f"That one isn't on your list. Current: {', '.join(cur) if cur else '(empty)'}."
        _save(keep)
        return f"Removed. {len(keep)} left" + (f": {', '.join(keep)}." if keep else " — the list is empty now.")
    if op == "clear":
        _save([])
        return "Cleared your favorite-artists list."
    # default: list
    if not cur:
        return "I don't have your favorite artists yet — tell me \"my favorite artists are X, Y, and Z.\""
    return f"Your artists: {', '.join(cur)}."


# ---- deterministic conversational management (LEGACY text parser — no longer a pre-router
# doorman; kept for the keyword-net path and tests) ----
_SET_RE = re.compile(r"^\s*(?:my\s+favou?rite\s+(?:artists?|bands?|musicians?)\s+(?:are|is)|"
                     r"set\s+my\s+(?:favou?rite\s+)?(?:artists?|bands?|music)\s+to)\s+(.+)$", re.I)
_ADD_RE = re.compile(r"^\s*add\s+(.+?)\s+to\s+my\s+(?:favou?rite\s+)?(?:artists?|bands?|music(?:\s+list)?)\s*[.!?]?\s*$", re.I)
_REMOVE_RE = re.compile(r"^\s*(?:remove|drop|delete|take)\s+(.+?)\s+(?:from|off)\s+my\s+(?:favou?rite\s+)?(?:artists?|bands?|music(?:\s+list)?)\s*[.!?]?\s*$", re.I)
_LIST_RE = re.compile(r"^\s*(?:who|what)\s+are\s+my\s+(?:favou?rite\s+)?(?:artists?|bands?|musicians?)\b"
                      r"|^\s*list\s+my\s+(?:favou?rite\s+)?(?:artists?|bands?)\b", re.I)


def handle(text: str) -> str | None:
    """Returns a reply if this turn manages the favorites list, else None (route normally).
    Deterministic — works even with every model rung down."""
    t = (text or "").strip()
    m = _SET_RE.match(t)
    if m:
        names = _parse_names(m.group(1))
        if not names:
            return "I didn't catch any artist names there — try \"my favorite artists are X, Y, and Z.\""
        _save(names)
        return f"Saved {len(names)} artist{'s' if len(names) != 1 else ''}: {', '.join(names)}."
    m = _ADD_RE.match(t)
    if m:
        names = _parse_names(m.group(1))
        artists = load_artists()
        added = [n for n in names if n.lower() not in {a.lower() for a in artists}]
        artists.extend(added)
        _save(artists)
        if not added:
            return f"Already on the list. You've got: {', '.join(artists)}."
        return f"Added {', '.join(added)}. That's {len(artists)} now."
    m = _REMOVE_RE.match(t)
    if m:
        names = {n.lower() for n in _parse_names(m.group(1))}
        artists = load_artists()
        keep = [a for a in artists if a.lower() not in names]
        if len(keep) == len(artists):
            return f"That one isn't on your list. Current: {', '.join(artists) if artists else '(empty)'}."
        _save(keep)
        return f"Removed. {len(keep)} left" + (f": {', '.join(keep)}." if keep else " — the list is empty now.")
    if _LIST_RE.match(t):
        artists = load_artists()
        if not artists:
            return "I don't have your favorite artists yet — tell me \"my favorite artists are X, Y, and Z.\""
        return f"Your artists: {', '.join(artists)}."
    return None