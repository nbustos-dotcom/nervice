import truststore; truststore.inject_into_ssl()  # Norton TLS for HF download
import os, sys, time, wave
import numpy as np
from neutts import NeuTTS

ROOT = r"C:\Users\nateb\nervice"
REF = os.path.join(ROOT, "models", "voice_ref", "synthetic_ref.wav")
REF_TEXT = open(os.path.join(ROOT, "models", "voice_ref", "synthetic_ref.txt")).read().strip()
OUT = os.path.join(ROOT, "models", "neutts_samples")
os.makedirs(OUT, exist_ok=True)

# GGUF variant (neutts-air-q4-gguf) is the "real-time CPU" path but needs llama-cpp-python;
# fall back to the torch backbone (neutts-air, 0.5B) which is slower on CPU. Pick by arg.
backbone = sys.argv[1] if len(sys.argv) > 1 else "neuphonic/neutts-air"
print(f"backbone: {backbone}")

t = time.time()
tts = NeuTTS(backbone_repo=backbone, backbone_device="cpu",
             codec_repo="neuphonic/neucodec", codec_device="cpu")
print(f"load: {time.time()-t:.1f}s")

t = time.time()
ref_codes = tts.encode_reference(REF)
print(f"encode_reference (one-time per voice): {time.time()-t:.2f}s")

SR = 24000
sents = ["Hey Nate, this is NeuTTS Air cloning a synthetic reference voice.",
         "It runs the point five billion model on CPU.",
         "Tell me whether this sounds more natural than Kokoro."]

tts.infer(sents[0], ref_codes, REF_TEXT)  # warm
print("--- warm first-audio latency per sentence (CPU) ---")
for i, s in enumerate(sents):
    t = time.time()
    wav = tts.infer(s, ref_codes, REF_TEXT)
    audio_s = len(wav) / SR
    print(f"  {len(s):3d} chars -> {audio_s:.1f}s audio in {time.time()-t:.2f}s  (bar <1.5s)")
    pcm = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
    with wave.open(os.path.join(OUT, f"neutts_{i}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
print(f"wrote 3 samples + cloned voice to {OUT}")
