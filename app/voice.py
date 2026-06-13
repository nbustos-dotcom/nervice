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

SAMPLE_RATE = 16000          # whisper + Silero VAD rate

# --- Silero VAD ("ears") replaces the old webrtcvad gate. Free, MIT, runs on the existing
# onnxruntime (no torch). webrtcvad stays ONLY as a fallback if the Silero model is ever missing.
SILERO_WINDOW = 512          # Silero's fixed 16k window (32ms)
SILERO_CONTEXT = 64          # Silero v5 prepends this many prior samples to each window (16k)
SILERO_SPEECH_PROB = 0.5     # per-window speech-probability threshold
SILERO_MIN_SPEECH_MS = 200   # contains_speech(): sustained speech needed to accept a received clip
# Low-confidence STT gate (whisper metrics) — a second net behind Silero on the API clip path.
NO_SPEECH_DROP = 0.85        # whisper no_speech_prob >= this -> drop as non-speech
LOGPROB_DROP = -1.0          # whisper avg_logprob   <= this -> drop as too unconfident (tightened)
INTER_SENTENCE_PAUSE_MS = 90  # trailing silence per synthesized sentence -> natural pacing

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_KOKORO_MODEL = _ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"
_KOKORO_VOICES = _ROOT / "models" / "kokoro" / "voices-v1.0.bin"
_PIPER_VOICE = _ROOT / "models" / "piper" / "en_US-ryan-high.onnx"
_SILERO_MODEL = _ROOT / "models" / "silero" / "silero_vad.onnx"

VOICE = "am_echo"   # Kokoro voice — swap here (e.g. am_echo, am_adam, am_onyx, bm_george). am_/af_
#                     voices phonemize en-us; bm_/bf_ voices phonemize en-gb (derived below).

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


def _load_silero():
    """Silero VAD via onnxruntime (no torch). Prefer models/silero/silero_vad.onnx; fall back to the
    pip silero-vad package's bundled model (located WITHOUT importing it — find_spec only). CPU EP:
    the model is ~2MB, so leave the GPU for whisper/kokoro. Returns an InferenceSession, or None when
    no model is found (callers then degrade to webrtcvad / a permissive gate)."""
    import importlib.util
    cands = [_SILERO_MODEL]
    try:
        spec = importlib.util.find_spec("silero_vad")
        if spec and spec.submodule_search_locations:
            cands.append(pathlib.Path(spec.submodule_search_locations[0]) / "data" / "silero_vad.onnx")
    except Exception:
        pass
    path = next((p for p in cands if p.is_file()), None)
    if path is None:
        return None
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])


print("[voice] loading local models (STT + TTS + VAD)...", file=sys.stderr)
_t0 = time.time()
_whisper, WHISPER_PATH = _load_whisper()
_synth_raw, TTS_ENGINE, TTS_RATE = _load_tts()
try:
    _SILERO = _load_silero()
except Exception as e:
    print(f"[voice] silero load failed ({repr(e)[:80]}) — webrtcvad fallback", file=sys.stderr)
    _SILERO = None
VAD_ENGINE = "silero" if _SILERO is not None else "webrtcvad"
_SILERO_SR = np.array(SAMPLE_RATE, dtype=np.int64)


def _silero_step(chunk_f32, state, context):
    """One Silero v5 step. The model is trained on [64-sample context | 512-sample window]; we
    prepend the carried context, run, and return (speech_prob, new_state, new_context). The first
    call passes a zero context. (The official OnnxWrapper does this with torch; we do it in numpy
    so no torch dependency is pulled — that was the whole point of running it on onnxruntime.)"""
    inp = np.concatenate([context, np.asarray(chunk_f32, dtype=np.float32).reshape(1, -1)], axis=1)
    out, state = _SILERO.run(None, {"input": inp, "state": state, "sr": _SILERO_SR})
    return float(np.asarray(out).reshape(-1)[0]), state, inp[:, -SILERO_CONTEXT:]


# A short trailing silence on every synthesized sentence: a natural inter-sentence pause that also
# guarantees click-free joins when sentences are concatenated (synth_to_pcm) or streamed back-to-back
# (speak / the WS per-sentence pipeline). Centralized here so every TTS path benefits.
_PAUSE_SAMPLES = np.zeros(int(TTS_RATE * INTER_SENTENCE_PAUSE_MS / 1000), dtype=np.int16)


