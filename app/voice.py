import app.net  # noqa  (truststore: Norton TLS — needed for first-run model downloads)

import re
import sys
import time
import pathlib

import numpy as np
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel
from piper import PiperVoice

# All audio is processed locally — faster-whisper (STT) and Piper (TTS) run on this machine.
# Nothing audio-related leaves the box; only the final TEXT goes to the existing pipeline,
# exactly like typed input. No audio files are written; PCM lives in memory only.

SAMPLE_RATE = 16000          # whisper + webrtcvad rate
VAD_FRAME_MS = 30            # webrtcvad accepts 10/20/30ms frames
_FRAME_LEN = SAMPLE_RATE * VAD_FRAME_MS // 1000   # 480 samples / 30ms
_VAD = webrtcvad.Vad(2)      # aggressiveness 2 (0 lax .. 3 strict)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PIPER_VOICE = _ROOT / "models" / "piper" / "en_US-lessac-medium.onnx"

try:
    import msvcrt  # Windows: lets a keypress drop a turn to typed input
except ImportError:
    msvcrt = None


def _load_whisper():
    """Prefer CUDA; fall back to CPU int8 if the CUDA/cuBLAS runtime is missing (common on
    Windows). Warm the model once so the first real transcription isn't slow."""
    for dev, ct in (("cuda", "float16"), ("cpu", "int8")):
        try:
            m = WhisperModel("base.en", device=dev, compute_type=ct)
            list(m.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")[0])
            return m, f"{dev}/{ct}"
        except Exception as e:
            print(f"[voice] whisper {dev}/{ct} unavailable: {repr(e)[:80]}", file=sys.stderr)
    raise RuntimeError("no usable faster-whisper backend (cuda and cpu both failed)")


print("[voice] loading local models (STT + TTS)...", file=sys.stderr)
_t0 = time.time()
_whisper, WHISPER_PATH = _load_whisper()
_piper = PiperVoice.load(str(_PIPER_VOICE))
TTS_RATE = _piper.config.sample_rate
print(f"[voice] models loaded in {time.time() - _t0:.1f}s  "
      f"(stt=faster-whisper base.en {WHISPER_PATH}, tts=piper lessac-medium @ {TTS_RATE}Hz)",
      file=sys.stderr)


def record_until_silence(max_seconds: int = 60, trailing_silence: float = 1.0,
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


def speak(text: str) -> None:
    """Sentence-stream the reply: split on .!? and synthesize+play each sentence as it's ready, so
    the first sentence is audible ASAP. Skips empty/markdown-junk sentences. Code blocks are not
    spoken (replaced earlier with 'I've put the code on screen')."""
    cleaned = _clean_for_speech(text)
    for sent in _SENT_SPLIT.split(cleaned):
        sent = sent.strip()
        if not sent or not re.search(r"[A-Za-z0-9]", sent):
            continue  # skip empty / punctuation-only / junk
        chunks = list(_piper.synthesize(sent))
        if not chunks:
            continue
        audio = np.concatenate([c.audio_int16_array for c in chunks])
        sd.play(audio, samplerate=TTS_RATE)
        sd.wait()


def read_typed_line() -> str:
    """Read a full typed line after a keypress already arrived (Windows). Echoes as typed."""
    if msvcrt is None:
        return sys.stdin.readline().strip()
    first = msvcrt.getwche()
    if first in ("\r", "\n"):
        return sys.stdin.readline().strip()
    return (first + sys.stdin.readline()).strip()
