"""Hardened local-Ollama client — the free, unlimited capped-state rung (qwen3.5:4b).

ONE wrapper so the two mandatory configs can never leak to a call site (benchmarked on this box):
  think:false   — qwen3.5's default thinking burned 1,332 tokens = 22s of dead air per turn
  keep_alive:-1 — keeps the model resident (cold reload measured 28.6s); fits with voice
                  models at ~3.4GB VRAM headroom

Leaf module: stdlib + httpx only. Everything is local (http://127.0.0.1:11434) — $0, no network
beyond loopback. Raises OllamaUnavailable on down/timeout so callers can ladder onward loudly,
never hang or silently crash."""
import json

import httpx

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3.5:4b"
OLLAMA_DOWN_MSG = ("My local backup brain isn't running — start Ollama and I'll stay fast "
                   "even when I'm rate-limited.")

_CHAT_TIMEOUT = 45.0     # generation cap — a capped-state answer must never wedge a turn
_PROBE_TIMEOUT = 2.0


class OllamaUnavailable(Exception):
    """Ollama is down/unreachable/timed out — callers ladder to the next rung."""


async def is_up() -> bool:
    """Quick health probe: is the Ollama service answering on loopback?"""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as c:
            r = await c.get(f"{OLLAMA_URL}/api/version")
        return r.status_code == 200
    except Exception:
        return False


async def chat(messages: list[dict], tools: list[dict] | None = None,
               timeout: float = _CHAT_TIMEOUT) -> dict:
    """One non-streamed chat call with the mandatory configs ALWAYS baked in. Returns Ollama's
    message dict: {"role","content"[,"tool_calls"]}. Raises OllamaUnavailable on any transport
    failure so the ladder can advance."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,          # mandatory — see module docstring
        "keep_alive": -1,        # mandatory — stay resident
    }
    if tools:
        payload["tools"] = tools
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
        if r.status_code != 200:
            raise OllamaUnavailable(f"ollama http {r.status_code}: {r.text[:120]}")
        return r.json().get("message", {}) or {}
    except OllamaUnavailable:
        raise
    except Exception as e:
        raise OllamaUnavailable(f"ollama unreachable: {repr(e)[:120]}") from e


async def chat_json(system: str, user: str, timeout: float = 20.0) -> dict:
    """Strict-JSON completion (Ollama format=json) for classification. Raises OllamaUnavailable
    on transport failure; raises ValueError on unparseable output."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False, "keep_alive": -1,
        "format": "json",
        "options": {"temperature": 0},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
        if r.status_code != 200:
            raise OllamaUnavailable(f"ollama http {r.status_code}")
        content = (r.json().get("message", {}) or {}).get("content", "")
    except OllamaUnavailable:
        raise
    except Exception as e:
        raise OllamaUnavailable(f"ollama unreachable: {repr(e)[:120]}") from e
    return json.loads(content)
