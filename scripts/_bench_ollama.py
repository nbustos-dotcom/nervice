"""Benchmark local Ollama candidates as a Nervice rung: first-token latency, tokens/sec, and
tool-calling reliability (the router + sysinfo/weather tools need dependable tool calls).
Runs with voice models resident (realistic VRAM). Read-only probe. Throwaway."""
import json, time, sys
import urllib.request

BASE = "http://127.0.0.1:11434"

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Nate's local weather right now. Use for ANY weather/temperature question.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_system_info",
        "description": "This computer's hardware + live stats (CPU, RAM, GPU, disk, OS). Use for any question about the machine.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current events/facts.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

# (prompt, expected_tool or None for direct answer)
TOOL_CASES = [
    ("what's the weather like today?", "get_weather"),
    ("how much RAM does this machine have?", "get_system_info"),
    ("what's the latest news about the election?", "web_search"),
    ("what's 2+2?", None),
    ("write me a haiku about coffee", None),
    ("what GPU do I have?", "get_system_info"),
]


def post(path, payload, timeout=120):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def bench(model):
    print(f"\n===== {model} =====", flush=True)
    # cold load time (model may need to load into VRAM)
    t0 = time.time()
    post("/api/chat", {"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "stream": False, "options": {"num_predict": 5}}).read()
    print(f"load+first-reply (cold): {time.time()-t0:.1f}s", flush=True)

    # first-token + tok/s on a normal chat turn (warm, streaming)
    t0 = time.time(); first = None; toks = 0
    with post("/api/chat", {"model": model, "stream": True,
                            "messages": [{"role": "user", "content":
                                          "In 3 short sentences, what makes a good morning routine?"}]}) as r:
        for line in r:
            d = json.loads(line)
            if d.get("message", {}).get("content"):
                if first is None:
                    first = time.time() - t0
                toks += 1
            if d.get("done"):
                total = time.time() - t0
                ev = d.get("eval_count", toks); ed = d.get("eval_duration", 1) / 1e9
                print(f"warm chat: first-token {first:.2f}s | {ev/ed:.1f} tok/s | total {total:.1f}s ({ev} toks)", flush=True)

    # tool-calling reliability
    ok = 0
    for prompt, want in TOOL_CASES:
        t0 = time.time()
        try:
            resp = json.loads(post("/api/chat", {
                "model": model, "stream": False, "tools": TOOLS,
                "messages": [{"role": "system", "content": "You are Nervice, Nate's assistant. Use a tool when one fits; answer directly when none does."},
                             {"role": "user", "content": prompt}]}).read())
            calls = resp.get("message", {}).get("tool_calls") or []
            got = calls[0]["function"]["name"] if calls else None
        except Exception as e:
            got = f"ERR {repr(e)[:40]}"
        hit = (got == want)
        ok += hit
        print(f"  tool[{'Y' if hit else 'N'}] {time.time()-t0:4.1f}s  want={str(want):16} got={got}  <- {prompt[:42]}", flush=True)
    print(f"tool-call accuracy: {ok}/{len(TOOL_CASES)}", flush=True)

    # VRAM while resident
    import subprocess
    vs = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader"],
                        capture_output=True, text=True).stdout.strip()
    print(f"VRAM with model resident: {vs}", flush=True)


for m in sys.argv[1:] or ["qwen3.5:4b"]:
    bench(m)
