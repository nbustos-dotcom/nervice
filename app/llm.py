import app.net  # noqa

import os, re, json, sys, asyncio, inspect
from groq import AsyncGroq, BadRequestError, RateLimitError
from dotenv import load_dotenv

from app.agent import ask_claude, AllClaudeExhausted, current_rung   # the Claude account ladder (Pro -> Max)
from app import ollama_client as ollama                 # the local qwen3.5:4b rung (free, unlimited)

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
TOOL_MODEL = "openai/gpt-oss-120b"
VERIFY_MODEL = "llama-3.3-70b-versatile"   # different family from TOOL_MODEL — no self-grading


def rate_limit_message(err=None) -> str:
    """Friendly, speakable reply for a Groq 429 (free-tier daily token cap). Never raises; the
    callers below return this instead of crashing so the phone speaks it rather than going silent.
    Pulls the 'try again in Xm' window out of the error when Groq provides it."""
    when = "in a bit"
    m = re.search(r"try again in ([0-9hms.]+)", str(err or ""))
    if m:
        hm = re.match(r"(?:(\d+)h)?(?:(\d+)m)?", m.group(1))
        hours = int(hm.group(1)) if hm and hm.group(1) else 0
        mins = int(hm.group(2)) if hm and hm.group(2) else 0
        parts = []
        if hours:
            parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
        if mins:
            parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
        if parts:
            when = "in about " + " and ".join(parts)
        elif m.group(1):
            when = "in under a minute"
    return ("I've hit my daily free usage limit on the fast model — it resets "
            f"{when}. Try me again soon.")


def _is_rate_limit(e: Exception) -> bool:
    return isinstance(e, RateLimitError) or "rate_limit" in str(e).lower()


# Shown only when the WHOLE ladder is exhausted — Groq capped AND every Claude account unavailable.
LADDER_EXHAUSTED_MSG = ("I've hit my usage limits for now — they reset shortly. "
                        "Try me again in a bit.")

# ---- capped news = extractive, zero LLM (Phase 0.3) ----
# When Groq is capped, a news question is answered straight from the fetched REAL headlines —
# no model paraphrase, so zero fabrication risk and zero token cost. With budget, the normal
# grounded synthesis path (search -> synth -> cross-verify) is unchanged.
_NEWS_RE = re.compile(r"\b(news|headlines?|what'?s\s+(?:happening|going\s+on)(?:\s+(?:in\s+the\s+world|today|out\s+there))?\s*\??$|catch\s+me\s+up)\b", re.I)


def is_news_question(text: str) -> bool:
    return bool(_NEWS_RE.search(text or ""))


async def extractive_news() -> str | None:
    """Real headlines verbatim from the cached BBC fetch, formatted for speech. None if the fetch
    has nothing (caller proceeds to its normal fallback)."""
    try:
        from app.api import _fetch_news   # lazy: api imports this module at startup (same pattern as skills)
        items = await _fetch_news()
    except Exception:
        items = []
    if not items:
        return None
    tops = [i["title"].rstrip(".") for i in items[:4]]
    body = ". ".join(f"{n}) {t}" for n, t in enumerate(tops, 1))
    return (f"I'm rate-limited, so here are the headlines straight from BBC News: {body}. "
            "Want me to dig into one when I'm back to full power?")


# Tools safe on the LOCAL rung: local-only, free, instant. Grounded synthesis stays on Claude when
# capped (a 4B must never paraphrase web sources — council mandate), and the heavy agent tools
# (consult/build/browse/selfmod) never run on a 4B.
_OLLAMA_TOOL_NAMES = {"get_weather", "get_system_info", "get_top_processes", "count_files"}


