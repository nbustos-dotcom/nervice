import asyncio, uuid
from app.chat import respond, save_exchange

USER = "nate"


async def main():
    conversation_id = str(uuid.uuid4())
    window = []
    print("Nervice is ready. Type 'exit' or 'quit' to leave.\n")
    while True:
        try:
            user_message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nLater, Nate.")
            break
        if user_message.lower() in {"exit", "quit"}:
            print("Later, Nate.")
            break
        if not user_message:
            continue
        print("Nervice: ", end="", flush=True)
        reply = await respond(USER, user_message, window)
        window.append({"role": "user", "content": user_message})
        window.append({"role": "assistant", "content": reply})
        window[:] = window[-12:]
        await save_exchange(USER, conversation_id, user_message, reply)


asyncio.run(main())
