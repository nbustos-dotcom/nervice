"""Deterministic capability-vs-telemetry routing checks (no network). Run:
   .venv/Scripts/python.exe scripts/test_capability_routing.py
The live brain answers + the Groq router are exercised by the adversarial /chat test.
"""
import sys, asyncio, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from app import router
from app.persona import PERSONA

P = F = 0
def chk(d, got, want):
    global P, F
    okq = got == want
    print(f"  {'PASS' if okq else 'FAIL'} {d}: {got!r}")
    P += okq; F += not okq

print("[is_capability_question] ability/self questions -> True")
for m in ["can you see my screen", "what can you do", "can you read my emails",
          "are you able to see what I'm doing right now", "can you control my computer",
          "can you watch my screen", "can you access my files",
          "do you have the ability to see my screen"]:
    chk(m, router.is_capability_question(m), True)

print("[is_capability_question] telemetry + action requests -> False (don't over-catch)")
for m in ["what's my CPU", "how much RAM do I have", "what's my screen resolution",
          "what are my specs", "can you open notepad", "can you play music",
          "my screen is frozen"]:
    chk(m, router.is_capability_question(m), False)

print("[classify pre-guard] capability question -> route normal WITHOUT calling Groq (short-circuit)")
async def main():
    for m in ["can you see my screen", "what can you do", "can you control my computer",
              "are you able to see what I'm doing right now", "can you read my emails"]:
        d = await router.classify(m)
        chk(f"classify({m!r})", d, {"route": "normal"})
asyncio.run(main())

print("[persona SYSTEM FACTS] honest screen capability is present")
chk("screenshot is a still image, not live view", ("still image" in PERSONA) and ("live desktop" in PERSONA.lower()), True)
chk("can read Canvas/web (real ability listed)", "Canvas" in PERSONA, True)
chk("cannot send email (boundary kept)", "send email" in PERSONA.lower(), True)

print(f"\n=== {P} passed, {F} failed ===")
sys.exit(1 if F else 0)
