import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import truststore; truststore.inject_into_ssl()
import asyncio

from app import tools
from app.chat import respond

CAPTURE = pathlib.Path(__file__).parent / "probe_capture.txt"

captured = []
_orig_search = tools.web_search


async def logged_search(query: str, max_results: int = 6) -> str:
    out = await _orig_search(query, max_results)   # web_search is async now
    captured.append((query, out))
    return out


tools.TOOL_FUNCS["web_search"] = logged_search

PROBES = [
    ("GROUNDING", "tell me the latest news about AI"),
    ("PERSONA",   "do you have a favorite color?"),
]


async def main():
    replies = {}
    for label, q in PROBES:
        print(f"\n===== {label} =====")
        print(f"PROBE: {q}")
        print("REPLY: ", end="")
        replies[label] = await respond("nate", q, [])
    with open(CAPTURE, "w", encoding="utf-8") as f:
        for q, out in captured:
            f.write(f"========== QUERY: {q} ==========\n{out}\n\n")
        f.write("\n########## FINAL GROUNDING REPLY ##########\n")
        f.write(replies["GROUNDING"] or "")
        f.write("\n\n########## PERSONA REPLY ##########\n")
        f.write(replies["PERSONA"] or "")
    print(f"\n[capture written to {CAPTURE}]")


asyncio.run(main())
