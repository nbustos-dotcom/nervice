import sys, pathlib, asyncio, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.chat as chat
from app.router import classify


async def t1():
    print("=== t1: discussion of self-improvement -> normal (no tool) ===")
    route = await classify("what do you think could be improved about yourself?")
    print(f"  route = {route!r}  -> {'PASS' if route == 'normal' else 'FAIL'}")
    return route == "normal"


async def t2():
    print("=== t2: explicit self-change -> selfmod, WITH ack line first ===")
    route = await classify("stop ending sentences with questions")
    print(f"  route = {route!r}  -> {'selfmod ok' if route == 'selfmod' else 'ROUTE FAIL'}")

    # ack-order mechanics: ack must be spoken BEFORE the tool runs
    events = []

    async def fake_cwt(*a, **k):
        events.append("tool_ran")
        return "draft created"

    async def fake_sys(*a, **k):
        return "sys"

    orig_cwt, orig_sys, orig_classify = chat.chat_with_tools, chat.build_system_prompt, chat.classify
    chat.chat_with_tools = fake_cwt
    chat.build_system_prompt = fake_sys
    chat.classify = lambda m: _ret("selfmod")
    spoken = []
    try:
        reply = await chat.respond("nate", "stop ending sentences with questions", [],
                                   voice_mode=True, speak=lambda t: events.append(f"spoke:{t}"))
        spoke_first = events and events[0].startswith("spoke:") and "tool_ran" in events
        ack_ok = any(e == f"spoke:{chat._ACK['selfmod']}" for e in events)
        print(f"  event order = {events}")
        print(f"  ack spoken before tool: {spoke_first} | ack text correct: {ack_ok}")
    finally:
        chat.chat_with_tools, chat.build_system_prompt, chat.classify = orig_cwt, orig_sys, orig_classify
    return route == "selfmod" and spoke_first and ack_ok


def _ret(v):
    async def _c(*a, **k):
        return v
    return _c()


async def t3():
    print("=== t3: slow tool > timeout -> timeout message, loop alive ===")

    async def slow_cwt(*a, **k):
        await asyncio.sleep(5)          # longer than the (overridden) timeout
        return "should never return"

    async def fake_sys(*a, **k):
        return "sys"

    orig_cwt, orig_sys, orig_classify, orig_to = (
        chat.chat_with_tools, chat.build_system_prompt, chat.classify, dict(chat._TIMEOUTS))
    chat.chat_with_tools = slow_cwt
    chat.build_system_prompt = fake_sys
    chat.classify = lambda m: _ret("build")
    chat._TIMEOUTS["build"] = 1          # 1s timeout for the test
    try:
        t = time.time()
        reply = await chat.respond("nate", "build something huge", [], voice_mode=False)
        dt = time.time() - t
        print(f"  reply = {reply!r}  (in {dt:.1f}s)")
        ok = reply == chat._TIMEOUT_MSG and dt < 3
        print(f"  timeout message returned, loop alive: {'PASS' if ok else 'FAIL'}")
    finally:
        chat.chat_with_tools, chat.build_system_prompt, chat.classify = orig_cwt, orig_sys, orig_classify
        chat._TIMEOUTS.clear(); chat._TIMEOUTS.update(orig_to)
    return ok


async def main():
    r1 = await t1()
    r2 = await t2()
    r3 = await t3()
    print(f"\nRESULT: t1={'PASS' if r1 else 'FAIL'}  t2={'PASS' if r2 else 'FAIL'}  t3={'PASS' if r3 else 'FAIL'}")


asyncio.run(main())
