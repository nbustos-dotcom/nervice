"""Wake-word loop for the LOCAL always-on brain (the MSI). Dormant -> "Hey Jarvis" -> active
conversation -> back to dormant on a sleep phrase or a quiet follow-up window.

Engine: openWakeWord (https://github.com/dscripka/openWakeWord) — fully FREE, NO account, NO API
key, NO network for detection. It runs small ONNX models on-device (via the onnxruntime already
installed for Kokoro). DORMANT is cheap and private: openWakeWord scores the wake word only — NO
transcription, nothing leaves the machine, ~6% of one CPU core. ACTIVE reuses the EXISTING turn
pipeline unchanged (record -> whisper STT -> junk gate -> respond [memory/persona/ladder/safety] ->
Kokoro TTS).

WAKE WORD: ships with the bundled "Hey Jarvis" model as a fallback. For the custom "Hey Nervice"
word, the least-painful free path is openWakeWord's official training Colab (free Google T4 GPU,
~10-30 min, only a normal Google account):
    https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb
In the config cell set  config["target_phrase"] = ["hey nervice"]  (and model_name "hey_nervice"),
run the cells (install -> download data -> generate -> augment -> train -> export), then download
my_custom_model/hey_nervice.onnx into models/wakeword/. wake.py auto-prefers any *nervice*.onnx
over jarvis — no code change. Tune WAKE_THRESHOLD below if it's too eager/deaf. Full walkthrough:
models/wakeword/README. (Local training on this box is NOT recommended — multi-GB augmentation
downloads + a torch training stack that fights Python 3.14/Windows; the Colab path avoids all of it.)

If no model is present, this prints a clear message and falls back to the normal always-listening
voice loop, so `python wake.py` always does something useful. `python wake.py --setup` downloads the
free model (one time). Old REPLs (python voice.py, python nervice.py) are untouched.

PHONE wake word is a separate future piece (the PWA needs the openWakeWord/TF.js web build or a
native shell) — not attempted here.
"""
import os
import re
import sys
import uuid
import asyncio
import pathlib

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import sounddevice as sd

from app.chat import respond, greeting
from app.voice import record_until_silence, transcribe, speak, is_junk_transcript, WHISPER_PATH, SAMPLE_RATE
from nervice import store_exchange

USER = "nate"
_WAKE_DIR = pathlib.Path("models/wakeword")
WAKE_THRESHOLD = 0.5          # openWakeWord's recommended default for a positive detection
_FRAME = 1280                 # 80ms @ 16kHz — openWakeWord's preferred chunk

# Editable: phrases that, while ACTIVE, put Nervice back to sleep.
SLEEP_WORDS = ["stand by", "standby", "goodbye", "good bye", "talk to you later",
               "talk later", "go to sleep", "that's all", "thats all", "go dormant", "never mind"]
FOLLOWUP_SECONDS = 6          # after a reply, stay active this long waiting for a natural follow-up
WAKE_CUE = "Yes?"
SLEEP_ACK = "Standing by."

_SETUP_MSG = ("Wake word disabled — no model (*.onnx) in models/wakeword/. Run  python wake.py --setup  "
              "to download the free 'hey jarvis' model (no account, no key). Running the normal voice loop instead.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]+", " ", (s or "").lower()).strip()


def _is_sleep(text: str) -> bool:
    n = " " + _norm(text) + " "
    return any((" " + sw + " ") in n for sw in SLEEP_WORDS)


def _find_model() -> pathlib.Path | None:
    """Pick the wake-word model in models/wakeword/: prefer a custom 'nervice', then 'jarvis', else
    the first *.onnx present. None if the dir has no model."""
    if not _WAKE_DIR.exists():
        return None
    onnx = sorted(p for p in _WAKE_DIR.glob("*.onnx"))
    if not onnx:
        return None
    for key in ("nervice", "jarvis"):
        for p in onnx:
            if key in p.name.lower():
                return p
    return onnx[0]


def init_wakeword():
    """Load the openWakeWord model from models/wakeword/. Returns (model, name, path) or None with a
    specific logged reason. No API key, no network at detection time. Never raises."""
    mp = _find_model()
    if mp is None:
        print(f"[wake] no wake-word model (*.onnx) found in {_WAKE_DIR}.", file=sys.stderr)
        return None
    try:
        from openwakeword.model import Model
        model = Model(wakeword_models=[str(mp)], inference_framework="onnx")
        name = list(model.models.keys())[0]
        return model, name, mp
    except Exception as e:
        print(f"[wake] openWakeWord init failed ({repr(e)[:140]}).", file=sys.stderr)
        return None