def _synth(text: str):
    audio, sr = _synth_raw(text)
    if audio.size and _PAUSE_SAMPLES.size:
        audio = np.concatenate([audio, _PAUSE_SAMPLES])
    return audio, sr


print(f"[voice] models loaded in {time.time() - _t0:.1f}s  "
      f"(stt=faster-whisper base.en {WHISPER_PATH}, tts={TTS_ENGINE} @ {TTS_RATE}Hz, vad={VAD_ENGINE})",
      file=sys.stderr)


def record_until_silence(max_seconds: int = 60, trailing_silence: float = 0.7,
                         allow_type_abort: bool = True, start_timeout: float | None = None):
    """Capture 16kHz mono from the default mic, gated by Silero VAD (webrtcvad fallback): start
    collecting on the first voiced window (with a short pre-roll so onsets aren't clipped), stop
    after ~trailing_silence of quiet, hard cap at max_seconds. Returns int16 PCM in memory. If
    allow_type_abort and the user presses a key, returns None to signal "type this turn instead".
    PCM is never written to disk. start_timeout (optional): if no speech ONSET occurs within this
    many seconds, return empty PCM (size 0) — used by the wake loop's short follow-up window."""
    # Pick the VAD primitive + its native frame size. Silero is stateful (its hidden state is carried
    # across windows); webrtcvad is the fallback used only when the Silero model is absent.
    if _SILERO is not None:
        frame_len = SILERO_WINDOW
        win_ms = SILERO_WINDOW * 1000.0 / SAMPLE_RATE
        _state = np.zeros((2, 1, 128), dtype=np.float32)
        _ctx = np.zeros((1, SILERO_CONTEXT), dtype=np.float32)

        def is_speech(mono_i16):
            nonlocal _state, _ctx
            prob, _state, _ctx = _silero_step(mono_i16.astype(np.float32) / 32768.0, _state, _ctx)
            return prob >= SILERO_SPEECH_PROB
    else:
        frame_len = SAMPLE_RATE * 30 // 1000   # webrtcvad 30ms frame (480 samples)
        win_ms = 30.0
        _vad = webrtcvad.Vad(2)

        def is_speech(mono_i16):
            return _vad.is_speech(mono_i16.tobytes(), SAMPLE_RATE)

    silence_limit = int(trailing_silence * 1000 / win_ms)
    max_frames = int(max_seconds * 1000 / win_ms)
    start_frames = int(start_timeout * 1000 / win_ms) if start_timeout else None
    preroll_len = 8  # ~250ms kept before speech onset
    preroll, collected = [], []
    started = False
    silence = 0
    print("listening...", flush=True)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=frame_len) as stream:
        for fi in range(max_frames):
            if allow_type_abort and msvcrt and msvcrt.kbhit():
                return None  # user wants to type this turn
            data, _ = stream.read(frame_len)
            mono = data[:, 0]
            speech = is_speech(mono)
            if not started:
                if start_frames is not None and fi >= start_frames:
                    return np.zeros(0, dtype=np.int16)   # no speech onset in the window
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

# --- spoken-symbol normalization -----------------------------------------------------------------
# Symbols Kokoro would otherwise voice by NAME (it said "dash"/"slash" aloud) become natural words
# or a short pause. Deterministic regex only — negligible cost, no LLM. Applied to the SPOKEN text
# inside _clean_for_speech; the on-screen HUD transcript is never touched. Conservative: ambiguous
# cases (currency, numeric ranges, units) are left for the deeper normalization pass, not guessed.
_NORM_AMP = re.compile(r"\s*&\s*")                   # &  -> and
_NORM_PCT = re.compile(r"\s*%")                      # %  -> percent
_NORM_INWORD_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")  # well-known -> well known ; 2-3 -> 2 3
_NORM_INWORD_SLASH = re.compile(r"(?<=\w)/(?=\w)")   # and/or -> and or ; TCP/IP -> TCP IP
_NORM_UNIDASH = re.compile(r"[‐‑−]")   # hyphen / non-breaking hyphen / minus -> ASCII -
_NORM_EMDASH = re.compile(r"\s*[‒–—―]\s*")   # figure/en/em dash, horizontal bar -> pause
_NORM_SEP = re.compile(r"\s*[/|•·▪◦‣⁃]\s*|\s+-\s+")   # slash/pipe/bullet or " - " separator -> pause
_NORM_HYPHEN_LEFT = re.compile(r"-")                 # any leftover hyphen -> space (never "dash")
_NORM_WS = re.compile(r"[ \t]{2,}")
_NORM_SPACE_PUNCT = re.compile(r"\s+([,.;:!?])")     # " ," -> ","
_NORM_COMMA_RUN = re.compile(r"(?:,\s*){2,}")        # ", , " -> ", "
_NORM_LEAD = re.compile(r"^[\s,;:.]+")               # no leading pause/punctuation


