"""Wake-word loop tests (mocked — no real mic/Porcupine/LLM). Verifies: Porcupine load path + the
clean missing-key/file message, dormant->active fires the full turn, sleep word -> dormant, junk
gate still applies in active, and the graceful fallback. Throwaway."""
import sys, os, types, asyncio, io, contextlib, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import wake

R = {}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- t1: init_porcupine — missing key -> None+log; key+ppn present -> loads with the .ppn path ----
os.environ.pop("PICOVOICE_ACCESS_KEY", None)
t1_missing = wake.init_porcupine() is None                 # clean None when unconfigured

os.environ["PICOVOICE_ACCESS_KEY"] = "fake-key"
wake._PPN.parent.mkdir(parents=True, exist_ok=True)
_had_ppn = wake._PPN.exists()
if not _had_ppn:
    wake._PPN.write_text("placeholder")                    # so .exists() is True for the load path
cap = {}
def _fake_create(access_key, keyword_paths):
    cap["key"] = access_key; cap["paths"] = list(keyword_paths)
    return types.SimpleNamespace(frame_length=512, process=lambda f: -1, delete=lambda: None)
class _FakeRec:
    def __init__(self, frame_length): cap["fl"] = frame_length
    def delete(self): pass
sys.modules["pvporcupine"] = types.SimpleNamespace(create=_fake_create)
sys.modules["pvrecorder"] = types.SimpleNamespace(PvRecorder=_FakeRec)
pr = wake.init_porcupine()
t1_load = (pr is not None and cap.get("paths") == [str(wake._PPN)] and cap.get("fl") == 512)
if not _had_ppn:
    wake._PPN.unlink()                                     # remove placeholder
R["t1 loads custom ppn / clean missing msg"] = bool(t1_missing and t1_load)
print(f"t1: missing->None={t1_missing}  load paths={cap.get('paths')}")
print("t1", "PASS" if R["t1 loads custom ppn / clean missing msg"] else "FAIL")

# ---- t2: dormant->active runs the FULL turn pipeline (respond) ----
spoken = []
wake.speak = lambda t: spoken.append(t)
wake.store_exchange = lambda *a, **k: None
asked = {}
async def _resp(user, text, window, voice_mode=False, speak=None):
    asked["text"] = text; asked["voice"] = voice_mode; return "Paris."
wake.respond = _resp
_texts = iter(["what's the capital of france", ""])         # one turn, then quiet -> dormant
wake._capture_text = lambda: next(_texts, "")
win = []
run(wake.active_session(win, "cid", set()))
R["t2 wake->active fires full turn"] = (asked.get("text") == "what's the capital of france"
                                        and asked.get("voice") is True and "Paris." in spoken
                                        and wake.WAKE_CUE in spoken and len(win) == 2)
print(f"t2: asked={asked.get('text')!r}  spoke Paris={'Paris.' in spoken}  window={len(win)}")
print("t2", "PASS" if R["t2 wake->active fires full turn"] else "FAIL")

# ---- t3: a sleep phrase in active -> back to dormant, NO turn ----
spoken.clear(); called = {"respond": False}
async def _resp_flag(*a, **k): called["respond"] = True; return "x"
wake.respond = _resp_flag
_texts = iter(["go to sleep"])
wake._capture_text = lambda: next(_texts, "")
run(wake.active_session([], "cid", set()))
R["t3 sleep word -> dormant, no turn"] = (wake.SLEEP_ACK in spoken and not called["respond"]
                                          and wake._is_sleep("go to sleep")
                                          and not wake._is_sleep("what's the news today"))
print(f"t3: ack={wake.SLEEP_ACK in spoken}  respond_called={called['respond']}")
print("t3", "PASS" if R["t3 sleep word -> dormant, no turn"] else "FAIL")

# ---- t5: junk gate still applies in ACTIVE (no turn on a junk transcript) ----
spoken.clear(); called = {"respond": False}
async def _resp_flag2(*a, **k): called["respond"] = True; return "x"
wake.respond = _resp_flag2
_texts = iter(["uh", ""])                                   # junk, then quiet -> dormant
wake._capture_text = lambda: next(_texts, "")
run(wake.active_session([], "cid", set()))
R["t5 junk gate active in ACTIVE"] = (not called["respond"]
                                      and any("catch" in s.lower() for s in spoken)
                                      and wake.is_junk_transcript("uh"))
print(f"t5: respond_called={called['respond']}  nudged={any('catch' in s.lower() for s in spoken)}")
print("t5", "PASS" if R["t5 junk gate active in ACTIVE"] else "FAIL")

# ---- t4: missing key -> graceful specific message + normal REPL fallback (unaffected) ----
os.environ.pop("PICOVOICE_ACCESS_KEY", None)
fell = {"v": False}
async def _fb(): fell["v"] = True
wake._normal_voice_fallback = _fb
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    run(wake.main())
out = buf.getvalue()
R["t4 missing -> message + fallback"] = (fell["v"] and "models/wakeword" in out
                                         and "PICOVOICE_ACCESS_KEY" in out)
print(f"t4: fellback={fell['v']}  msg_shown={'models/wakeword' in out}")
print("t4", "PASS" if R["t4 missing -> message + fallback"] else "FAIL")

print("\nRESULT:")
for k, v in R.items():
    print(("  PASS " if v else "  FAIL ") + k)
ok = all(R.values())
print("ALL PASS" if ok else "SOME FAILED")
os._exit(0 if ok else 1)
