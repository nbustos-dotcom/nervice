import asyncio, uuid, sys, re
from app.chat import respond, save_exchange, greeting
from app import selfmod

USER = "nate"

_PID_RE = re.compile(r"^\d{8}-")  # proposal-id shape, e.g. 20260609-...
_META_HELP = ("Commands (slash optional): proposals | show <id> | approve <id> | reject <id> | "
              "exit. Anything else is sent to Nervice.")


def _handle_meta(cmd: str) -> str:
    """Route meta commands before the LLM. Returns 'exit', 'handled', or 'pass'.
    Any input starting with '/' is meta and NEVER reaches the LLM (unknown slash -> help)."""
    raw = cmd.strip()
    is_slash = raw.startswith("/")
    body = raw[1:].strip() if is_slash else raw
    parts = body.split(maxsplit=1)
    verb = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if verb in ("exit", "quit"):
        return "exit"

    # For id commands, non-slash input is only treated as a command when the arg looks like a
    # proposal id — so natural language ("show me the news") isn't intercepted.
    id_ok = bool(arg) and (is_slash or _PID_RE.match(arg))

    if verb == "proposals" and (is_slash or not arg):
        pending = [p for p in selfmod.list_proposals() if p.get("status") == "pending"]
        if not pending:
            print("No pending proposals.")
        else:
            for p in pending:
                print(f"  {p['id']}  {p.get('paths')}  — {p.get('summary', '')[:100]}")
        return "handled"

    if verb == "show" and id_ok:
        rec = selfmod.get(arg)
        if rec is None:
            print(f"No proposal {arg}.")
            return "handled"
        print(f"id: {rec['id']}   status: {rec.get('status')}   paths: {rec.get('paths')}")
        print(f"instruction: {rec.get('instruction')}")
        print(f"summary: {rec.get('summary')}")
        if rec.get("reason"):
            print(f"reason: {rec.get('reason')}")
        patch = selfmod.PROPOSALS_DIR / f"{arg}.patch"
        if patch.exists():
            print("\n--- patch ---")
            print(patch.read_text(encoding="utf-8"))
        return "handled"

    if verb == "approve" and id_ok:
        ok, msg = selfmod.apply(arg)
        print(("OK: " if ok else "BLOCKED: ") + msg)
        return "handled"

    if verb == "reject" and id_ok:
        ok, msg = selfmod.reject(arg)
        print(("OK: " if ok else "ERROR: ") + msg)
        return "handled"

    if is_slash:
        print(_META_HELP)   # unknown slash command -> help, never sent to the LLM
        return "handled"
    return "pass"


def store_exchange(pending: set, user_id, conversation_id, user_message, reply):
    """Fire-and-forget persistence so the next prompt is available immediately. Errors are logged
    to stderr, never raised into the loop. Caller awaits `pending` on exit so nothing is lost."""
    async def _run():
        try:
            await save_exchange(user_id, conversation_id, user_message, reply)
            print("[stored]", file=sys.stderr)
        except Exception as e:
            print(f"[store failed] {repr(e)[:120]}", file=sys.stderr)
    t = asyncio.create_task(_run())
    pending.add(t)
    t.add_done_callback(pending.discard)


async def main():
    conversation_id = str(uuid.uuid4())
    window = []
    pending: set = set()
    print("Nervice is ready. Type '/exit' to leave.\n")
    try:
        g = await greeting(USER)
        if g:
            print(f"Nervice: {g}\n")
    except Exception as e:
        print(f"[greeting skipped] {repr(e)[:80]}", file=sys.stderr)
    try:
        while True:
            try:
                user_message = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nLater, Nate.")
                break
            if not user_message:
                continue
            meta = _handle_meta(user_message)
            if meta == "exit":
                print("Later, Nate.")
                break
            if meta == "handled":
                continue
            print("Nervice: ", end="", flush=True)
            reply = await respond(USER, user_message, window)
            window.append({"role": "user", "content": user_message})
            window.append({"role": "assistant", "content": reply})
            window[:] = window[-12:]
            store_exchange(pending, USER, conversation_id, user_message, reply)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
