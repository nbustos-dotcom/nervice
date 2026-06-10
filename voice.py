import asyncio, uuid, sys

from app.chat import respond, greeting
from app.voice import record_until_silence, transcribe, speak, read_typed_line, WHISPER_PATH
from nervice import _handle_meta, store_exchange

USER = "nate"


async def main():
    conversation_id = str(uuid.uuid4())
    window = []
    pending: set = set()
    print(f"\nNervice is listening (STT: {WHISPER_PATH}). "
          "Speak, or press a key to type instead. Say or type '/exit' to leave.\n")
    try:
        g = await greeting(USER, voice_mode=True)
        if g:
            print(f"Nervice: {g}\n")
            speak(g)
    except Exception as e:
        print(f"[greeting skipped] {repr(e)[:80]}", file=sys.stderr)
    try:
        while True:
            # Default modality is the mic. Pressing a key during "listening" drops to typed input
            # for that turn (record_until_silence returns None on keypress).
            try:
                pcm = record_until_silence()
            except KeyboardInterrupt:
                print("\nLater, Nate.")
                break

            if pcm is None:  # user pressed a key — typed fallback for this turn
                user_message = read_typed_line()
                source = "typed"
            else:
                user_message = transcribe(pcm)
                source = "voice"

            if not user_message:
                continue  # empty/noise — listen again
            print(f"You ({source}): {user_message}")

            meta = _handle_meta(user_message)  # slash/known commands, typed only
            if meta == "exit":
                print("Later, Nate.")
                break
            if meta == "handled":
                continue

            print("Nervice: ", end="", flush=True)
            reply = await respond(USER, user_message, window, voice_mode=True)
            speak(reply)
            window.append({"role": "user", "content": user_message})
            window.append({"role": "assistant", "content": reply})
            window[:] = window[-12:]
            store_exchange(pending, USER, conversation_id, user_message, reply)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nLater, Nate.")
