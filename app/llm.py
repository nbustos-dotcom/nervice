import app.net  # noqa

import os, json, sys, inspect
from groq import AsyncGroq, BadRequestError
from dotenv import load_dotenv

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
TOOL_MODEL = "openai/gpt-oss-120b"
VERIFY_MODEL = "llama-3.3-70b-versatile"   # different family from TOOL_MODEL — no self-grading

SYNTH_SYSTEM = """You answer the user's question using ONLY the source material provided.
HARD RULES:
- Every specific — product name, number, percentage, price, version, date, quote, deal direction, attribution — must appear in the source material. If it is not there, you do not say it.
- Never supplement from your own knowledge. The sources are the entire universe of facts.
- If the sources only partially answer, give the partial answer and say what is missing. A short grounded answer beats a complete invented one.
- Never fabricate quotes. Only quote text that appears verbatim in the sources.
- State what the sources state. Do not add interpretive conclusions, predictions, or "this hints/suggests/signals" framing the sources don't themselves draw.
- Voice: warm, direct, conversational — like a sharp coworker summarizing for a friend. Concise. No bullet-dump unless asked."""

VERIFY_SYSTEM = """You are a strict fact-check filter. You receive SOURCE MATERIAL and a DRAFT.
Step 1 — list every specific claim in the draft (names, numbers, quotes, dates, products, attributions). For each, find a verbatim supporting quote in the source material. Then check FIDELITY, not just presence:
- Same subject? (a quote or action attributed to X in the source must not be re-attached to Y)
- Same scope? (no added qualifiers the source doesn't state: "rolled out" vs announced, "first", "new", "all", "only")
- Same timeframe? (no dates attached to events the source doesn't date)
Mark UNSUPPORTED if the words exist but the referent, scope, or timing was shifted.
Step 2 — rewrite the draft: remove every UNSUPPORTED specific entirely or replace with honest vagueness ("reports suggest", "details unclear"). Never invent substitutes. Keep tone and flow.
Output format exactly:
CLAIMS:
<one line per claim: SUPPORTED "<evidence quote>" | UNSUPPORTED>
FINAL:
<the rewritten answer only>"""

_client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])


async def _grounded_synthesis(question: str, tool_outputs: list[str]) -> str:
    src = "\n\n=====\n\n".join(tool_outputs)
    # Pass 1: isolated synthesis — model sees ONLY question + sources
    resp = await _client.chat.completions.create(
        model=TOOL_MODEL,
        messages=[{"role": "system", "content": SYNTH_SYSTEM},
                  {"role": "user", "content": f"SOURCE MATERIAL:\n{src}\n\nQUESTION: {question}"}],
        temperature=0.2)
    draft = resp.choices[0].message.content
    # Pass 2: cross-model evidence-quoting verification
    resp = await _client.chat.completions.create(
        model=VERIFY_MODEL,
        messages=[{"role": "system", "content": VERIFY_SYSTEM},
                  {"role": "user", "content": f"SOURCE MATERIAL:\n{src}\n\nDRAFT:\n{draft}"}],
        temperature=0.0)
    out = resp.choices[0].message.content
    if "FINAL:" in out:
        return out.split("FINAL:", 1)[1].strip()
    return draft  # verifier format failure — fall back to draft


async def chat_stream(system: str, messages: list[dict]):
    stream = await _client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=0.6,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


async def chat_with_tools(system, messages, tools, tool_funcs, max_rounds=4, force_tool: str | None = None):
    msgs = [{"role": "system", "content": system}] + list(messages)
    tool_outputs: list[str] = []
    fired: set[str] = set()
    direct_answer = None
    for round_idx in range(max_rounds):
        tool_choice = ({"type": "function", "function": {"name": force_tool}}
                       if force_tool and round_idx == 0 else "auto")
        try:
            resp = await _client.chat.completions.create(
                model=TOOL_MODEL, messages=msgs, tools=tools, tool_choice=tool_choice, temperature=0.2)
        except BadRequestError as e:
            # gpt-oss can refuse a forced tool call and answer in text; Groq rejects that
            # generation as 400 tool_use_failed. The route is mechanical — run the tool ourselves.
            if not (force_tool and round_idx == 0 and "tool_use_failed" in str(e)):
                raise
            question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            fn = tool_funcs.get(force_tool)
            arg_name = next(iter(inspect.signature(fn).parameters))
            print(f"\n[TOOL CALL — synthesized after tool_use_failed] {force_tool}({arg_name}=<user message>)", file=sys.stderr)
            try:
                raw = fn(**{arg_name: question})
                result = await raw if inspect.isawaitable(raw) else raw
            except Exception as ex:
                result = f"tool error: {ex}"
            print(f"[TOOL RESULT first 600 chars]\n{str(result)[:600]}\n", file=sys.stderr)
            fired.add(force_tool)
            tool_outputs.append(str(result))
            msgs.append({"role": "assistant", "content": "",
                "tool_calls": [{"id": "forced_0", "type": "function",
                    "function": {"name": force_tool, "arguments": json.dumps({arg_name: question})}}]})
            msgs.append({"role": "tool", "tool_call_id": "forced_0", "content": str(result)})
            continue
        m = resp.choices[0].message
        if not m.tool_calls:
            if round_idx == 0:
                print("[NO TOOL CALLED — answered from parametric knowledge]", file=sys.stderr)
            direct_answer = m.content
            break
        msgs.append({"role": "assistant", "content": m.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in m.tool_calls]})
        for tc in m.tool_calls:
            print(f"\n[TOOL CALL] {tc.function.name}({tc.function.arguments})", file=sys.stderr)
            fn = tool_funcs.get(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
                raw = fn(**args) if fn else f"unknown tool {tc.function.name}"
                result = await raw if inspect.isawaitable(raw) else raw
            except Exception as e:
                result = f"tool error: {e}"
            print(f"[TOOL RESULT first 600 chars]\n{str(result)[:600]}\n", file=sys.stderr)
            fired.add(tc.function.name)
            tool_outputs.append(str(result))
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    # grounded synthesis is for web material only; consult_claude answers stay in the conversation
    if "web_search" in fired:
        question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return await _grounded_synthesis(question, tool_outputs)
    if direct_answer is None and tool_outputs:
        resp = await _client.chat.completions.create(model=TOOL_MODEL, messages=msgs, temperature=0.2)
        return resp.choices[0].message.content
    return direct_answer


async def chat_json(system: str, user: str) -> dict:
    resp = await _client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return json.loads(resp.choices[0].message.content)
