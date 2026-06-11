"""Tests for tonight's three fixes: rung observability, junk-transcript gate, widened memory
extraction. t2 is pure/deterministic; t1 + t3 use real Groq. t3 runs under a THROWAWAY user_id so
it can never touch Nate's real memories. Cleans up. Throwaway."""
import sys, pathlib, asyncio, uuid, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

from app.chat import respond
from app.voice import is_junk_transcript
from app.memory import remember
from app.retrieval import retrieve

R = {}
TURNS_LOG = pathlib.Path("logs/turns.log")


async def main():
    # ---- t1: a normal turn writes ONE rung line with route + rung=groq + timing + path ----
    before = TURNS_LOG.read_text(encoding="utf-8").splitlines() if TURNS_LOG.exists() else []
    reply = await respond("nate", "in three words, say hello", [])
    after = TURNS_LOG.read_text(encoding="utf-8").splitlines() if TURNS_LOG.exists() else []
    new = after[len(before):]
    line = new[-1] if new else ""
    R["t1 rung log line"] = bool(reply) and bool(new) and "route=" in line and "rung=" in line \
        and "path=rest" in line and "s\t" in line
    print(f"t1: reply={reply[:40]!r}")
    print(f"t1: log line -> {line!r}")
    print("t1", "PASS" if R["t1 rung log line"] else "FAIL")

    # ---- t2: junk gate — "You"/fillers blocked; real short commands pass ----
    JUNK = ["You", "you", "uh", "um", "the", "hmm", "I", "a", "", "  ", ".", "mm", "Mhm",
            "Thank you.", "thank you", "thanks for watching", "please subscribe", "subscribe",
            "uh uh", "um uh", "you know", "i mean", "the the", "you you", "so um"]  # whisper hallucinations + multi-word filler
    REAL = ["yes", "no", "stop", "open notepad", "what's the weather", "open youtube",
            "yeah", "go", "ok", "turn on the lights", "send it",
            "i dont know", "open the door", "turn it off", "what time is it", "five", "call mom"]
    junk_ok = all(is_junk_transcript(t) for t in JUNK)
    real_ok = all(not is_junk_transcript(t) for t in REAL)
    R["t2 junk gate"] = junk_ok and real_ok
    bad_junk = [t for t in JUNK if not is_junk_transcript(t)]
    bad_real = [t for t in REAL if is_junk_transcript(t)]
    print(f"t2: junk blocked={junk_ok} (leaked: {bad_junk})  real passed={real_ok} (blocked: {bad_real})")
    print("t2", "PASS" if R["t2 junk gate"] else "FAIL")

    # ---- t3: a business-idea conversation -> extracted + retrievable in a NEW query ----
    uid = "biztest-" + uuid.uuid4().hex[:8]
    user_text = ("So I've been seriously thinking about a business idea — building automated "
                 "micro-sites for local workers and small businesses around my area, like a "
                 "fast, cheap web-presence service I'd actually build and sell.")
    asst_text = ("That's a solid idea — an automated micro-site service for local businesses could "
                 "fill a real gap. Want help scoping the MVP?")
    applied = await remember(uid, user_text, asst_text, source_conv_id="biztest-conv")
    print(f"t3: extractor applied {len(applied or [])} op(s): {[a.get('op') for a in (applied or [])]}")
    # retrieve in a FRESH query (new conversation) under the same user
    hits = await retrieve(uid, "what business or project is Nate working on?")
    contents = [m.content.lower() for m in (hits.get("core", []) + hits.get("topic", []))]
    matched = [c for c in contents if "micro-site" in c or "micro site" in c
               or ("business" in c and "local" in c) or "web-presence" in c or "web presence" in c]
    R["t3 idea extracted + retrievable"] = bool(matched)
    print(f"t3: retrieved {len(contents)} memory(ies); idea match -> {matched[:1]}")
    print("t3", "PASS" if R["t3 idea extracted + retrievable"] else "FAIL")

    # ---- cleanup: wipe the throwaway test user's memories entirely ----
    from sqlalchemy import delete
    from app.db import AsyncSessionLocal
    from app.models import Memory
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Memory).where(Memory.user_id == uid))
        await s.commit()

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
