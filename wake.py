"""Wake-word loop for the LOCAL always-on brain (the MSI). Dormant -> "Hey Nervice" -> active
conversation -> back to dormant on a sleep phrase or a quiet follow-up window.

DORMANT is cheap and private: Picovoice Porcupine listens on-device for the custom wake word only —
NO transcription, no network, ~sub-1% of one CPU core. Nothing is sent anywhere and nothing is
recognized except the wake word itself. ACTIVE reuses the EXISTING turn pipeline unchanged
(record -> whisper STT -> junk gate -> respond [memory/persona/ladder/safety] -> Kokoro TTS).

If PICOVOICE_ACCESS_KEY or the .ppn is missing, this prints a clear setup message and falls back to
the normal always-listening voice loop, so `python wake.py` always does something useful. The
existing `python voice.py` (push-to-talk voice) and `python nervice.py` (text) REPLs are untouched.

PHONE wake word is a separate future piece (the phone can't run Porcupine from the PWA without a
native shell / the Picovoice Web SDK) — not attempted here.

  Setup (one time):
    1) Sign up free at https://console.picovoice.ai and copy your AccessKey.
    2) Train a custom wake word "Hey Nervice" for the Windows platform; download the .ppn.
    3) Put it at models/wakeword/hey-nervice.ppn
    4) Add to .env:  PICOVOICE_ACCESS_KEY=your_key_here
    5) Run:  python wake.py
"""
import os
import re
import sys
import uuid
import asyncio
import pathlib

from dotenv import load_dotenv
load_dotenv()

from app.chat import respond, greeting
from app.voice import record_until_silence, transcribe, speak, is_junk_transcript, WHISPER_PATH
from nervice import store_exchange

USER = "nate"
_PPN = pathlib.Path("models/wakeword/hey-nervice.ppn")

# Editable: phrases that, while ACTIVE, put Nervice back to sleep.
SLEEP_WORDS = ["stand by", "standby", "goodbye", "good bye", "talk to you later",
               "talk later", "go to sleep", "that's all", "thats all", "go dormant", "never mind"]
FOLLOWUP_SECONDS = 6      # after a reply, stay active this long waiting for a natural follow-up
WAKE_CUE = "Yes?"
SLEEP_ACK = "Standing by."

_SETUP_MSG = ("Wake word disabled — place hey-nervice.ppn in models/wakeword/ and set "
              "PICOVOICE_ACCESS_KEY in .env to enable it. Running the normal voice loop instead.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]+", " ", (s or "").lower()).strip()


def _is_sleep(text: str) -> bool:
    n = " " + _norm(text) + " "
    return any((" " + sw + " ") in n for sw in SLEEP_WORDS)


def init_porcupine():
    """Return (porcupine, recorder) if the key + .ppn are present and Porcupine inits, else None
    with a specific logged reason. Never raises — a None just means 'run without wake word'."""
    key = os.environ.get("PICOVOICE_ACCESS_KEY", "").strip()
    if not key:
        print("[wake] PICOVOICE_ACCESS_KEY is not set in .env.", file=sys.stderr)
        return None
    if not _PPN.exists():
        print(f"[wake] wake-word file not found at {_PPN}.", file=sys.stderr)
        return None
    try:
        import pvporcupine
        from pvrecorder import PvRecorder
        pp = pvporcupine.create(access_key=key, keyword_paths=[str(_PPN)])
        rec = PvRecorder(frame_length=pp.frame_length)
        return pp, rec
    except Exception as e:
        print(f"[wake] Porcupine init failed ({repr(e)[:120]}).", file=sys.stderr)
        return None


def wait_for_wake(pp, rec) -> bool | None:
    """Block (cheaply) until the wake word fires. Returns True on detection, None on interrupt.
    On-device only — reads tiny frames and runs Porcupine; no transcription, no network."""
    try:
        rec.start()
        while True:
            if pp.process(rec.read()) >= 0:
                return True
    except KeyboardInterrupt:
        return None
    finally:
        try:
            rec.stop()
        except Exception:
            pass


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


async def wake_main(pp, rec) -> None:
    conversation_id = str(uuid.uuid4())
    window: list = []
    pending: set = set()
    print(f"Nervice dormant (STT: {WHISPER_PATH}). Say “Hey Nervice” to wake me. Ctrl-C to quit.")
    try:
        while True:
            woke = await asyncio.to_thread(wait_for_wake, pp, rec)
            if woke is None:
                break                               # interrupted
            await active_session(window, conversation_id, pending)
            print("Nervice dormant.")
    finally:
        try:
            pp.delete(); rec.delete()
        except Exception:
            pass
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _normal_voice_fallback() -> None:
    """No wake word configured -> behave like the existing always-listening voice loop so
    `python wake.py` still works. Delegates to the untouched root voice.py main()."""
    import voice as _voiceloop   # the existing local voice REPL (root voice.py)
    await _voiceloop.main()


async def main() -> None:
    pr = init_porcupine()
    if pr is None:
        print(_SETUP_MSG)
        await _normal_voice_fallback()
        return
    pp, rec = pr
    await wake_main(pp, rec)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nLater, Nate.")
