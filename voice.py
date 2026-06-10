import asyncio, uuid

from app.chat import respond, save_exchange
from app.voice import record_until_silence, transcribe, speak, read_typed_line, WHISPER_PATH
from nervice import _handle_meta

USER = "nate"


async def main():
    conversation_id = str(uuid.uuid4())
    window = []
    print(f"\nNervice is listening (STT: {WHISPER_PATH}). "
          "Speak, or press a key to type instead. Say or type 'exit' to leave.\n")
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

        if user_message.lower() in {"exit", "quit"}:
            print("Later, Nate.")
            break
        if _handle_meta(user_message):  # proposals/show/approve/reject, typed only
            continue

        print("Nervice: ", end="", flush=True)
        reply = await respond(USER, user_message, window)
        speak(reply)
        window.append({"role": "user", "content": user_message})
        window.append({"role": "assistant", "content": reply})
        window[:] = window[-12:]
        await save_exchange(USER, conversation_id, user_message, reply)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nLater, Nate.")
