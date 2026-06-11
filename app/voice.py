import app.net  # noqa  (truststore: Norton TLS — needed for first-run model downloads)

import os
import re
import sys
import time
import queue
import threading
import pathlib

import numpy as np
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel


def _register_cuda_dlls() -> None:
    """Make the pip-installed CUDA runtime DLLs loadable by ctranslate2 (whisper) AND onnxruntime's
    CUDA EP (Kokoro) — Windows doesn't search site-packages for DLLs otherwise. ctranslate2 needs
    cublas+cudnn+cudart; onnxruntime additionally needs cufft+curand (torch used to bundle these,
    so they're separate pip packages now). Register via both add_dll_directory and PATH."""
    import importlib.util
    dirs = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime",
                "nvidia.cufft", "nvidia.curand", "nvidia.nvjitlink"):
        try:
            spec = importlib.util.find_spec(pkg)
            if spec and spec.submodule_search_locations:
                bin_dir = pathlib.Path(list(spec.submodule_search_locations)[0]) / "bin"
                if bin_dir.is_dir():
                    os.add_dll_directory(str(bin_dir))
                    dirs.append(str(bin_dir))
        except Exception:
            pass  # missing package -> CUDA load fails -> CPU fallback engages
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")

# All audio is processed locally — faster-whisper (STT) and Piper (TTS) run on this machine.
# Nothing audio-related leaves the box; only the final TEXT goes to the existing pipeline,
# exactly like typed input. No audio files are written; PCM lives in memory only.

SAMPLE_RATE = 16000          # whisper + webrtcvad rate
VAD_FRAME_MS = 30            # webrtcvad accepts 10/20/30ms frames
_FRAME_LEN = SAMPLE_RATE * VAD_FRAME_MS // 1000   # 480 samples / 30ms
_VAD = webrtcvad.Vad(2)      # aggressiveness 2 (0 lax .. 3 strict)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_KOKORO_MODEL = _ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _ROOT / "models" / "kokoro" / "voices-v1.0.bin"
_PIPER_VOICE = _ROOT / "models" / "piper" / "en_US-ryan-high.onnx"

VOICE = "bm_george"   # Kokoro voice — swap here (e.g. am_adam, am_onyx, bm_george, bm_lewis)

# Optional voice blend: average two preset style vectors (weights need not sum to 1, but ~1 is
# natural). Set BLEND to enable; it overrides VOICE. Leave None to use VOICE as-is.
#   BLEND = ("am_onyx", 0.7, "am_adam", 0.3)   # 70% onyx + 30% adam
BLEND = None

try:
    import msvcrt  # Windows: lets a keypress drop a turn to typed input
except ImportError:
    msvcrt = None


def _load_whisper():
    """Prefer CUDA (DLLs registered above); fall back to CPU int8 if the CUDA runtime is missing.
    Warm the model once so the first real transcription isn't slow."""
    _register_cuda_dlls()
    for dev, ct in (("cuda", "float16"), ("cpu", "int8")):
        try:
            m = WhisperModel("base.en", device=dev, compute_type=ct)
            list(m.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])
            return m, f"{dev}/{ct}"
        except Exception as e:
            print(f"[voice] whisper {dev}/{ct} unavailable: {repr(e)[:80]}", file=sys.stderr)
    raise RuntimeError("no usable faster-whisper backend (cuda and cpu both failed)")


def _load_tts():
    """Kokoro (natural, 24kHz) preferred; Piper en_US-ryan-high as fallback if kokoro-onnx is
    broken on this Python. Returns (synth_fn, engine_name, sample_rate) where synth_fn(text)
    -> int16 numpy audio."""
    try:
        from kokoro_onnx import Kokoro
        # Run Kokoro's ONNX on the GPU (CUDA EP) when onnxruntime-gpu + the CUDA DLLs are present
        # — ~3x faster than CPU (0.84s vs 2.5s/sentence). Falls back to default (CPU) session.
        _register_cuda_dlls()
        ep = "cpu"
        try:
            import onnxruntime as ort
            _so = ort.SessionOptions()
            _so.log_severity_level = 3  # quiet the CUDA Memcpy/ScatterND perf warnings
            sess = ort.InferenceSession(str(_KOKORO_MODEL), sess_options=_so,
                                        providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            if "CUDAExecutionProvider" in sess.get_providers():
                ep = "cuda"
            k = Kokoro.from_session(sess, str(_KOKORO_VOICES))
        except Exception as e:
            print(f"[voice] kokoro CUDA session failed ({repr(e)[:60]}) — CPU", file=sys.stderr)
            k = Kokoro(str(_KOKORO_MODEL), str(_KOKORO_VOICES))

        # resolve the voice once: a blended style vector if BLEND is set, else the preset name
        if BLEND:
            an, aw, bn, bw = BLEND
            voice_arg = (aw * k.get_voice_style(an) + bw * k.get_voice_style(bn)).astype(np.float32)
            label = f"blend({an}{aw:g}+{bn}{bw:g})"
            base = an
        else:
            voice_arg = VOICE
            label = VOICE
            base = VOICE
        # British presets (bm_/bf_) get British phonemization — en-us G2P on a British voice
        # flattens the accent (and would not match the audition samples in models/kokoro_samples)
        lang = "en-gb" if base.startswith(("bm_", "bf_")) else "en-us"

        def synth(text: str):
            samples, sr = k.create(text, voice=voice_arg, speed=1.0, lang=lang)
            return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16), sr

        _, rate = synth("hi")  # warm + get true rate
        return synth, f"kokoro/{label} ({ep})", rate
    except Exception as e:
        print(f"[voice] kokoro unavailable ({repr(e)[:80]}) — falling back to Piper", file=sys.stderr)
        from piper import PiperVoice
        pv = PiperVoice.load(str(_PIPER_VOICE))
        rate = pv.config.sample_rate

        def synth(text: str):
            chunks = list(pv.synthesize(text))
            if not chunks:
                return np.zeros(0, dtype=np.int16), rate
            return np.concatenate([c.audio_int16_array for c in chunks]), rate

        return synth, "piper/en_US-ryan-high", rate


