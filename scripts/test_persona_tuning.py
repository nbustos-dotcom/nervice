"""Persona-tuning tests: terse-by-default, assume-don't-ask, news behavior, grounding intact.
Real Groq + real web search. Read-only (respond() doesn't persist). Throwaway."""
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import app.tools as tools
from app.chat import respond

R = {}

# spy on web_search so we can prove the news turn actually searched (grounding) vs fabricated
_orig_search = tools.TOOL_FUNCS["web_search"]
SEARCH = {"called": False, "query": ""}
async def spy_search(query, max_results=6):
    SEARCH["called"] = True; SEARCH["query"] = query
    return await _orig_search(query, max_results)
tools.TOOL_FUNCS["web_search"] = spy_search

ASK_PATTERNS = ["what news", "which news", "what kind of news", "what type of news",
                "what would you like", "what are you interested", "what topic", "any particular",
                "what specifically", "could you clarify", "can you clarify", "be more specific"]


def asks_clarifying(reply):
    r = (reply or "").lower()
    return any(p in r for p in ASK_PATTERNS) or ("?" in r and len(r.split()) < 25 and "news" not in r)


async def main():
    # ---- t1: "tell me the news" -> searches + delivers headlines, does NOT ask "what news" ----
    SEARCH["called"] = False
    r1 = await respond("nate", "tell me the news", [], voice_mode=False)
    no_ask = not any(p in (r1 or "").lower() for p in ["what news", "which news", "what kind of news",
                     "what type of news", "what topic", "any particular news", "what would you like"])
    substantive = bool(r1) and len(r1.split()) > 12
    R["t1 news: delivers, no 'what news'"] = SEARCH["called"] and no_ask and substantive
    print(f"t1: searched={SEARCH['called']} q={SEARCH['query'][:50]!r}")
    print(f"t1: reply={r1[:160]!r}")
    print("t1", "PASS" if R["t1 news: delivers, no 'what news'"] else "FAIL")

    # ---- t5: grounding intact — the news came from a REAL search, not fabricated ----
    R["t5 grounding: real search fired"] = SEARCH["called"]
    print("t5", "PASS" if R["t5 grounding: real search fired"] else "FAIL")

    # ---- t2: a simple question -> short, leads with the answer, no padding ----
    r2 = await respond("nate", "what's the capital of France?", [], voice_mode=True)
    wc = len((r2 or "").split())
    R["t2 terse: short + leads with answer"] = "paris" in (r2 or "").lower() and wc <= 20
    print(f"t2: reply={r2[:80]!r}  words={wc}")
    print("t2", "PASS" if R["t2 terse: short + leads with answer"] else "FAIL")

    # ---- t3: "explain more" after a terse answer -> elaborates (longer) ----
    window3 = [
        {"role": "user", "content": "what is a vector database, in one line?"},
        {"role": "assistant", "content": "A database that stores data as vectors and retrieves by similarity."},
    ]
    r3 = await respond("nate", "explain that more — go deeper", window3, voice_mode=False)
    R["t3 elaborate on request"] = bool(r3) and len(r3.split()) >= 40
    print(f"t3: words={len((r3 or '').split())}  reply={r3[:90]!r}")
    print("t3", "PASS" if R["t3 elaborate on request"] else "FAIL")

    # ---- t4: a GENUINELY ambiguous request (no referent, no default) -> still asks ----
    r4 = await respond("nate", "hey, can you take care of that thing for me?", [], voice_mode=False)
    R["t4 ambiguous still asks"] = "?" in (r4 or "")
    print(f"t4: reply={r4[:120]!r}")
    print("t4", "PASS" if R["t4 ambiguous still asks"] else "FAIL")

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
