import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# Standalone STT test — REQUIRES Nate at the mic.
# Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/test_stt.py
import sounddevice as sd
import numpy as np
from app.voice import transcribe, SAMPLE_RATE, WHISPER_PATH

SECONDS = 5


def main():
    print(f"STT backend: {WHISPER_PATH}")
    di = sd.query_devices(kind="input")
    print(f"Input device: {di['name']}")
    input(f"Press Enter, then say anything for {SECONDS} seconds...")
    print("🎤 recording...")
    audio = sd.rec(int(SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("...transcribing")
    text = transcribe(audio[:, 0])
    print(f"\nTRANSCRIPT: {text!r}")
    print("PASS if that matches what you said.")


main()