async def _ollama_fallback(system: str | None, messages: list,
                           tool_specs: list | None = None, tool_funcs: dict | None = None) -> str | None:
    """Groq is capped: answer on the local qwen3.5:4b rung with the SAME system prompt (persona +
    memories + safety floor) and conversation window the Groq path had, plus the local-only tool
    subset — so weather/machine-control questions keep working when capped. Returns the answer, or
    None when Ollama is unavailable/empty (the caller ladders on to Claude)."""
    try:
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        specs = [t for t in (tool_specs or []) if t.get("function", {}).get("name") in _OLLAMA_TOOL_NAMES]
        funcs = {k: v for k, v in (tool_funcs or {}).items() if k in _OLLAMA_TOOL_NAMES}
        for _ in range(3):                                   # small tool loop, hard-capped
            m = await ollama.chat(msgs, tools=specs or None)
            calls = m.get("tool_calls") or []
            if not calls:
                out = (m.get("content") or "").strip()
                if out:
                    current_rung.set("ollama")               # telemetry: this turn answered locally
                    print("[groq 429 -> answered on the local ollama rung]", file=sys.stderr)
                return out or None
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for tc in calls:
                name = tc.get("function", {}).get("name", "")
                args = tc.get("function", {}).get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except Exception:
                        args = {}
                fn = funcs.get(name)
                try:
                    raw = fn(**args) if fn else f"tool {name} not available on the local rung"
                    result = await raw if inspect.isawaitable(raw) else raw
                except Exception as e:
                    result = f"tool error: {e}"
                # both name keys for Ollama API version compatibility
                msgs.append({"role": "tool", "tool_name": name, "name": name, "content": str(result)})
        return None                                          # tool loop didn't converge
    except ollama.OllamaUnavailable as e:
        print(f"[ollama rung unavailable] {e}", file=sys.stderr)
        return None
    except Exception as e:
        # ANY failure on the local rung ladders onward — a capped turn must never crash here.
        print(f"[ollama rung error -> laddering on] {repr(e)[:120]}", file=sys.stderr)
        return None


async def _exhausted_msg() -> str:
    """The friendly limit message — with the actionable 'start Ollama' hint when the local rung
    being down is part of why we're here."""
    if not await ollama.is_up():
        return LADDER_EXHAUSTED_MSG + " " + ollama.OLLAMA_DOWN_MSG
    return LADDER_EXHAUSTED_MSG


async def _claude_fallback(prompt: str | None = None, system: str | None = None,
                           messages: list | None = None) -> str | None:
    """Groq is capped on a turn that would normally use it — answer via Claude (the Pro -> Max
    account ladder) instead of failing. Pass `messages` (the window + current user turn) so Claude
    gets the SAME conversation context Groq would have, threaded by compose_claude_prompt — this is
    what prevents mid-conversation amnesia. `prompt` alone (no messages) is for the grounded path,
    which sends a self-contained sources+question string and must NOT thread the chat window.
    Returns Claude's answer, or None if Claude is ALSO unavailable (caller shows the limit message)."""
    try:
        return await ask_claude(prompt or "", system=system, messages=messages)
    except Exception as e:   # AllClaudeExhausted, or any SDK/auth error
        print(f"[groq->claude fallback unavailable] {repr(e)[:120]}", file=sys.stderr)
        return None

# Per-tool ceilings for the slow agent tools, applied wherever they fire: respond() uses them for
# forced routes, and chat_with_tools applies them when the tool model calls one on its own — the
# previously unbounded path that could wedge an API turn (and the phone UI) indefinitely.
TOOL_TIMEOUTS = {"consult_claude": 180, "agent_build": 600, "browse": 120, "propose_self_update": 300}
TIMEOUT_MSG = "That took too long and I stopped it — want me to try again?"
_TERMINAL_TOOLS = ("agent_build", "propose_self_update", "browse")  # their result IS the reply

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

# Appended to the synthesis/verify prompts on voice turns only — the main VOICE_ADDENDUM lives in
# the chat system prompt, which grounded synthesis never sees (it builds isolated messages).
VOICE_SYNTH_ADDENDUM = ("\n\nVOICE MODE: this answer will be spoken aloud. Two to four flowing "
                        "conversational sentences MAX — pick only the few most important facts and "
                        "drop the rest. Never bullets, lists, or headers. Lead with the answer.")
VOICE_VERIFY_ADDENDUM = ("\n\nVOICE MODE: the FINAL answer will be spoken aloud — keep it to 2-4 "
                         "conversational sentences, no lists. Tighten, never pad.")


def _effort(voice_mode: bool) -> dict:
    """Voice turns run gpt-oss at low reasoning effort — measured ~0.4s faster per call with no
    quality cliff on conversational/synthesis work. Text turns keep the default. gpt-oss only;
    the llama models reject the param."""
    return {"reasoning_effort": "low"} if voice_mode else {}


