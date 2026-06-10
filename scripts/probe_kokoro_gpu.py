import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
import time, wave
import numpy as np
import onnxruntime as ort
import app.voice as V  # registers CUDA DLLs in _load_whisper; also gives model paths

app_voice = V
V._register_cuda_dlls()  # ensure cublas/cudnn/cudart dirs are on the DLL path for onnxruntime

from kokoro_onnx import Kokoro

MODEL = str(V._KOKORO_MODEL)
VOICES = str(V._KOKORO_VOICES)
OUT = pathlib.Path(__file__).resolve().parent.parent / "models" / "kokoro_samples"
OUT.mkdir(parents=True, exist_ok=True)

SENT = "Hey Nate, this is Kokoro running on the GPU now. Tell me if this feels snappier."


def build(providers):
    sess = ort.InferenceSession(MODEL, providers=providers)
    return Kokoro.from_session(sess, VOICES), sess.get_providers()


def write_wav(path, samples, sr):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def measure(label, k):
    k.create("warm up", voice="am_michael", speed=1.0, lang="en-us")  # warm
    times = []
    for _ in range(3):
        t = time.time()
        samples, sr = k.create(SENT, voice="am_michael", speed=1.0, lang="en-us")
        times.append(time.time() - t)
    print(f"[{label}] per-sentence warm latency: {[round(x,2) for x in times]}s  (bar <1.5s)  sr={sr}")
    return min(times), sr


print("=== GPU (CUDA EP) ===")
k_gpu, provs = build(["CUDAExecutionProvider", "CPUExecutionProvider"])
print("session providers actually used:", provs)
gpu_best, sr = measure("kokoro-GPU", k_gpu)

print("\n=== CPU (baseline) ===")
k_cpu, _ = build(["CPUExecutionProvider"])
cpu_best, _ = measure("kokoro-CPU", k_cpu)

print(f"\nSPEEDUP: CPU {cpu_best:.2f}s -> GPU {gpu_best:.2f}s ({cpu_best/gpu_best:.1f}x)")

print("\n=== writing voice audition samples (GPU) ===")
for v in ("am_adam", "am_onyx", "bm_george"):
    samples, sr = k_gpu.create(SENT, voice=v, speed=1.0, lang="en-us")
    write_wav(OUT / f"{v}.wav", samples, sr)
    print(f"  wrote {OUT / (v + '.wav')}")
print("done")
