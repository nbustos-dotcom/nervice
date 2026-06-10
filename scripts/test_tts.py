import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# TTS test — audible. Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/test_tts.py
from app.voice import speak, TTS_RATE

print(f"TTS rate: {TTS_RATE}Hz. Speaking now (listen for two sentences, streamed)...")
speak("Hey Nate, voice is working. This is sentence two, streamed.")
print("Done. PASS if you heard both sentences, the first starting almost immediately.")
