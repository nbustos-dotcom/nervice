"""Manual voice-gate test for Nervice (Batch B). Run from the repo root with the venv:

    .venv\\Scripts\\python.exe scripts\\manual_voice_test.py

It records short clips from your default mic and runs them through the SERVER's exact receive-side
gate (app.voice.transcribe_clip = Silero VAD -> faster-whisper -> junk gate), then tells you whether
that clip would have started a real turn or been SILENTLY DROPPED.

Three things to try (the script prompts for each):
  1. SILENCE / ROOM NOISE: sit near a fan or AC and say NOTHING.
     Expect: "no speech -> SILENT DROP". This is the failure Batch B fixes — room noise must
     NEVER start a turn (the old amplitude/webrtcvad gate let it through; the "dots" turn).
  2. REAL SPEECH: say a normal sentence ("what's the weather today").
     Expect: "WOULD START A TURN", with your words transcribed.
  3. JUNK: cough, mumble, or tap the desk.
     Expect: "SILENTLY DROPPED" (no speech, or a junk/low-confidence transcript).

HONEST EXPECTATIONS / LIMITS:
  - Silero is a real neural VAD; it rejects steady fan/AC/white noise far better than the old
    amplitude gate (verified: white noise + tone score ~0.0 speech probability). It is NOT perfect:
    a sharp transient right into the mic (a slammed door, a hard cough) can register as speech for a
    moment — that is why the junk gate (empty / punctuation-only / filler / low-confidence whisper)
    is the second net behind it.
  - A short genuine word ("yes", "stop") is deliberately allowed through (whitelist) even at low
    confidence, so a real one-word command is never silently eaten.
  - This tests the RECEIVE side (what the phone/HUD upload). The browser's own VAD (in
    index_v3.html) still decides WHEN to send; this server gate is what now rejects its false
    positives. If you want the browser VAD replaced too, that is a separate change to the HUD.
"""
import os
import pathlib
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import sounddevice as sd
except Exception as e:  # pragma: no cover
    sys.exit(f"sounddevice unavailable ({e!r}); run this on the machine with the mic.")

import app.voice as v

SR = 16000


def record(seconds: float):
    print(f"   recording {seconds:.0f}s ...", flush=True)
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16")
    sd.wait()
    return audio[:, 0]


def run_clip(pcm) -> dict:
    t = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    t.close()
    try:
        with wave.open(t.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        return v.transcribe_clip(t.name)
    finally:
        os.unlink(t.name)


SCENARIOS = [
    ("SILENCE / ROOM NOISE (say nothing; sit near the fan)", 4, "no speech -> SILENT DROP"),
    ("REAL SPEECH (say: what's the weather today)",          4, "WOULD START A TURN"),
    ("JUNK (cough / mumble / tap the desk)",                 4, "SILENTLY DROPPED"),
]


def main():
    print(__doc__)
    print(f"VAD engine: {v.VAD_ENGINE}   (expected: silero)\n")
    for label, secs, expect in SCENARIOS:
        try:
            input(f">>> {label}\n    press ENTER to record {secs}s (Ctrl-C to quit) ... ")
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        clip = run_clip(record(secs))
        if not clip["speech"]:
            verdict = "SILENTLY DROPPED  (Silero: no speech)"
        elif clip["junk"]:
            verdict = (f"SILENTLY DROPPED  (junk: text={clip['text']!r} "
                       f"nsp={clip['no_speech_prob']:.2f} alp={clip['avg_logprob']:.2f})")
        else:
            verdict = f"WOULD START A TURN  ->  transcript: {clip['text']!r}"
        print(f"    expected: {expect}")
        print(f"    result:   {verdict}\n")
    print("If silence/fan ever reads 'WOULD START A TURN', tell Claude — SILERO_SPEECH_PROB /")
    print("SILERO_MIN_SPEECH_MS in app/voice.py can be tightened.")


if __name__ == "__main__":
    main()