def _normalize_for_speech(text: str) -> str:
    """Convert symbols a TTS engine would read by name into spoken words or a short pause. SPOKEN
    stream only (called from _clean_for_speech) — never alters the HUD transcript."""
    text = _NORM_UNIDASH.sub("-", text)            # fold Unicode hyphens/minus to ASCII (models emit ‑)
    text = _NORM_AMP.sub(" and ", text)            # R&D -> R and D  (before separators)
    text = _NORM_PCT.sub(" percent", text)         # 50% -> 50 percent
    text = _NORM_INWORD_HYPHEN.sub(" ", text)      # well-known -> well known
    text = _NORM_INWORD_SLASH.sub(" ", text)       # and/or -> and or
    text = _NORM_EMDASH.sub(", ", text)            # a — b -> a, b
    text = _NORM_SEP.sub(", ", text)               # a / b | c • d  and  "x - y" -> pauses
    text = _NORM_HYPHEN_LEFT.sub(" ", text)        # leading/trailing/leftover hyphen -> space
    text = _NORM_WS.sub(" ", text)                 # tidy the spacing the substitutions create
    text = _NORM_SPACE_PUNCT.sub(r"\1", text)
    text = _NORM_COMMA_RUN.sub(", ", text)
    text = _NORM_LEAD.sub("", text)
    return text.strip()


# --- screen-only tokens: commit hashes / file paths / long IDs belong on Nate's SCREEN, not read
# aloud. Dropped from the SPOKEN stream only (the HUD transcript keeps them verbatim). The persona
# is told to place them parenthetically/trailing so the line still reads once they're gone. Run
# BEFORE symbol normalization, while a path's slashes are still intact for detection.
_SCREEN_PAREN = re.compile(r"\s*\((?:[^)]*[\\/][^)]*|\s*(?=[0-9a-f]*\d)[0-9a-f]{7,}\s*)\)")  # (path)/(hash)
_SCREEN_PATH = re.compile(r"\b[\w.\-]+(?:[\\/][\w.\-]+)*[\\/][\w\-]+\.[A-Za-z]{1,5}\b")        # bare path w/ ext
_SCREEN_HASH = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b")                                  # bare commit hash


def _strip_screen_only(text: str) -> str:
    """Drop tokens that are exact-on-screen but should never be read aloud: parenthetical hash/path
    notes, bare file paths (-> 'a file'), and bare commit hashes. Spoken stream only — the HUD text
    is never run through this. A 7+ hex run must include a digit, so real words aren't mistaken for
    a hash; bare-path detection requires a file extension, so 'and/or' and 'TCP/IP' are left alone."""
    text = _SCREEN_PAREN.sub("", text)         # "(a918e75)" / "(docs/x.py)" -> gone
    text = _SCREEN_PATH.sub(" a file ", text)  # bare "app/voice.py" -> "a file"
    text = _SCREEN_HASH.sub("", text)          # bare "a918e75" -> gone
    return text


def _clean_for_speech(text: str) -> str:
    # code blocks are not read aloud — replaced with a single spoken note
    text = _CODE_BLOCK.sub(" I've put the code on screen. ", text)
    text = re.sub(r"(?:\s*I've put the code on screen\.\s*){2,}", " I've put the code on screen. ", text)
    text = _INLINE_CODE.sub(lambda m: m.group(0).strip("`"), text)
    text = _MD_LINK.sub(r"\1", text)          # keep link text, drop the URL
    text = _BARE_URL.sub(" a link ", text)    # don't read raw URLs aloud
    text = _strip_screen_only(text)           # hashes/paths/IDs: shown on screen, never spoken
    text = re.sub(r"[#*_>`]", "", text)        # strip markdown formatting (| is a pause, handled below)
    return _normalize_for_speech(text)         # symbols -> spoken words / pauses (spoken stream only)


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


