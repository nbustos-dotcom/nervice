"""Adversarial spoken-number normalization checks — runs the REAL _clean_for_speech (the spoken-only
transform; the on-screen HUD text is the raw input, untouched). No LLM, no server, no Claude.
Run: .venv/Scripts/python.exe scripts/test_spoken_numbers.py   (first import warms the voice models ~14s)"""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.voice as v

P = F = 0
def case(screen, must=(), must_not=()):
    """screen = the exact on-screen text; spoken = what TTS actually receives."""
    global P, F
    spoken = v._clean_for_speech(screen)
    ok = True
    for m in must:
        if m.lower() not in spoken.lower():
            ok = False; print(f"  FAIL  {screen!r} -> {spoken!r}\n        missing: {m!r}")
    for m in must_not:
        if m.lower() in spoken.lower():
            ok = False; print(f"  FAIL  {screen!r} -> {spoken!r}\n        must NOT contain: {m!r}")
    # no private-use placeholder ever leaks to TTS
    if any(0xE000 <= ord(c) <= 0xF8FF for c in spoken):
        ok = False; print(f"  FAIL  {screen!r} -> {spoken!r}  (leaked mask placeholder)")
    if ok:
        print(f"  PASS  screen={screen!r}\n        spoken={spoken!r}")
    P += ok; F += (not ok)

GIANT = ("thousand", "million", "billion", "trillion")   # a version/IP/hash must never read as these

print("=== CORE: abbreviations + large numbers + currency + percent + year ===")
case("$880M",        must=["eight hundred eighty million dollars"], must_not=["$", "880"])
case("1,200,000",    must=["one point two million"],                must_not=[",", "zero"])
case("5K users",     must=["five thousand users"],                  must_not=["5k"])
case("$1.2B",        must=["one point two billion dollars"],        must_not=["$", "1.2"])
case("50%",          must=["fifty percent"],                        must_not=["%", "50"])
case("2026",         must=["twenty twenty six"],                    must_not=["two thousand"])
case("$3.50",        must=["three dollars and fifty cents"],        must_not=["$", "3.50"])
case("100 points",   must=["one hundred points"],                   must_not=["100"])
case("Step 2 of 5",  must=["step two of five"],                     must_not=["2 of 5"])

print("\n=== HARD GUARD: versions / hashes / IPs / times / model names NOT mangled ===")
case("Claude Opus 4.8",  must=["four point eight"], must_not=GIANT + ("forty",))      # version reads sensibly
case("v4.6",             must=["v4.6"],             must_not=GIANT + ("forty",))      # masked -> literal
case("llama-3.3-70b",    must=["llama-3.3-70b"],    must_not=GIANT + ("seventy billion", "three point three"))
case("gpt-oss-120b",     must=["gpt-oss-120b"],     must_not=GIANT)                   # model id literal
case("9:36 PM",          must=["nine thirty six"],  must_not=GIANT + ("hundred",))    # sensible time
case("127.0.0.1",        must=["127.0.0.1"],        must_not=GIANT)                   # IP literal, not expanded
case("commit 9fc71d8 landed", must=["commit", "landed"], must_not=GIANT + ("9fc71d8",))  # hash dropped (screen-only)

print("\n=== news-style sentence: several numbers, all natural ===")
case("the deal was worth $880M, up 15% over 2025, affecting 1,200,000 users",
     must=["eight hundred eighty million dollars", "fifteen percent", "twenty twenty five", "one point two million users"],
     must_not=["$", "%", "880", "1,200,000"])

print("\n=== REGRESSION: Part-1 symbol normalization (dash / slash / & / %) still works ===")
case("well-known issue",   must=["well known issue"],  must_not=["well-known"])
case("R&D budget",         must=["R and D budget"],    must_not=["&"])
case("TCP/IP stack",       must=["TCP IP stack"],      must_not=["/"])
case("yes and/or no",      must=["and or"],            must_not=["/"])
case("a — b",              must=["a, b"])               # em-dash -> pause

# screen-stays-exact: _clean_for_speech is a pure transform of a COPY; the caller passes the raw text
# to the HUD and only the return value to TTS (streaming.py: text frame = raw, audio = cleaned).
print("\n=== SCREEN STAYS EXACT (spoken transform never mutates the on-screen string) ===")
_screen = "$880M"
_spoken = v._clean_for_speech(_screen)
ok = (_screen == "$880M" and _spoken != _screen and "eight hundred eighty million dollars" in _spoken)
print(f"  {'PASS' if ok else 'FAIL'}  screen still {_screen!r}; spoken separately {_spoken!r}")
P += ok; F += (not ok)

print(f"\n=== SPOKEN-NUMBERS {P} passed, {F} failed ===")
sys.exit(1 if F else 0)
