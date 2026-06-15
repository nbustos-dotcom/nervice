"""Deterministic observability checks (no network, no LLM): the central error log + secret scrub,
and the /stats/history aggregation parse (numbers verified by hand). The live /chat fail-soft, the
secret-scrub-in-context proof, and the real endpoints are exercised by the adversarial test.
Run: .venv/Scripts/python.exe scripts/test_observability.py"""
import os, sys, json, tempfile, datetime, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
# Seed fake secret VALUES so scrub has env values to catch (kept distinct from the real .env ones).
os.environ["GROQ_API_KEY"] = "gsk_FAKE_envvalue_DEADBEEF1234567890"
os.environ["NERVICE_API_TOKEN"] = "fake_api_token_ABCDEFGHIJKLMNOP"

from app import errorlog

P = F = 0
def chk(d, c):
    global P, F
    print(f"  {'PASS' if c else 'FAIL'} {d}")
    P += bool(c); F += (not c)

print("[scrub] secrets + tokens redacted, normal text untouched")
s1 = errorlog.scrub("my groq key is gsk_FAKE_envvalue_DEADBEEF1234567890 right here")
chk("env GROQ_API_KEY value -> [scrubbed:GROQ_API_KEY]",
    "gsk_FAKE_envvalue_DEADBEEF1234567890" not in s1 and "[scrubbed:GROQ_API_KEY]" in s1)
chk("env NERVICE_API_TOKEN value scrubbed",
    "fake_api_token_ABCDEFGHIJKLMNOP" not in errorlog.scrub("token fake_api_token_ABCDEFGHIJKLMNOP leaked"))
chk("provider-prefixed token sk-... scrubbed",
    "sk-ABCD1234EFGH5678IJKLmnop" not in errorlog.scrub("here is sk-ABCD1234EFGH5678IJKLmnop a key"))
chk("Bearer header scrubbed",
    "abc123XYZ.def456-ghi789" not in errorlog.scrub("Authorization: Bearer abc123XYZ.def456-ghi789"))
chk("long high-entropy run scrubbed", ("a1b2c3d4" * 5) not in errorlog.scrub("checksum " + "a1b2c3d4" * 5))
chk("normal text untouched", errorlog.scrub("what's my CPU and how much RAM?") == "what's my CPU and how much RAM?")
chk("empty/None safe", errorlog.scrub("") == "" and errorlog.scrub(None) == "")

print("[log_error + read_recent] newest-first, scrubbed, no double-log, malformed skipped")
tmp = pathlib.Path(tempfile.mkdtemp())
errorlog._LOG = tmp / "errors.log"
errorlog.log_error("recall:embed", ConnectionError("ollama down sk-LEAK1234567890abcd"),
                   context="user said gsk_FAKE_envvalue_DEADBEEF1234567890")
errorlog.log_error("route:canvas", ValueError("bad selector"), context="what's due this week")
rec = errorlog.read_recent(10)
chk("read_recent returns 2 entries", len(rec) == 2)
chk("newest first (route:canvas on top)", rec[0]["where"] == "route:canvas")
chk("exception type captured", rec[1]["type"] == "ConnectionError")
chk("message scrubbed (no sk- token)", "sk-LEAK1234567890abcd" not in json.dumps(rec[1]))
chk("context scrubbed (no env value)", "gsk_FAKE_envvalue_DEADBEEF1234567890" not in json.dumps(rec[1]))
chk("traceback captured for an exception", "traceback" in rec[1])
e = RuntimeError("once")
errorlog.log_error("a", e); errorlog.log_error("b", e)             # same object twice
chk("same exception logged once (marker)",
    sum(1 for r in errorlog.read_recent(10) if r.get("message") == "once") == 1)
with open(errorlog._LOG, "a", encoding="utf-8") as f:
    f.write("this is not json\n")
chk("malformed line skipped by read_recent", all("message" in r for r in errorlog.read_recent(20)))

print("[count_since] time-window counting")
past = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat(timespec="seconds")
future = (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
chk("count_since(past) counts all valid entries", errorlog.count_since(past) >= 3)
chk("count_since(future) == 0", errorlog.count_since(future) == 0)

print("[trim] a huge log self-trims to <= _MAX_BYTES, still readable")
big = tmp / "big.log"; errorlog._LOG = big
big.write_text(("x" * 100 + "\n") * 30000, encoding="utf-8")       # ~3 MB
errorlog.log_error("route:test", ValueError("trigger trim"))
chk(f"errors.log trimmed to <= {errorlog._MAX_BYTES}", big.stat().st_size <= errorlog._MAX_BYTES)
chk("read_recent still works after trim", len(errorlog.read_recent(1)) == 1)

print("[empty/missing] no crash, honest zeros")
errorlog._LOG = tmp / "nope.log"
chk("read_recent([]) on missing file", errorlog.read_recent(5) == [])
chk("count_since==0 on missing file", errorlog.count_since(past) == 0)

print("[/stats/history _history] window math + aggregates verified BY HAND")
import app.api as api
def tline(dt, route, rung, secs):
    ts = dt.strftime("%Y-%m-%d %H:%M:%S,") + f"{dt.microsecond // 1000:03d}"
    return f"{ts}\troute={route}\trung={rung}\t{secs:.2f}s\tpath=rest"
now = datetime.datetime.now()
turns = tmp / "turns.log"
turns.write_text("\n".join([
    tline(now - datetime.timedelta(minutes=10), "normal", "groq", 0.40),
    tline(now - datetime.timedelta(minutes=20), "normal", "groq", 0.80),
    tline(now - datetime.timedelta(minutes=30), "news", "ollama", 2.00),
    tline(now - datetime.timedelta(hours=5),    "normal", "groq", 9.99),   # OUT of a 2h window
    "GUARD-TRIP a non-turn / malformed line",                              # skipped
]) + "\n", encoding="utf-8")
api._TURNS_LOG = turns
errorlog._LOG = tmp / "errs.log"
errorlog.log_error("recall:embed", ConnectionError("x"))               # 1 error, in-window
h = api._history(2.0)
print("    ->", json.dumps(h))
chk("turns in 2h window == 3 (5h-ago + malformed excluded)", h["turns"] == 3)
chk("rungs == {groq:2, ollama:1}", h["rungs"] == {"groq": 2, "ollama": 1})
chk("routes == {normal:2, news:1}", h["routes"] == {"normal": 2, "news": 1})
chk("avg == (0.40+0.80+2.00)/3 == 1.07", h["latency"]["avg_s"] == round((0.40 + 0.80 + 2.00) / 3, 2))
chk("p95 == 2.00 (nearest-rank of the 3)", h["latency"]["p95_s"] == 2.00)
chk("max == 2.00", h["latency"]["max_s"] == 2.00)
chk("errors == 1 (in-window, joined from errors.log)", h["errors"] == 1)
empty = tmp / "empty.log"; empty.write_text("", encoding="utf-8")
api._TURNS_LOG = empty
h0 = api._history(24.0)
chk("empty turns.log -> honest zeros", h0["turns"] == 0 and h0["latency"]["p95_s"] == 0.0 and h0["rungs"] == {})

print(f"\n=== OBSERVABILITY (deterministic) {P} passed, {F} failed ===")
sys.exit(1 if F else 0)