async def _grounded_synthesis(question: str, tool_outputs: list[str], voice_mode: bool = False) -> str:
    src = "\n\n=====\n\n".join(tool_outputs)
    synth_system = SYNTH_SYSTEM + (VOICE_SYNTH_ADDENDUM if voice_mode else "")
    verify_system = VERIFY_SYSTEM + (VOICE_VERIFY_ADDENDUM if voice_mode else "")
    try:
        # Pass 1: isolated synthesis — model sees ONLY question + sources
        resp = await _client.chat.completions.create(
            model=TOOL_MODEL,
            messages=[{"role": "system", "content": synth_system},
                      {"role": "user", "content": f"SOURCE MATERIAL:\n{src}\n\nQUESTION: {question}"}],
            temperature=0.2, **_effort(voice_mode))
        draft = resp.choices[0].message.content
        # Pass 2: cross-model evidence-quoting verification
        resp = await _client.chat.completions.create(
            model=VERIFY_MODEL,
            messages=[{"role": "system", "content": verify_system},
                      {"role": "user", "content": f"SOURCE MATERIAL:\n{src}\n\nDRAFT:\n{draft}"}],
            temperature=0.0)
    except RateLimitError as e:
        # Cap hit between search and synthesis. News: extractive real headlines, zero LLM.
        # (Grounded synthesis deliberately does NOT run on the 4B — Claude only.)
        if is_news_question(question):
            news = await extractive_news()
            if news:
                print("[groq 429 in synthesis -> extractive news]", file=sys.stderr)
                current_rung.set("extractive")
                return news
        # Otherwise ground the answer with CLAUDE instead, from the SAME sources under the SAME
        # hard rules (answer only from sources) — the grounding boundary holds. The cross-model
        # verifier is skipped because Groq is down; we never emit the raw unverified Groq draft.
        print("[groq 429 in grounded synthesis -> Claude fallback]", file=sys.stderr)
        prompt = f"SOURCE MATERIAL:\n{src}\n\nQUESTION: {question}"
        ans = await _claude_fallback(prompt, system=synth_system)
        return ans if ans else LADDER_EXHAUSTED_MSG
    out = resp.choices[0].message.content
    if "FINAL:" in out:
        return out.split("FINAL:", 1)[1].strip()
    return draft  # verifier format failure — fall back to draft


async def chat_stream(system: str, messages: list[dict]):
    try:
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
    except RateLimitError as e:
        # e.g. the startup greeting when the cap is already hit — speak the limit, don't crash
        print("[groq 429 in chat_stream]", file=sys.stderr)
        yield rate_limit_message(e)


async def chat_with_tools(system, messages, tools, tool_funcs, max_rounds=4,
                          force_tool: str | None = None, voice_mode: bool = False):
    """Tier-ladder guard over the Groq tool loop. On a Groq daily-cap 429 anywhere in the loop,
    fall back to Claude (the Pro -> Max account ladder) rather than failing — cheapest-capable
    Groq first, Claude when Groq is out. If a FORCED Claude tool (the hard route) finds every
    account exhausted, return the friendly limit message. Nothing here 500s on a rate limit."""
    # Publish this turn's context so the consult TOOL (invoked generically by the tool model) threads
    # the same system + window as everything else. Reset in finally so it never leaks across turns.
    from app.agent import claude_turn_ctx
    token = claude_turn_ctx.set({"system": system, "messages": list(messages), "voice_mode": voice_mode})
    try:
        return await _chat_with_tools_impl(system, messages, tools, tool_funcs, max_rounds,
                                           force_tool, voice_mode)
    except RateLimitError:
        # Groq is capped. Ladder: extractive news (zero LLM) -> local Ollama (free, fast, with the
        # local tool subset) -> Claude (scarce, context-threaded) -> friendly message (+hint).
        question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if is_news_question(question):
            news = await extractive_news()
            if news:
                print("[groq 429 -> extractive news, zero LLM]", file=sys.stderr)
                current_rung.set("extractive")
                return news
        ans = await _ollama_fallback(system, messages, tools, tool_funcs)
        if ans:
            return ans
        print("[groq 429, ollama unavailable -> Claude fallback]", file=sys.stderr)
        ans = await _claude_fallback(system=system, messages=messages)
        return ans if ans else await _exhausted_msg()
    except AllClaudeExhausted:
        # the hard route forced Claude and every account is out — friendly, never a crash
        print("[claude ladder exhausted in chat_with_tools]", file=sys.stderr)
        return LADDER_EXHAUSTED_MSG
    finally:
        claude_turn_ctx.reset(token)


