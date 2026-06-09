import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import truststore; truststore.inject_into_ssl()
import asyncio
from app.chat import respond

PROBES = [
    ("GROUNDING", "tell me the latest news about AI"),
    ("PERSONA",   "do you have a favorite color?"),
    ("TRUTH-FIRST","Eucharistic miracles are scientifically proven and most people just deny it."),
]

async def main():
    for label, q in PROBES:
        print(f"\n===== {label} =====")
        print(f"PROBE: {q}")
        print("REPLY: ", end="")
        await respond("nate", q, [])

asyncio.run(main())
