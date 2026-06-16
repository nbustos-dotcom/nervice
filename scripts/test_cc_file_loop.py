"""Deterministic file-channel loop checks against a REAL toy git workspace. No LLM on the file path
(result.json is structured JSON + read-only git -> rung=direct, never Claude). _gen_step_prompt and
_read_doc are stubbed so the advance step needs no Groq; STATE_FILE is redirected to a temp file so the
real orchestrator state is untouched. The live brain + router are exercised by the adversarial /chat run.
Run: .venv/Scripts/python.exe scripts/test_cc_file_loop.py"""
import os, sys, json, asyncio, tempfile, subprocess, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
import app.orchestrator as orch

P = F = 0
def chk(d, c):
    global P, F
    print(f"  {'PASS' if c else 'FAIL'} {d}")
    P += bool(c); F += (not c)

def git(ws, *a):
    return subprocess.run(["git", "-C", str(ws), *a], capture_output=True, text=True)

def mkrepo():
    ws = pathlib.Path(tempfile.mkdtemp(prefix="ccloop-"))
    git(ws, "init", "-q"); git(ws, "config", "user.email", "t@t"); git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("toy", encoding="utf-8")
    git(ws, "add", "-A"); git(ws, "commit", "-q", "-m", "init")
    return ws

# redirect state to a temp file; stub the Groq-using bits so the file path is fully deterministic
orch.STATE_FILE = pathlib.Path(tempfile.mkdtemp(prefix="ccstate-")) / "orchestrator_state.json"
orch._read_doc = lambda: "GOAL: a toy project"
async def _stub_gen(doc, steps, idx, lead=""):
    return f"[generated prompt for step {idx + 1}]" + orch._write_prompt_file(idx, f"STUB step {idx + 1}", steps[idx])
orch._gen_step_prompt = _stub_gen

STEPS = [{"title": "Step one", "detail": "d1", "status": "pending"},
         {"title": "Step two", "detail": "d2", "status": "pending"}]
def set_state(ws, current=0, last_head=None):
    st = {"steps": [dict(s) for s in STEPS], "current": current, "workspace": str(ws)}
    if last_head is not None:
        st["last_head"] = last_head
    orch._save_state(st)