print("[voice] loading local models (STT + TTS)...", file=sys.stderr)
_t0 = time.time()
_whisper, WHISPER_PATH = _load_whisper()
_synth, TTS_ENGINE, TTS_RATE = _load_tts()
print(f"[voice] models loaded in {time.time() - _t0:.1f}s  "
      f"(stt=faster-whisper base.en {WHISPER_PATH}, tts={TTS_ENGINE} @ {TTS_RATE}Hz)",
      file=sys.stderr)


def record_until_silence(max_seconds: int = 60, trailing_silence: float = 0.7,
                         allow_type_abort: bool = True):
    """Capture 16kHz mono from the default mic, gated by webrtcvad: start collecting on the first
    voiced frames (with a short pre-roll so onsets aren't clipped), stop after ~trailing_silence of
    quiet, hard cap at max_seconds. Returns int16 PCM in memory. If allow_type_abort and the user
    presses a key, returns None to signal "type this turn instead". PCM is never written to disk."""
    silence_limit = int(trailing_silence * 1000 / VAD_FRAME_MS)
    max_frames = int(max_seconds * 1000 / VAD_FRAME_MS)
    preroll_len = 8  # ~240ms kept before speech onset
    preroll, collected = [], []
    started = False
    silence = 0
    print("🎤 listening...", flush=True)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=_FRAME_LEN) as stream:
        for _ in range(max_frames):
            if allow_type_abort and msvcrt and msvcrt.kbhit():
                return None  # user wants to type this turn
            data, _ = stream.read(_FRAME_LEN)
            mono = data[:, 0]
            speech = _VAD.is_speech(mono.tobytes(), SAMPLE_RATE)
            if not started:
                preroll.append(mono.copy())
                if len(preroll) > preroll_len:
                    preroll.pop(0)
                if speech:
                    started = True
                    collected.extend(preroll)
            else:
                collected.append(mono.copy())
                silence = 0 if speech else silence + 1
                if silence >= silence_limit:
                    break
    print("...", flush=True)
    if not collected:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(collected)


def transcribe(pcm) -> str:
    """faster-whisper, English. int16 PCM in -> stripped text out. Empty audio -> empty string."""
    if pcm is None or len(pcm) == 0:
        return ""
    audio = pcm.astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(audio, language="en")
    return "".join(s.text for s in segments).strip()


_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://\S+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _clean_for_speech(text: str) -> str:
    # code blocks are not read aloud — replaced with a single spoken note
    text = _CODE_BLOCK.sub(" I've put the code on screen. ", text)
    text = re.sub(r"(?:\s*I've put the code on screen\.\s*){2,}", " I've put the code on screen. ", text)
    text = _INLINE_CODE.sub(lambda m: m.group(0).strip("`"), text)
    text = _MD_LINK.sub(r"\1", text)          # keep link text, drop the URL
    text = _BARE_URL.sub(" a link ", text)    # don't read raw URLs aloud
    text = re.sub(r"[#*_>|`]", "", text)       # strip markdown punctuation
    return text


_SYNTH_SENTINEL = object()


def speak(text: str) -> None:
    """Pipelined sentence streaming: a worker thread synthesizes sentences AHEAD (queue depth 2)
    while a single continuous output stream plays the current one — so the next chunk is already
    rendered the moment the current ends, with no synth pause between sentences. Sentence order is
    preserved (single FIFO producer); a synth failure is logged and that sentence skipped without
    stalling the queue; the stream drains the final sentence fully before closing."""
    cleaned = _clean_for_speech(text)
    sentences = [s.strip() for s in _SENT_SPLIT.split(cleaned)
                 if s.strip() and re.search(r"[A-Za-z0-9]", s)]
    if not sentences:
        return

    q: queue.Queue = queue.Queue(maxsize=2)  # synth-ahead depth

    def producer():
        for sent in sentences:
            try:
                audio, sr = _synth(sent)
            except Exception as e:  # never deadlock the queue on a bad sentence
                print(f"[voice] synth failed, skipping sentence: {repr(e)[:80]}", file=sys.stderr)
                continue
            if audio.size:
                q.put((audio.reshape(-1, 1), sr))  # blocks when full → backpressure on the worker
        q.put(_SYNTH_SENTINEL)

    worker = threading.Thread(target=producer, daemon=True)
    worker.start()

    stream = None
    try:
        while True:
            item = q.get()
            if item is _SYNTH_SENTINEL:
                break
            audio, sr = item
            if stream is None:  # open once; sample rate is constant within an engine
                stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16")
                stream.start()
            stream.write(audio)  # paced by playback; next chunk is already queued → seamless
    finally:
        if stream is not None:
            stream.stop()   # Pa_StopStream drains buffered audio before returning (full tail)
            stream.close()
        worker.join()


