import asyncio, uuid
from app.chat import respond, save_exchange
from app import selfmod

USER = "nate"


def _handle_meta(cmd: str) -> bool:
    """Intercept self-mod REPL commands before they reach the LLM. Returns True if handled."""
    parts = cmd.split(maxsplit=1)
    verb = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if verb == "proposals":
        pending = [p for p in selfmod.list_proposals() if p.get("status") == "pending"]
        if not pending:
            print("No pending proposals.")
        else:
            for p in pending:
                print(f"  {p['id']}  {p.get('paths')}  — {p.get('summary', '')[:100]}")
        return True

    if verb == "show" and arg:
        rec = selfmod.get(arg)
        if rec is None:
            print(f"No proposal {arg}.")
            return True
        print(f"id: {rec['id']}   status: {rec.get('status')}   paths: {rec.get('paths')}")
        print(f"instruction: {rec.get('instruction')}")
        print(f"summary: {rec.get('summary')}")
        if rec.get("reason"):
            print(f"reason: {rec.get('reason')}")
        patch = selfmod.PROPOSALS_DIR / f"{arg}.patch"
        if patch.exists():
            print("\n--- patch ---")
            print(patch.read_text(encoding="utf-8"))
        return True

    if verb == "approve" and arg:
        ok, msg = selfmod.apply(arg)
        print(("OK: " if ok else "BLOCKED: ") + msg)
        return True

    if verb == "reject" and arg:
        ok, msg = selfmod.reject(arg)
        print(("OK: " if ok else "ERROR: ") + msg)
        return True

    return False


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
        if _handle_meta(user_message):
            continue
        print("Nervice: ", end="", flush=True)
        reply = await respond(USER, user_message, window)
        window.append({"role": "user", "content": user_message})
        window.append({"role": "assistant", "content": reply})
        window[:] = window[-12:]
        await save_exchange(USER, conversation_id, user_message, reply)


if __name__ == "__main__":
    asyncio.run(main())
