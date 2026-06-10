import sys, pathlib, time, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# TTS test — audible. Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/test_tts.py
import app.voice as v

PARA = ("Hey Nate, the pipelined voice should feel continuous now. "
        "Each sentence is synthesized while the previous one is still playing. "
        "That means there's no pause between sentences, even on longer replies. "
        "Tell me if you hear any gap at all.")

sents = [s.strip() for s in v._SENT_SPLIT.split(v._clean_for_speech(PARA))
         if s.strip() and re.search(r"[A-Za-z0-9]", s)]

# synth-ahead margin: a sentence stays gapless if its synth time < the audio time the PREVIOUS
# sentence buys us. Report synth time vs this sentence's own audio duration as the headroom proxy.
v._synth(sents[0])  # warm
print(f"--- synth-ahead margin ({v.TTS_ENGINE}) ---")
for i, s in enumerate(sents):
    t = time.time(); audio, sr = v._synth(s); st = time.time() - t
    dur = len(audio) / sr
    print(f"  s{i+1}: synth {st:.2f}s | audio {dur:.2f}s | margin {dur - st:+.2f}s "
          f"({'ahead' if st < dur else 'BEHIND'})")

print(f"\nTTS rate: {v.TTS_RATE}Hz. Speaking the 4-sentence paragraph (pipelined)...")
t = time.time()
v.speak(PARA)
print(f"Done in {time.time() - t:.1f}s. PASS if you heard NO gap between sentences.")