def read_typed_line() -> str:
    """Read a full typed line after a keypress already arrived (Windows). Echoes as typed."""
    if msvcrt is None:
        return sys.stdin.readline().strip()
    first = msvcrt.getwche()
    if first in ("\r", "\n"):
        return sys.stdin.readline().strip()
    return (first + sys.stdin.readline()).strip()


# ---- API helpers (used by app/api.py; the local loops don't need them) ----

def transcribe_file(path: str) -> str:
    """Transcribe any audio file (webm/opus/ogg/wav/m4a/...) straight from disk. faster-whisper
    decodes + resamples to 16k mono via av (PyAV's bundled ffmpeg libs), so NO external ffmpeg
    binary is required — phone-browser MediaRecorder webm/opus is handled by the same decoder."""
    segments, _ = _whisper.transcribe(path, language="en")
    return "".join(s.text for s in segments).strip()


# --- junk-transcript gate (voice) ----------------------------------------------------------------
# Whisper sometimes emits a stray pronoun/filler from noise or a half-word. Don't spend a full turn
# (LLM + memory write + the Claude ladder) on it — a garbled clip once spawned a selfmod proposal,
# and a lone "You" got a confident reply. Conservative: real short commands/confirmations still pass.
_VOICE_OK = {"yes", "no", "ok", "okay", "yep", "yeah", "nope", "sure", "stop", "go", "wait", "next",
             "back", "up", "down", "open", "close", "mute", "play", "pause", "hi", "hey", "help",
             "cancel", "done", "more", "louder", "quieter", "repeat", "again", "now", "off", "on",
             "left", "right", "skip", "send"}
_VOICE_FILLER = {"you", "uh", "um", "the", "a", "an", "hmm", "mm", "mhm", "huh", "er", "ah", "oh",
                 "i", "it", "that", "this", "and", "so", "to", "of", "is", "in", "me", "my", "he",
                 "she", "they", "we", "us", "your", "like", "well", "yo", "ha", "hm", "but", "or"}
# Whole-phrase junk: faster-whisper's well-known silence/noise hallucinations (it emits these video
# sign-offs on near-silence) plus multi-word fillers the per-word rule can't catch. Matched against
# the normalized transcript. Kept to UNAMBIGUOUS non-commands — real answers like "I don't know" /
# "I think so" are deliberately NOT here so they still pass.
_VOICE_JUNK_PHRASES = {
    "thank you", "thanks", "thank you very much", "thank you so much", "thank you for watching",
    "thanks for watching", "thanks for watching the video", "please subscribe", "subscribe",
    "like and subscribe", "you know", "i mean", "bye bye", "okay bye", "see you", "see you next time",
}


def is_junk_transcript(text: str) -> bool:
    """True if a transcript isn't worth running a turn on. Conservative by design: short REAL
    commands/confirmations ('yes', 'no', 'stop', 'open notepad') pass. Junk = empty, <3 non-command
    chars, a known whisper hallucination phrase, or an utterance whose words are ALL fillers."""
    s = (text or "").strip().lower().strip(".,!?;:").strip()
    if not s:
        return True
    if s in _VOICE_OK:                 # known short command/confirmation -> real
        return False
    if s in _VOICE_JUNK_PHRASES:       # known whisper hallucination / multi-word filler
        return True
    words = s.split()
    if len(s) < 3:                     # too short and not a known command
        return True
    if words and all(w in _VOICE_FILLER for w in words):   # every word is filler (1+ words)
        return True
    return False


def synth_to_wav_b64(text: str) -> str:
    """Synthesize text -> base64-encoded WAV string (for the junk-gate spoken nudge)."""
    import io, wave, base64
    pcm, sr = synth_to_pcm(text)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def synth_to_pcm(text: str):
    """Synthesize a full reply to one int16 PCM array (+ sample rate) — same engine and cleaning
    as speak(), but rendered to a buffer (for the API to wrap as a WAV) instead of the speakers."""
    cleaned = _clean_for_speech(text)
    parts, sr = [], TTS_RATE
    for sent in _SENT_SPLIT.split(cleaned):
        sent = sent.strip()
        if not sent or not re.search(r"[A-Za-z0-9]", sent):
            continue
        audio, sr = _synth(sent)
        if audio.size:
            parts.append(audio)
    if not parts:
        return np.zeros(0, dtype=np.int16), sr
    return np.concatenate(parts), sr