async def _chat_with_tools_impl(system, messages, tools, tool_funcs, max_rounds=4,
                                force_tool: str | None = None, voice_mode: bool = False):
    msgs = [{"role": "system", "content": system}] + list(messages)
    tool_outputs: list[str] = []
    fired: set[str] = set()
    direct_answer = None
    for round_idx in range(max_rounds):
        tool_choice = ({"type": "function", "function": {"name": force_tool}}
                       if force_tool and round_idx == 0 else "auto")
        try:
            resp = await _client.chat.completions.create(
                model=TOOL_MODEL, messages=msgs, tools=tools, tool_choice=tool_choice,
                temperature=0.2, **_effort(voice_mode))
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
            except AllClaudeExhausted:
                raise   # let the wrapper return the friendly limit message
            except Exception as ex:
                result = f"tool error: {ex}"
            print(f"[TOOL RESULT first 600 chars]\n{str(result)[:600]}\n", file=sys.stderr)
            # builds are terminal: the builder's summary IS the answer
            if force_tool == "agent_build":
                return "Build complete — here's what the builder did:\n\n" + str(result)
            # self-update proposals are terminal: return the gate's verbatim id/approve-reject text
            if force_tool == "propose_self_update":
                return str(result)
            # browse is terminal: the browser agent's report returns verbatim, no chat rewrite
            if force_tool == "browse":
                return str(result)
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
                if inspect.isawaitable(raw):
                    limit = TOOL_TIMEOUTS.get(tc.function.name)
                    result = await (asyncio.wait_for(raw, limit) if limit else raw)
                else:
                    result = raw
            except (asyncio.TimeoutError, TimeoutError):
                # auto-fired slow tool hit its ceiling — never wedge the turn (phone hang bug)
                print(f"[TOOL TIMEOUT] {tc.function.name} exceeded "
                      f"{TOOL_TIMEOUTS.get(tc.function.name)}s — cancelled", file=sys.stderr)
                if tc.function.name in _TERMINAL_TOOLS:
                    return TIMEOUT_MSG   # terminal tools: the reply IS the timeout notice
                result = (f"tool timed out after {TOOL_TIMEOUTS.get(tc.function.name)}s and was "
                          "stopped — answer from what you have and say the expert call timed out")
            except AllClaudeExhausted:
                raise   # auto-fired consult found every Claude account out — wrapper handles it
            except Exception as e:
                result = f"tool error: {e}"
            print(f"[TOOL RESULT first 600 chars]\n{str(result)[:600]}\n", file=sys.stderr)
            # builds are terminal: the builder's summary IS the answer — no further rounds
            if tc.function.name == "agent_build":
                return "Build complete — here's what the builder did:\n\n" + str(result)
            # self-update proposals are terminal: return the gate's verbatim id/approve-reject text
            if tc.function.name == "propose_self_update":
                return str(result)
            # browse is terminal: the browser agent's report returns verbatim
            if tc.function.name == "browse":
                return str(result)
            fired.add(tc.function.name)
            tool_outputs.append(str(result))
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
        # Once web material is in, the answer comes from grounded synthesis below — asking the
        # tool model for another completion first just generates a full draft that gets discarded
        # (measured 5.2s wasted per grounded turn). Tradeoff: the model no longer refines with a
        # second search; SYNTH_SYSTEM already handles thin sources by answering partially.
        if "web_search" in fired or "get_news" in fired:
            break
    # grounded synthesis is for web material only (web_search + get_news); consult_claude answers
    # stay in the conversation. Grounding here is what keeps news headlines REAL — no fabrication.
    if "web_search" in fired or "get_news" in fired:
        question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return await _grounded_synthesis(question, tool_outputs, voice_mode=voice_mode)
    if direct_answer is None and tool_outputs:
        resp = await _client.chat.completions.create(model=TOOL_MODEL, messages=msgs,
                                                     temperature=0.2, **_effort(voice_mode))
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
