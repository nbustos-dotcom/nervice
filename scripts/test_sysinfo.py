"""System-awareness + self-knowledge + news tests. t1-t5 run the real respond() pipeline (real
Groq tool-calling); t6 is a static read-only audit. Read-only — none of these tools modify anything.
Throwaway."""
import sys, pathlib, asyncio, inspect, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import app.tools as tools
import app.sysinfo as sysinfo
from app.chat import respond
from app.llm import LADDER_EXHAUSTED_MSG

R = {}

# spy on web_search so t5 can confirm news really searched (grounded), not confabulated
_orig_search = tools.web_search
SEARCHED = {"hit": False, "q": ""}
async def _spy(query, max_results=6):
    SEARCHED["hit"] = True; SEARCHED["q"] = query
    return await _orig_search(query, max_results)
tools.web_search = _spy
tools.TOOL_FUNCS["web_search"] = _spy


def _capped(reply):
    return not reply or LADDER_EXHAUSTED_MSG in reply


async def main():
    real_cpu = sysinfo._cpu_name().lower()                      # ground truth for t1

    # t1: what CPU do I have -> real CPU model
    r1 = await respond("nate", "what CPU do I have?", [])
    R["t1 real CPU model"] = (not _capped(r1)) and (("ryzen" in r1.lower() or "amd" in r1.lower()
                              or real_cpu.split()[0] in r1.lower()))
    print(f"t1: {r1[:110]!r}")

    # t2: what's using the most memory -> a process + MB
    r2 = await respond("nate", "what process is using the most memory right now?", [])
    R["t2 top memory process"] = (not _capped(r2)) and bool(re.search(r"\d", r2)) \
        and bool(re.search(r"\b(mb|gb|gigabyte|megabyte|memory|ram)\b", r2.lower()))
    print(f"t2: {r2[:110]!r}")

    # t3: how many files in Downloads -> a count, no hang
    r3 = await respond("nate", "how many files are in my Downloads folder?", [])
    R["t3 file count"] = (not _capped(r3)) and bool(re.search(r"\d", r3)) and "file" in r3.lower()
    print(f"t3: {r3[:110]!r}")

    # t4: what model are you -> accurate (Groq llama-3.3 / gpt-oss / Claude), NOT confabulated
    r4 = await respond("nate", "what model are you using?", [])
    low4 = (r4 or "").lower()
    accurate = ("groq" in low4 or "llama-3.3" in low4 or "gpt-oss" in low4 or "claude" in low4)
    confab = "llama 3.2" in low4 or "gpt-4" in low4 or "llama-3.2" in low4
    R["t4 accurate self-knowledge"] = (not _capped(r4)) and accurate and not confab
    print(f"t4: {r4[:140]!r}")

    # t5: give me the news -> real search happened (grounded), not generic filler
    SEARCHED["hit"] = False
    r5 = await respond("nate", "give me the news", [])
    filler = "landscape is evolving" in (r5 or "").lower() or "stay tuned" in (r5 or "").lower()
    R["t5 news really searched"] = SEARCHED["hit"] and (not _capped(r5)) and not filler and len((r5 or "").split()) > 8
    print(f"t5: searched={SEARCHED['hit']} q={SEARCHED['q'][:40]!r} reply={r5[:110]!r}")

    # t6: READ-ONLY audit — sysinfo + the new tool fns contain NO destructive operations
    src = inspect.getsource(sysinfo)
    for fn in ("get_system_info", "get_top_processes", "count_files", "get_news"):
        src += inspect.getsource(getattr(tools, fn))
    BANNED = ["os.remove", "os.unlink", "os.rmdir", "rmtree", ".kill(", ".terminate(", ".suspend(",
              "os.system(", "shell=True", "Popen(", "os.rename", "os.replace", ".write(", "open("]
    hits = [b for b in BANNED if b in src]
    R["t6 read-only (no destructive ops)"] = (hits == [])
    print(f"t6: banned-ops found (want []): {hits}")

    for t in ("t1", "t2", "t3", "t4", "t5", "t6"):
        pass
    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    capped_note = any(_capped(x) for x in [r1, r2, r3, r4, r5])
    if capped_note:
        print("  NOTE: a turn hit the Groq cap / ladder message — rerun when Groq has daily budget.")
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