def wait_for_wake(model) -> bool | None:
    """Block (cheaply) until the wake word scores past the threshold. Returns True on detection,
    None on interrupt. Fully on-device — scores the wake word only; no transcription, no network."""
    try:
        if hasattr(model, "reset"):
            model.reset()                       # clear any buffered scores from a prior session
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=_FRAME) as stream:
            while True:
                data, _ = stream.read(_FRAME)
                scores = model.predict(data[:, 0])
                if scores and max(scores.values()) >= WAKE_THRESHOLD:
                    return True
    except KeyboardInterrupt:
        return None


def _capture_text() -> str:
    """One ACTIVE listen: short follow-up window for speech onset, then full capture + transcribe.
    Empty string means nothing was said (-> go dormant). Voice-only (no type-abort in wake mode)."""
    pcm = record_until_silence(start_timeout=FOLLOWUP_SECONDS, allow_type_abort=False)
    if pcm is None or getattr(pcm, "size", 0) == 0:
        return ""
    return transcribe(pcm)


async def active_session(window: list, conversation_id: str, pending: set) -> None:
    """Run one ACTIVE conversation after a wake: turns until a sleep phrase, a quiet follow-up
    window, or junk. Reuses respond() unchanged — same memory/persona/ladder/safety. Returns when
    it's time to go dormant."""
    speak(WAKE_CUE)
    first = True
    while True:
        text = _capture_text()
        if not text:
            break                                  # quiet follow-up window -> dormant
        print(f"You (voice): {text}")
        if _is_sleep(text):                         # sleep phrase -> dormant
            speak(SLEEP_ACK)
            break
        if is_junk_transcript(text):                # existing junk gate still applies in ACTIVE
            speak("Didn't catch that.")
            if first:
                first = False
                continue                            # one more chance right after waking
            break
        reply = await respond(USER, text, window, voice_mode=True, speak=speak)
        speak(reply)
        window.append({"role": "user", "content": text})
        window.append({"role": "assistant", "content": reply})
        window[:] = window[-12:]
        store_exchange(pending, USER, conversation_id, text, reply)
        first = False


async def wake_main(model, name) -> None:
    conversation_id = str(uuid.uuid4())
    window: list = []
    pending: set = set()
    print(f"Nervice dormant (wake word: {name}, STT: {WHISPER_PATH}). "
          "Say the wake word to talk. Ctrl-C to quit.")
    try:
        while True:
            woke = await asyncio.to_thread(wait_for_wake, model)
            if woke is None:
                break                               # interrupted
            await active_session(window, conversation_id, pending)
            print("Nervice dormant.")
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _normal_voice_fallback() -> None:
    """No wake model present -> behave like the existing always-listening voice loop so
    `python wake.py` still works. Delegates to the untouched root voice.py main()."""
    import voice as _voiceloop
    await _voiceloop.main()


def setup_models() -> None:
    """One-time, no account: download the free bundled openWakeWord models and place 'hey_jarvis' in
    models/wakeword/. Needs network once; detection afterward is fully offline."""
    import app.net  # noqa  (truststore for the one-time download)
    import shutil
    import openwakeword
    import openwakeword.utils
    _WAKE_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading free openWakeWord models (no account/key)…")
    openwakeword.utils.download_models(["hey_jarvis"])
    res = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
    src = next(iter(res.glob("hey_jarvis*.onnx")), None)
    if src:
        shutil.copy(src, _WAKE_DIR / "hey_jarvis.onnx")
        print(f"Ready: {_WAKE_DIR / 'hey_jarvis.onnx'}. Now run:  python wake.py")
    else:
        print("Download ran but hey_jarvis.onnx wasn't found in the openWakeWord resources.")


async def main() -> None:
    mw = init_wakeword()
    if mw is None:
        print(_SETUP_MSG)
        await _normal_voice_fallback()
        return
    model, name, _ = mw
    await wake_main(model, name)


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_models()
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nLater, Nate.")