def _decode_16k(path: str):
    """Decode any audio file to 16k mono float32 once (faster-whisper's av-based decoder — no ffmpeg
    binary), so Silero VAD and whisper can share a single decode."""
    from faster_whisper.audio import decode_audio
    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def contains_speech(audio) -> bool:
    """Silero VAD gate for a received clip: True only when there is >= SILERO_MIN_SPEECH_MS of
    SUSTAINED speech. This is what rejects the room noise / silence the browser's VAD let through,
    BEFORE STT spends a turn. `audio` is 16k mono float32. Permissive (returns True) if Silero is
    unavailable — the junk gate then remains the only net (pre-Silero behavior)."""
    if _SILERO is None:
        return True
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    if a.size < SILERO_WINDOW:
        return False
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, SILERO_CONTEXT), dtype=np.float32)
    win_ms = SILERO_WINDOW * 1000.0 / SAMPLE_RATE
    need = max(1, int(SILERO_MIN_SPEECH_MS / win_ms))   # sustained windows = SILERO_MIN_SPEECH_MS
    run = best = 0
    for i in range(0, a.size - SILERO_WINDOW + 1, SILERO_WINDOW):
        prob, state, context = _silero_step(a[i:i + SILERO_WINDOW], state, context)
        if prob >= SILERO_SPEECH_PROB:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best >= need


def _transcribe_with_metrics(audio):
    """faster-whisper on a 16k float32 array -> (text, worst_no_speech_prob, worst_avg_logprob), so
    the junk gate can drop low-confidence noise. Empty / zero-segment audio -> ('', 1.0, 0.0)."""
    segments, _ = _whisper.transcribe(audio, language="en")
    segs = list(segments)
    text = "".join(s.text for s in segs).strip()
    if not segs:
        return text, 1.0, 0.0
    return text, max(s.no_speech_prob for s in segs), min(s.avg_logprob for s in segs)


def transcribe_clip(path: str) -> dict:
    """Receive-side STT for the API (/voice, /ws/voice). Decode once, gate on Silero VAD, then
    transcribe with confidence. Returns:
      {'speech': bool, 'text': str, 'junk': bool, 'no_speech_prob': float, 'avg_logprob': float}
    speech=False -> Silero heard no speech (silent drop, no STT cost beyond the decode);
    junk=True    -> transcribed but not worth a turn (empty / punct-only / filler / low-confidence)."""
    audio = _decode_16k(path)
    if not contains_speech(audio):
        return {"speech": False, "text": "", "junk": True, "no_speech_prob": 1.0, "avg_logprob": 0.0}
    text, nsp, alp = _transcribe_with_metrics(audio)
    return {"speech": True, "text": text,
            "junk": is_junk_transcript(text, no_speech_prob=nsp, avg_logprob=alp),
            "no_speech_prob": nsp, "avg_logprob": alp}


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
# Repeated hum / mumble: any run of m/h sounds ("mmm", "hmm", "mhm", "hm", "mm hmm" ...). Whisper
# emits a confident-LOOKING mumble that the fixed filler set can't enumerate; matched per word
# (after stripping punctuation) so "Mmm. Mmm." is caught. No real English word is only m's and h's.
_MUMBLE = re.compile(r"^[mh]+$")


def is_junk_transcript(text: str, *, no_speech_prob: float | None = None,
                       avg_logprob: float | None = None) -> bool:
    """True if a transcript isn't worth running a turn on. Conservative: short REAL commands/
    confirmations ('yes', 'no', 'stop', 'open notepad') pass. Junk = empty, punctuation-only,
    <3 non-command chars, a known whisper hallucination phrase, an all-filler utterance, OR (when
    the API clip path supplies whisper metrics) a low-confidence transcript — high no_speech_prob
    or very low avg_logprob, i.e. noise the VAD let slip through."""
    s = (text or "").strip().lower().strip(".,!?;:").strip()
    if not s:
        return True
    if s in _VOICE_OK:                 # known short command/confirmation -> keep (whitelist wins)
        return False
    if no_speech_prob is not None and no_speech_prob >= NO_SPEECH_DROP:   # whisper: not speech
        return True
    if avg_logprob is not None and avg_logprob <= LOGPROB_DROP:           # whisper: too unconfident
        return True
    if s in _VOICE_JUNK_PHRASES:       # known whisper hallucination / multi-word filler
        return True
    if not re.search(r"[a-z0-9]", s):  # punctuation/symbol-only ("...", "?!", "-")
        return True
    words = [w.strip(".,!?;:'\"-") for w in s.split()]   # per-word punctuation off ("mmm." -> "mmm")
    words = [w for w in words if w]
    if len(s) < 3:                     # too short and not a known command
        return True
    if words and all(w in _VOICE_FILLER or _MUMBLE.match(w) for w in words):  # all filler / humming
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
