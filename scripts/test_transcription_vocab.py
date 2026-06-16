"""Mechanism-level checks for the STT vocabulary bias + the conservative wake-phrase repair. No audio
(CC can't speak) — the real recognition gain is validated by Nate's voice listen (see the checklist in
the task report). Here we prove: the hotwords vocab is right, and the wake-fix corrects clear wake
variants while NEVER corrupting mid-sentence "service"/"claw"/"rock" or legit speech.
Run: .venv/Scripts/python.exe scripts/test_transcription_vocab.py   (first import warms models ~14s)"""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.voice as v

P = F = 0
def chk(d, c):
    global P, F
    print(f"  {'PASS' if c else 'FAIL'} {d}")
    P += bool(c); F += (not c)

print("[hotwords vocab] the real Nervice terms are present")
for term in ("Nervice", "Claude", "Groq", "Ollama", "Anthropic", "Tailscale", "Supabase", "Kokoro", "Whisper", "Canvas"):
    chk(f"vocab includes {term}", term in v._STT_HOTWORDS)

print("\n[wake-fix] clear misheard wake-addresses -> 'Hey Nervice'")
for src, want in [
    ("say service",                         "Hey Nervice"),
    ("say service, what's the weather",     "Hey Nervice, what's the weather"),
    ("hey service",                         "Hey Nervice"),
    ("hey service.",                        "Hey Nervice."),
    ("okay nervice",                        "Hey Nervice"),
    ("hey nervice, ask Claude",             "Hey Nervice, ask Claude"),
]:
    got = v._fix_wake_phrase(src)
    chk(f"{src!r} -> {got!r}", got == want)

print("\n[wake-fix ADVERSARIAL] normal speech is NEVER corrupted")
for src in [
    "the customer service was great",       # mid-sentence 'service', no leading greeting
    "I saw a claw machine at the arcade",   # 'claw' is a real word
    "the rock concert was loud",            # 'rock' is a real word
    "hey, the service was slow",            # comma breaks greeting+name adjacency
    "say service three times fast",         # not clause-final -> not a wake address
    "okay service the request now",         # not clause-final
    "customer service is closed",           # no leading greeting
    "I'm nervous about the exam",           # 'nervous' deliberately NOT a variant
    "hey there friend",                     # greeting but no name-variant
]:
    got = v._fix_wake_phrase(src)
    chk(f"unchanged: {src!r}", got == src)

print(f"\n=== TRANSCRIPTION-VOCAB {P} passed, {F} failed ===")
sys.exit(1 if F else 0)
