"""Skills/shortcuts tests (Build B). The LLM parse is mocked for determinism (the create machinery,
validation, gating, run, list, delete are what's under test); the launchers are mocked so nothing
real opens. Snapshots + restores data/skills.json. Throwaway."""
import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import app.skills as skills
import app.computer as computer
import app.llm as llm

R = {}
STORE = pathlib.Path("data/skills.json")
_backup = STORE.read_text(encoding="utf-8") if STORE.exists() else None

# mock launchers — record calls, open nothing real
OPENED = []
computer.open_app = lambda name: (OPENED.append(("app", name)) or f"Opened {name} for you.")
computer.open_url = lambda url, browser=None: (OPENED.append(("url", url)) or f"Opened {url}.")


async def fake_parse(system, user):
    u = user.lower()
    if "work mode" in u:
        return {"trigger": "work mode",
                "steps": [{"action": "open_app", "arg": "vscode"}, {"action": "open_url", "arg": "github"}],
                "unsupported": []}
    if "cleanup" in u:
        return {"trigger": "cleanup", "steps": [{"action": "open_app", "arg": "notepad"}],
                "unsupported": ["close all chrome windows", "delete my downloads"]}
    if "study mode" in u:   # obsidian is NOT whitelisted -> must be gated at run
        return {"trigger": "study mode",
                "steps": [{"action": "open_app", "arg": "obsidian"}, {"action": "open_url", "arg": "youtube"}],
                "unsupported": []}
    return {"trigger": "", "steps": [], "unsupported": []}
llm.chat_json = fake_parse


async def main():
    if STORE.exists():
        STORE.unlink()   # clean slate

    # t1: create by chat -> parsed sequence confirmed, saved on yes
    c1 = await skills.handle("nate", "when I say work mode, open vscode and open github")
    confirmed = ("work mode" in c1 and "vscode" in c1.lower() and "github" in c1.lower()
                 and "save" in c1.lower())
    s1 = await skills.handle("nate", "yes")
    saved = "saved" in s1.lower() and any(s["trigger"] == "work mode" for s in skills.load_skills())
    R["t1 create + confirm + save"] = confirmed and saved
    print(f"t1: confirm={c1[:90]!r}")
    print(f"t1: save={s1[:50]!r}  stored={[s['trigger'] for s in skills.load_skills()]}")
    print("t1", "PASS" if R["t1 create + confirm + save"] else "FAIL")

    # t2: say the trigger -> steps execute IN ORDER via the real (mocked) actions
    OPENED.clear()
    r2 = await skills.handle("nate", "work mode")
    R["t2 run executes steps in order"] = ("running work mode" in r2.lower()
        and OPENED == [("app", "vscode"), ("url", "https://github.com")])
    print(f"t2: opened={OPENED}  reply={r2[:80]!r}")
    print("t2", "PASS" if R["t2 run executes steps in order"] else "FAIL")

    # t3: list
    r3 = await skills.handle("nate", "list my skills")
    R["t3 list"] = "work mode" in r3.lower() and "vscode" in r3.lower()
    print(f"t3: {r3[:90]!r}")
    print("t3", "PASS" if R["t3 list"] else "FAIL")

    # t4: delete
    r4 = await skills.handle("nate", "delete the work mode skill")
    R["t4 delete"] = "deleted" in r4.lower() and not any(s["trigger"] == "work mode" for s in skills.load_skills())
    print(f"t4: {r4[:60]!r}  remaining={[s['trigger'] for s in skills.load_skills()]}")
    print("t4", "PASS" if R["t4 delete"] else "FAIL")

    # t5: a skill referencing UNSUPPORTED actions -> honest, not faked (reports them, saves only supported)
    r5 = await skills.handle("nate", "make a cleanup skill that closes all chrome windows and deletes my downloads and opens notepad")
    honest = ("left out" in r5.lower() or "can't" in r5.lower() or "cannot" in r5.lower())
    names_them = ("chrome" in r5.lower() or "download" in r5.lower() or "close" in r5.lower() or "delete" in r5.lower())
    keeps_supported = "notepad" in r5.lower()
    R["t5 unsupported handled honestly"] = honest and names_them and keeps_supported
    print(f"t5: {r5[:160]!r}")
    print("t5", "PASS" if R["t5 unsupported handled honestly"] else "FAIL")
    await skills.handle("nate", "no")   # don't keep it

    # t6: skill steps respect risky-action gating — a non-whitelisted app is NOT auto-opened
    await skills.handle("nate", "make a study mode skill that opens obsidian and opens youtube")
    await skills.handle("nate", "yes")
    OPENED.clear()
    r6 = await skills.handle("nate", "study mode")
    obsidian_gated = ("app", "obsidian") not in OPENED                       # never auto-opened
    youtube_ran = any(o[0] == "url" and "youtube" in o[1] for o in OPENED)   # safe step still ran
    reported = "skipped" in r6.lower() and "obsidian" in r6.lower()
    R["t6 risky step gated, not bypassed"] = obsidian_gated and youtube_ran and reported
    print(f"t6: opened={OPENED}  reply={r6[:120]!r}")
    print("t6", "PASS" if R["t6 risky step gated, not bypassed"] else "FAIL")

    # ---- restore the real store ----
    if _backup is not None:
        STORE.write_text(_backup, encoding="utf-8")
    elif STORE.exists():
        STORE.unlink()

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