async def main():
    print("[_is_check_result] file-check vs pasted report")
    chk("'check the result' -> file", orch._is_check_result("check the result"))
    chk("'read the result file' -> file", orch._is_check_result("read the result file"))
    chk("'did claude code finish' -> file", orch._is_check_result("did claude code finish?"))
    chk("long pasted report -> NOT file", not orch._is_check_result(
        "here's what claude code said: i created foo.py and bar.py, all tests pass, summary done, committed it all"))
    chk("'what's the plan' -> NOT file", not orch._is_check_result("what's the plan"))

    print("[_op_workspace] set / show / bogus (no guessing)")
    ws = mkrepo()
    r = await orch._op_workspace(False, f"set the project workspace to {ws}")
    chk("set ok + persisted", ("workspace set to" in r.lower()) and orch._load_state().get("workspace") == str(ws))
    chk("set is rung=direct (no Claude)", orch.current_rung.get() == "direct")
    r = await orch._op_workspace(False, "where's the workspace")
    chk("show reports path + git-repo", str(ws) in r and "git repo" in r.lower())
    r = await orch._op_workspace(False, r"set the project workspace to C:\no\such\dir__xyz")
    chk("bogus path -> honest can't-find (no crash)", "can't find" in r.lower())

    print("[_write_prompt_file] writes .nervice/prompt.md + convention + baseline HEAD")
    set_state(ws, current=0)
    note = orch._write_prompt_file(0, "do step one", STEPS[0])
    pf = ws / ".nervice" / "prompt.md"
    body = pf.read_text(encoding="utf-8")
    chk("prompt.md written with the step + CC convention", pf.exists() and "do step one" in body and "result.json" in body and "commit" in body.lower())
    chk("last_head baseline recorded", orch._load_state().get("last_head") == orch._git_head(ws))

    print("[OK + matching commit] git cross-check PASSES -> verified -> 'yes' advances + writes step 2")
    (ws / ".nervice" / "result.json").write_text(json.dumps(
        {"status": "ok", "files_changed": ["a.py"], "tests": "passed", "summary": "built step one"}), encoding="utf-8")
    (ws / "a.py").write_text("print('one')", encoding="utf-8"); git(ws, "add", "a.py"); git(ws, "commit", "-q", "-m", "step one")
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("OK+commit -> 'verified by git' in reply", "verified by git" in r.lower() and "mark it done" in r.lower())
    chk("file path is rung=direct (NEVER Claude)", orch.current_rung.get() == "direct")
    chk("pending set kind=result, current->1", orch._PENDING.get("kind") == "result" and orch._PENDING["apply"]["current"] == 1)
    r = await orch.resolve_edit("yes")
    st = orch._load_state()
    chk("'yes' advanced: current=1, step1 done", st.get("current") == 1 and st["steps"][0]["status"] == "done")
    chk("step 2 prompt.md written on advance", "STUB step 2" in pf.read_text(encoding="utf-8"))

    print("[MISMATCH] result says done but NO commit + clean tree -> FLAG, do not advance")
    ws2 = mkrepo()
    set_state(ws2, current=0, last_head=orch._git_head(ws2))
    (ws2 / ".nervice").mkdir(parents=True, exist_ok=True)
    (ws2 / ".nervice" / "result.json").write_text(json.dumps({"status": "ok", "summary": "all done"}), encoding="utf-8")  # untracked .nervice only
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("empty-diff mismatch FLAGGED (no commit/changes)", "⚠️" in r and "no new commit" in r.lower())
    chk("mismatch still asks (doesn't silently advance)", orch._load_state().get("current") == 0)
    r = await orch.resolve_edit("cancel")
    chk("'cancel' on mismatch -> nothing changed", orch._load_state().get("current") == 0 and "left the plan" in r.lower())

    print("[FAIL] status fail -> proposes a fix step")
    set_state(ws2, current=0, last_head=orch._git_head(ws2))
    (ws2 / ".nervice" / "result.json").write_text(json.dumps({"status": "fail", "problems": ["import error in timer.py"]}), encoding="utf-8")
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("fail -> proposes a fix step", "failed" in r.lower() and "fix step" in r.lower())
    chk("fail pending inserts a Fix step", orch._PENDING.get("apply", {}).get("steps", [{}, {}, {}])[1]["title"].startswith("Fix:"))

    print("[PARTIAL] status partial -> proposes a follow-up step")
    (ws2 / ".nervice" / "result.json").write_text(json.dumps({"status": "partial", "problems": ["no tests yet"]}), encoding="utf-8")
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("partial -> proposes a follow-up", "mostly worked" in r.lower() and "follow-up" in r.lower())

    print("[MISSING / GARBAGE result.json] honest, no crash, no mutation")
    ws3 = mkrepo(); set_state(ws3, current=0, last_head=orch._git_head(ws3))
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("missing result -> honest 'no result yet'", "no result yet" in r.lower() and not orch._PENDING)
    (ws3 / ".nervice").mkdir(parents=True, exist_ok=True)
    (ws3 / ".nervice" / "result.json").write_text("{not valid json", encoding="utf-8")
    r = await orch.propose_result_from_file()
    chk("garbage result -> honest parse error, no crash", "couldn't parse" in r.lower() and not orch._PENDING)

    print("[no workspace set] honest 'set it first'")
    orch._save_state({"steps": [dict(s) for s in STEPS], "current": 0})  # no workspace key
    orch._PENDING = {}
    r = await orch.propose_result_from_file()
    chk("no workspace -> asks to set it (no guess)", "set the project workspace" in r.lower())

    print(f"\n=== CC-FILE-LOOP (deterministic) {P} passed, {F} failed ===")
    sys.exit(1 if F else 0)

asyncio.run(main())
