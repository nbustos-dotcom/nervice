"""Wake-word loop tests (openWakeWord engine; mocked mic/LLM). Verifies: model loads from
models/wakeword/ + clean fallback message if absent; dormant->active fires the full turn; sleep
word -> dormant; missing model -> normal-loop fallback; junk gate in active; and that detection
needs NO API key / NO network. Throwaway."""
import sys, os, types, asyncio, io, contextlib, pathlib, tempfile, inspect
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()
import numpy as np

import wake

R = {}
_real_wait_for_wake = wake.wait_for_wake   # t2 monkeypatches it; t6 needs the real one back
def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- t1: init_wakeword loads from models/wakeword/ (real hey_jarvis.onnx) / None if absent ----
mw = wake.init_wakeword()
t1_load = mw is not None and mw[1] and mw[2].name.endswith(".onnx")
_orig_dir = wake._WAKE_DIR
wake._WAKE_DIR = pathlib.Path(tempfile.mkdtemp())   # empty -> no model
t1_absent = wake.init_wakeword() is None
wake._WAKE_DIR = _orig_dir
R["t1 model loads / clean None if absent"] = bool(t1_load and t1_absent)
print(f"t1: loaded={mw[1] if mw else None}  path={mw[2].name if mw else None}  absent->None={t1_absent}")
print("t1", "PASS" if R["t1 model loads / clean None if absent"] else "FAIL")

# shared mocks for the turn-pipeline tests
spoken = []
wake.speak = lambda t: spoken.append(t)
wake.store_exchange = lambda *a, **k: None

# ---- t2: dormant->active transition fires the full respond() turn (mock the detection event) ----
spoken.clear()
_woke = iter([True, None])                          # detect once, then exit the loop
wake.wait_for_wake = lambda model: next(_woke, None)
asked = {}
async def _resp(user, text, window, voice_mode=False, speak=None):
    asked["text"] = text; asked["voice"] = voice_mode; return "Paris."
wake.respond = _resp
_texts = iter(["what's the capital of france", ""])
wake._capture_text = lambda: next(_texts, "")
run(wake.wake_main(object(), "hey_jarvis"))         # model arg unused (wait_for_wake mocked)
R["t2 wake->active fires full turn"] = (asked.get("text") == "what's the capital of france"
                                        and asked.get("voice") is True and "Paris." in spoken
                                        and wake.WAKE_CUE in spoken)
print(f"t2: asked={asked.get('text')!r}  spoke Paris={'Paris.' in spoken}")
print("t2", "PASS" if R["t2 wake->active fires full turn"] else "FAIL")

# ---- t3: sleep phrase in active -> dormant, NO turn ----
spoken.clear(); called = {"r": False}
async def _rf(*a, **k): called["r"] = True; return "x"
wake.respond = _rf
_texts = iter(["go to sleep"]); wake._capture_text = lambda: next(_texts, "")
run(wake.active_session([], "cid", set()))
R["t3 sleep word -> dormant, no turn"] = (wake.SLEEP_ACK in spoken and not called["r"]
                                          and wake._is_sleep("go to sleep")
                                          and not wake._is_sleep("what's the news today"))
print(f"t3: ack={wake.SLEEP_ACK in spoken}  respond_called={called['r']}")
print("t3", "PASS" if R["t3 sleep word -> dormant, no turn"] else "FAIL")

# ---- t5: junk gate still applies in ACTIVE (no turn on junk) ----
spoken.clear(); called = {"r": False}
async def _rf2(*a, **k): called["r"] = True; return "x"
wake.respond = _rf2
_texts = iter(["uh", ""]); wake._capture_text = lambda: next(_texts, "")
run(wake.active_session([], "cid", set()))
R["t5 junk gate active in ACTIVE"] = (not called["r"] and any("catch" in s.lower() for s in spoken)
                                      and wake.is_junk_transcript("uh"))
print(f"t5: respond_called={called['r']}  nudged={any('catch' in s.lower() for s in spoken)}")
print("t5", "PASS" if R["t5 junk gate active in ACTIVE"] else "FAIL")

# ---- t4: missing model -> graceful message + normal-loop fallback (unaffected) ----
wake._WAKE_DIR = pathlib.Path(tempfile.mkdtemp())   # empty
fell = {"v": False}
async def _fb(): fell["v"] = True
wake._normal_voice_fallback = _fb
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    run(wake.main())
out = buf.getvalue()
wake._WAKE_DIR = _orig_dir
R["t4 missing -> message + fallback"] = (fell["v"] and "models/wakeword" in out and "--setup" in out)
print(f"t4: fellback={fell['v']}  msg_shown={'models/wakeword' in out}")
print("t4", "PASS" if R["t4 missing -> message + fallback"] else "FAIL")

# ---- t6: detection needs NO API key and NO network (local onnx predict on mic frames) ----
class _FakeStream:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n): return (np.zeros((n, 1), dtype=np.int16), False)
wake.sd.InputStream = lambda **k: _FakeStream()
_n = {"c": 0}
class _FakeModel:
    def reset(self): pass
    def predict(self, frame): _n["c"] += 1; return {"hey_jarvis": 0.9 if _n["c"] >= 3 else 0.0}
detected = _real_wait_for_wake(_FakeModel())          # purely local: mic frames -> model.predict
src = inspect.getsource(wake.init_wakeword) + inspect.getsource(wake.wait_for_wake)
no_key = ("PICOVOICE" not in src and "access_key" not in src.lower() and "api_key" not in src.lower()
          and "requests" not in src and "http" not in src.lower())
R["t6 no key / no network for detection"] = bool(detected is True and _n["c"] >= 3 and no_key)
print(f"t6: detected_locally={detected}  frames={_n['c']}  no_key_no_net={no_key}")
print("t6", "PASS" if R["t6 no key / no network for detection"] else "FAIL")

print("\nRESULT:")
for k, v in R.items():
    print(("  PASS " if v else "  FAIL ") + k)
ok = all(R.values())
print("ALL PASS" if ok else "SOME FAILED")
os._exit(0 if ok else 1)
