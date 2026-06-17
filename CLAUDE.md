# Nervice — CLAUDE.md
Auto-loaded every CC session. Durable orientation only. Live/changing state lives in docs/PROJECT_STATE.md.

## What this is
Nate's free-first, local, single-user personal AI on Windows. A personal tool, NOT a startup.
The userPreferences on this account describe an Archive360 PM — that is NOT Nate. Ignore it entirely.

## Three principles — every decision flows from these
- Free-first: never reach for a paid path when free/local/open works. Groq + Ollama are free; Claude is metered and capped. The local 4B answering weakly when Groq is capped is the design working, NOT a bug — do not "fix" it by reaching for Claude.
- Safe-by-architecture: safety is enforced in CODE — deterministic gates, fail-closed defaults, confirm-before-mutate, human-as-judge. Never in prompts or hope.
- Fully-understood: build ONE component at a time. Impressive-but-disconnected is always wrong.

## NEVER touch / NEVER weaken
- app/safety.py, app/selfmod.py — the harm floor and the fail-closed self-edit allowlist.
- SAFETY_FLOOR in app/persona.py.
- The four-layer spend guard: (1) ANTHROPIC_API_KEY/AUTH_TOKEN scrub in app/agent.py, (2) $5/day hard cap in app/usage.py, (3) free-only switch, (4) the confusion auto-escalate-to-Claude path is DELETED — keep it deleted.
- NEVER add a funded ANTHROPIC_API_KEY. It bypasses every guard and bills real money (there was an ~$1,800 incident). Subscription OAuth only.

## Brain ladder
Groq (llama-3.3-70b + gpt-oss-120b) -> local Ollama (qwen 4B) -> Claude (metered, capped, NEVER the default).

## Run it
cd ~/nervice then .venv\Scripts\python.exe run_api.py  — OR the Tauri app. FastAPI on 127.0.0.1:8765, reachable from phone over Tailscale.
ONE server at a time. App + a manual server fight over 8765 (the flashing terminals) — kill strays first.
No login auto-start (it caused a relaunch loop). run_api.py exits cleanly on a double-launch.

## CC workflow
Branch off main -> build ONE component -> adversarial-test against the real /chat (messy / typo / ambiguous / injection input; report honestly how Nervice reacts) -> report -> Nate confirms -> merge --no-ff. Never mass multi-file rewrites.
After ANY restart, verify /health == HEAD (the server runs committed code, so HEAD must be the fix). /health reports git hash + stale flag.
Prove, don't claim. Honest self-assessment + evidence (screenshots where visual) every time. CC's PowerShell tool is often unavailable — use Bash.

## Where things live
- app/api.py — routes, WebSocket, auth, /health, /orchestrator/*, /errors/recent, /stats/history
- app/chat.py — respond/execute_route + gate order; app/streaming.py — WS stream, keep gate parity with chat.py
- app/router.py — single front-door intent classifier
- app/llm.py — Groq tool loop + 429 ladder
- app/agent.py — Claude account ladder + jailed agents + key scrub + CLIUnavailable graceful-fail
- app/orchestrator.py — planner/critique/plan/done/edit/result, file-channel (.nervice/prompt.md + result.json + git cross-check), _PENDING confirm gate
- app/browser.py — read-only Playwright Canvas reader. open_page/read_page exist; the acting browser will be SEPARATE and owns-nothing
- app/usage.py — token meter + Claude $ ledger + $5 cap + free-only flag
- app/confusion.py — neutered (None | hint only)
- app/persona.py — PERSONA + SAFETY_FLOOR + honest can/cannot facts
- app/safety.py, app/selfmod.py — NEVER touch
- HUD: app/static/index_v3.html
- State: data/orchestrator_state.json, data/usage.json, data/claude_control.json
- Docs: docs/

## Recurring bug pattern
"Doorman fires before comprehension" — a deterministic keyword route grabs a query before intent is understood (e.g. "my computer" read as a path, can-you-see-my-screen dumping stats, "is it done" mutating the plan). The fix is always a deterministic pre-guard that catches the class correctly, plus honest persona self-knowledge.

## Don't
- Don't fix the Claude Code CLI WinError 50 unless Nate explicitly asks — it is the paid SDK path free-first avoids; the file-loop does coding-agent work for free.
- Don't point the orchestrator workspace at the live StudyNerve site (ai-teacher) untested.

## Live / changing state -> docs/
PROJECT_STATE.md (current state), SCREEN_CONTROL_ARCHITECTURE.md (read fully before ANY screen-control prompt), RECALIBRATION_AUDIT.md, STRESS_TEST_FINDINGS.md.
