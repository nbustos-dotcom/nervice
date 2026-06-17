# NERVICE — FULL RECALIBRATION AUDIT

**Date:** 2026-06-14 · **Scope:** read-only, zero code changes · **Branch:** main (HEAD `290eddd`)
**Lens:** free-first, local, single-user personal AI — audited against *that* standard (provably free, safe by architecture, fully understood, daily-useful), **not** feature parity with hosted assistants.

> **Method.** Read the real code on `main`, not the docs. `docs/PROJECT_STATE.md` is dated 2026-06-11 and is already stale — it predates five shipped modules (`orchestrator.py`, `confusion.py`, `usage.py`, `browser.py`, `actionlog.py`) and misstates current details (voice is `am_echo`, not `bm_george`; VAD is Silero, not webrtcvad). Where this audit and PROJECT_STATE disagree, the code wins. Evidence is cited as `file:line`. Where I did not verify something, I say so.

> **Bottom line up front.** The architecture is more disciplined than most hobby agents — real code-enforced gates, a genuine single front door, honest-failure engineering throughout. But the project's defining promise — *provably free* — is now only **partially true**. Claude/the Agent SDK became metered on June 15; the newest module (`orchestrator.py`) was written knowing that and deliberately avoids Claude, while the older confusion-net, the consult tool, and the 429-fallback ladder were all built when Claude was free and can now **spend money in at least 8 paths, several of them silent, with no budget, no kill switch, and — for the most common path — no cost tracking at all.** That, plus a hidden hard dependency that 500s turns when the "free local rung" is down, are the two things that most contradict the stated identity.

---

## 1. WHAT EXISTS — the real inventory

`app/` + root entrypoints ≈ **8.4k lines of Python** (grown well past the doc's "~10.7k total"), plus ~44 scripts and a single-file HUD now ~1,260 lines. Health verdict: **solid** / **fragile** / **duct-taped**.

| Capability | File(s) | Wired & working? | Health |
|---|---|---|---|
| HTTP/WS API + Bearer auth | `app/api.py` | yes | **solid** |
| Turn orchestration (REST) | `app/chat.py` `respond()` | yes | **solid** |
| Turn orchestration (WS stream) | `app/streaming.py` `stream_reply()` | yes | **solid** |
| Router — single front door | `app/router.py` | yes | **solid** |
| Brain ladder: Groq tool loop + 429 fallbacks | `app/llm.py` | yes | **solid** (free-first gaps — §3) |
| Claude account ladder (Pro→Max) + jails | `app/agent.py` | yes | **solid** (now metered) |
| Groq token metering | `app/usage.py` | yes | **fragile** (Groq-only; ignores Claude) |
| Confusion → Claude escalation net | `app/confusion.py` | yes | **fragile / duct-taped** (paid auto-escalate — §3) |
| Memory: store / recall / extract | `app/{models,db,embeddings,retrieval,memory}.py` | yes | **fragile** (hard dep — §6; requeue unconsumed — §5) |
| Persona + content safety floor | `app/{persona,safety}.py` | yes | **solid** |
| Self-modification gate | `app/selfmod.py` | yes | **solid** (4 stale proposals) |
| Computer control (closed 5-action set) | `app/computer.py` | yes | **solid** |
| Skills (saved command chains) | `app/skills.py` | yes | **solid** (`data/skills.json` currently empty) |
| Music favorites | `app/music.py` | yes | **solid** (legacy text parser inside) |
| Sysinfo telemetry (read-only) | `app/sysinfo.py` | yes | **solid** |
| Weather / GPS | `app/{weather,geo}.py` | yes | **solid** (hardcoded coord fallback) |
| Voice STT/TTS (local) | `app/voice.py` | yes | **solid** (`am_echo` Kokoro, Silero VAD) |
| Wake word loop | `wake.py` | yes (separate loop) | **fragile** (import-order coupling, PROJECT_STATE #8) |
| **Direct browser — Canvas read** | `app/browser.py` | yes | **fragile** (holds real creds; brittle selectors — §4/§6) |
| Canvas Q&A route | `app/chat.py` `_execute_canvas` | yes | **fragile** (depends on `browser.py` + Canvas DOM) |
| Browse agent (isolated, Claude) | `app/agent.py` `browse_agent` | yes | **solid** |
| Builder agent (jailed, Claude) | `app/agent.py` `agent_task` | yes | **solid** (Pro-only / metered) |
| **Orchestrator (project planner)** | `app/orchestrator.py` | yes | **solid** (cleanest new module) |
| Action-audit feed | `app/actionlog.py` | yes | **solid** |
| HUD v3 (active) | `app/static/index_v3.html` | yes | **fragile** (1.2k-line single file, preview-tested only) |
| Telemetry endpoints (`/ladder` `/pending` `/daily-summary` `/activity` `/updates` `/last-turn` `/system` `/news` …) | `app/api.py` | yes | **solid** |

**Built-but-unused / not-wired (dead or dormant):**
- `GET /location` (api.py:238) — no client calls it (only the POST half is live).
- `GET /memories/recent` (api.py:526) — zero references in any HUD; strongest dead-endpoint candidate.
- `POST /browser/kill` (api.py:807) — works and is authed, but its own docstring says "The HUD will wire a button to this later." Functional, unwired.
- `data/memory_requeue.jsonl` — written, **never consumed** (§5).
- Piper TTS fallback, `index.html` (v1), `index_v2.html` (v2) — legacy, kept (§5).
- Orchestrator's result-feedback loop, propose→approve-from-results, and the **ASKS panel** — designed in `docs/ORCHESTRATOR_LOOP_DESIGN.md`, **unbuilt** (the talk-to-edit half *is* built).

---

## 2. ARCHITECTURE HEALTH

**The good shape.** The system has one genuine spine and it held:

- **The router really is the single front door.** `router.classify()` is the only intent decider; deterministic code only *executes* (`router.py:1-8`). The old "doormen" (music regexes, sysinfo guard) were demoted to route targets, not pre-emptors. New routes (`canvas`, `orchestrator`, `actions`) were added inside the same dict-returning contract (`router.py:46`), not bolted on beside it.
- **REST and WS share one executor.** `chat.respond()` and `streaming.stream_reply()` run the *same* gate order and both call `chat.execute_route()` (chat.py:89, streaming.py:403). Gate parity is real — confusion escalation, the 429 ladder, and the pending-confirm gates are identical on both paths. This is the single best structural decision in the codebase; it's why the surface didn't fork.
- **The brain ladder is intact and instrumented.** Groq → Ollama → Claude with a sticky cap-state (`llm.py:44-110`), per-turn rung telemetry (`current_rung` contextvar), and `logs/turns.log`. The "no second doorman crept back" test passes.
- **`orchestrator.py` is the model of how to build here** — free-rung-only by construction, scope-flag guards, read-mostly, explicitly metering-aware. It proves the team *can* build correctly free-first when it's front of mind.

**The 3–5 worst structural problems (with evidence):**

1. **Metering-awareness is inconsistent across modules.** `orchestrator.py:6-12` and `actionlog.py:93` were written *post-June-15* and refuse Claude on purpose. But `confusion.py`, `tools.consult_claude`, and the `llm.py` 429-fallback were written when Claude was free and were never retrofitted. The result is an architecture that contradicts itself on its core value depending on which file you're in. (Full trace in §3.)

2. **The "free local rung" is also a hidden hard dependency, and it has no failure handling.** `retrieval.retrieve()` calls `embed()` (Ollama) and the DB with no try/except (retrieval.py:19-20); `embeddings.embed()` is a bare `await` (embeddings.py:9-14); `respond()` runs `build_system_prompt` inside `asyncio.gather` with no handler (chat.py:275-277). If Ollama or Supabase is unreachable, **the gather raises and the turn 500s** — the endpoint only catches `RateLimitError` (api.py:740, 784). So Ollama isn't just the fallback; it's load-bearing for every routed turn, and its absence is a hard failure, not a graceful degrade. (Detail in §6.)

3. **Two browser subsystems with opposite security postures.** The documented one (`agent.browse_agent`) is `--isolated`, zero-credential, tool-locked (agent.py:202-238). The new one (`app/browser.py`) is a **persistent, credential-holding** direct-Playwright context (browser.py:28-32, 123-139). They share no code and no policy. Nothing names this split or reconciles it. (Detail in §4.)

4. **Cost/telemetry asymmetry.** `usage.py` meters Groq tokens only (usage.py:1-8). `agent_task`/`browse_agent`/`propose_agent` populate `agent.last_run.cost_usd` and log it (tools.py:122-129, 152-159, 166-171). But `ask_claude` — the *consult* path and the confusion-escalation target — **never populates `last_run`** (agent.py:63-95), so the most-traveled paid path has zero cost capture anywhere.

5. **In-process single-user state is load-bearing and scattered.** `_windows` (api.py:61), `confusion._HIST/_escalations/_PREV_BAD_END` (confusion.py:20-23), `computer._pending`, `skills._pending`, `orchestrator._PENDING`, and the `browser` singleton are all module globals keyed loosely or not at all. Correct for one user on one process; every one of them is a latent bug the moment concurrency or a second user appears, and several silently reset semantics on restart.

---

## 3. FREE-FIRST INTEGRITY *(critical)*

**There is no global Claude kill-switch, no daily/session Claude budget, and no confirm-before-spend anywhere in the codebase.** A repo-wide grep for budget/disable/kill flags turns up only `NERVICE_FORCE_CAPPED` (a *test* lever, llm.py:52), `NERVICE_BROWSER_HEADLESS`, and the confusion net's `MAX_PER_HOUR = 4`. That 4/hour is the **only** numeric limiter on Claude in the entire system.

### Every path that can reach metered Claude / the Agent SDK

| # | Path | Trigger | Gated by | Can spend silently? |
|---|---|---|---|---|
| 1 | Router `hard` → `consult_claude` → `ask_claude` (Pro→Max) | router classifies a "hard" question (router.py:29) | nothing; fires even when capped via `_forced_tool_direct` (llm.py:417-422, 451-465) | No (Nate asked hard) but **uncapped & uncosted** |
| 2 | Router `build` → `agent_build` → `agent_task` | explicit build request | Pro-only; 600s timeout (chat.py:85) | No (explicit) |
| 3 | Router `selfmod` → `propose_self_update` → `propose_agent` | explicit self-change request | Pro-only | No (explicit) |
| 4 | Router `browse` → `browse` → `browse_agent` | explicit browse-a-site request | Pro-only; 120s | No (explicit) |
| 5 | **Tool-model auto-call** of `consult_claude`/`agent_build`/`browse`/`propose_self_update` | the gpt-oss-120b model decides to, on **any normal turn** | **only the tool description text** (tools.py:183, 294) — present on every REST `chat_with_tools` and WS `_stream_normal` (streaming.py:173-174) | **YES** |
| 6 | **Confusion escalation** → forces `hard` → `consult_claude` | two consecutive "signal" turns (confusion.py:69-77) | budget 4/hour (confusion.py:24) | **YES** |
| 7 | **Groq-429 fallback** → `_claude_fallback` → `ask_claude` | Groq capped **and** Ollama returns nothing | only "is Ollama answering?" (llm.py:437-442; streaming.py:369-376) | **YES** |
| 8 | **Grounded-synthesis 429** → Claude | Groq caps mid web-search synthesis, non-news (llm.py:356-372) | nothing | **YES** |

### The confusion-escalation net — quantified risk

`confusion.check()` returns `'escalate'` when **two consecutive turns** each raise a signal, where a signal is *(a)* the previous turn ended `exhausted` or guard-tripped, *(b)* the message opens with a correction ("no,", "that's not", "I said"…), or *(c)* it's a near-duplicate of the last 3 utterances (confusion.py:55-79). On escalate, `chat.py:286` / `streaming.py:340` rewrite the route to `{"route":"hard"}` → `consult_claude` → **paid Claude**, budgeted at **4 per rolling hour** (confusion.py:71-78). No daily ceiling. Worst case ≈ 96 Claude consults/day from repetition alone.

**The perverse part:** a signal is *most likely* exactly when Groq is capped — because then you're on the 4B (Ollama), which trips its own honesty guard or gives weak answers, so Nate repeats/corrects. So the confusion net is biased to fire **precisely during the capped state you're trying to stay cheap in**, converting "free local fallback" into "paid Claude" on the second corrective turn. It was a sensible feature when Claude was free; post-June-15 it is an uncapped-by-day spend valve triggered by user frustration.

### Where free-first can silently break post-June-15

- **Path 5 is the biggest silent surface:** any normal turn where the 120b model judges a question "hard/puzzle/architecture" calls `consult_claude` itself — no router `hard`, no Nate intent, no budget. The only brake is a tool description that now says "(and metered)" (tools.py:183) — i.e., the model's discretion is the policy.
- **A down Ollama flips the capped-state fallback from free to paid.** When Groq is capped, the intended path is Ollama (free). If Ollama is up-but-empty (model not loaded, non-convergent loop), Path 7 sends the turn to Claude (llm.py:440-441). Given the documented restart/`forrtl` reality where Ollama isn't always running, this is the most likely real-world money leak. *(Caveat: if Ollama is **fully** down, the turn instead 500s at embedding retrieval before reaching the ladder — see §6 — so "Ollama flaky" spends money while "Ollama dead" breaks turns. Both are bad.)*
- **No cost visibility on the consult path** (agent.py:63-95): even when it fires, nothing records the spend. `/ladder` and `usage.json` show Groq only. You cannot currently answer "how much did Nervice spend on Claude today?" from the system itself.

**Verdict for §3:** Free-first is architecturally true for the *primary* path (Groq→Ollama) and for the newest modules, but **operationally unguarded** for Claude. The identity claim "provably free" is not currently provable — there's no meter, no cap, and no switch on the metered tier.

---

## 4. SAFETY AUDIT

| Mechanism | Protects | Enforcement | Gap / bypass |
|---|---|---|---|
| **SAFETY_FLOOR** (safety.py:1) | content-harm uplift (drugs/weapons/malware) | one-line string appended to PERSONA at import (persona.py:52); re-asserted by selfmod (selfmod.py:225-231) | **Content-only and model-enforced.** Does nothing for actions, spend, or the 4B beyond prompt text. Not an action-safety layer. |
| **Self-mod gate** (selfmod.py) | unsafe self-edits | code: fail-closed `EDITABLE` allowlist of 8 files (selfmod.py:18-21), staging shows proposer only those files (43-53), double path re-validation (191-196), clean-tree + `git apply --check` dry-run + `py_compile` + SAFETY_FLOOR assert + rollback (198-231), human-approve-only | `EDITABLE` includes `llm.py`/`router.py`/`tools.py`/`chat.py` — the free-first ladder — and the only automated invariant is `SAFETY_FLOOR in PERSONA`; **no free-first invariant** is checked. Human review is the sole backstop. Also: `propose()` spends Pro Claude. |
| **Builder jail** (agent.py:152-199) | filesystem/secret escape during builds | code: `cwd=WORKSPACE`, tool allowlist + Bash sub-command allowlist, `acceptEdits`, secrets popped from `os.environ` (`_SCRUB_KEYS`), no remotes/push | Scrub is a **global `os.environ` pop/restore**; agent.py:59 admits concurrent calls would race and leak. Bash safety leans on Claude Code's per-subcommand parsing. Canaries exist (`scripts/canary_*`). |
| **Browse jail** (agent.py:202-238) | injection / credential theft via web | code: Playwright MCP `--isolated`, `strict_mcp_config`, `tools=[]`, `allowed_tools=mcp__playwright__*`, `disallowed_tools` removes `browser_evaluate`/`run_code_unsafe`; prompt forbids login/purchase | Strong. This is the zero-credential browser. |
| **Direct browser** (browser.py) | — | read-only **by construction** (no click/type/fill/submit primitive exists, browser.py:44-46); https-only (141-144); honest needs-login (458-467); kill switch (496-513); full audit + debug dump | **Holds Nate's real Canvas/MTU session cookies** (`data/nervice_browser_auth.json`, persistent profile, browser.py:28-32, 123-139). Runs `page.evaluate` JS in an authenticated session (read-only JS, but it's the seed of an acting surface). **No confirm gate on navigation.** Long-lived singleton. `CANVAS_URL` hardcoded (browser.py:41 — the "Nate fills this in" comment at :39 is stale). |
| **Control boundary** (computer.py) | local machine actions | code: closed 5-action set, deterministic `interpret()` (no model), `_RISKY_WORDS` → fail-safe-to-RISKY, confirmation gate (`resolve_pending`), audit on every decision; router args re-risk-checked in code (`_decision_from_args`, computer.py:589-619) | Solid. `play_youtube` composes `open_url` (no new boundary). No bypass found. |
| **Kill switch** | runaway browser | `browser.close_browser()`, `is_kill_request` in-turn (chat.py:229, streaming.py:246), `/browser/kill` (authed) | **Browser only.** There is **no global "stop everything"/panic switch** for the agent as a whole. |
| **API/WS auth** | network access | Bearer on every data route (`Depends(auth)`, constant-time, api.py:168-175); WS token in first frame, close 1008 (api.py:822-836) | Static pages unauth but inert; localhost-bind only (Phase-B tailnet bind pending). Solid. |
| **Orchestrator scope-flag** (orchestrator.py:66-69) | planner emitting prompts that touch Nervice's own safety/self-mod/core | deterministic regex on generated text *and* step labels (orchestrator.py:364-370, 928-940) | Good defense-in-depth. |

**Weakest points as the system moves toward autonomy (screen control / the closed loop):**

1. **Unbounded Claude spend (§3) is itself a safety problem** once a closed loop can fire turns without a human in the seat. A loop that hits the confusion net or the auto-consult path has no spend ceiling to stop it.
2. **The direct browser is one feature away from acting in an authenticated session.** Read-only is enforced by *absence of a primitive*, not by a gate — exactly the kind of safety that erodes the moment someone adds `click()`. There is no navigation confirm and no per-action human-in-the-loop there (unlike `computer.py`'s confirm gate).
3. **No global kill/panic switch.** Browser teardown exists; "stop all action / freeze" does not.
4. **Self-mod can rewrite the ladder** (`EDITABLE` ⊇ llm/router/tools/chat) with only human review as the free-first guard — fine today (human approves), risky if approval ever gets rubber-stamped or automated.
5. **The secret-scrub race** (agent.py:59) is benign under today's strict single-user-sequential assumption and a live hazard the instant that assumption breaks.

The gates that exist are genuinely enforced in code, not vibes — that's the strength. The exposure is in *what isn't gated yet* (spend, browser acting, a global stop) right as the roadmap points at autonomy.

---

## 5. DEAD WEIGHT & DRIFT

- **`data/memory_requeue.jsonl` — written, never consumed.** 63 lines (56 `verify`, 7 `deferred`). The only reader is `GET /pending` (api.py:632), which *counts lines for display* — it never re-verifies, re-extracts, or drains. `memory.py:16-18` describes a "batch re-verification when Groq resets" that **does not exist**. Quarantined local-rung memories stay `salience ≤ 3` forever and never enter CORE. This is documented as a known gap (PROJECT_STATE #2) and is still open.
- **`scripts/` — ~44 files, no runner.** No `pytest.ini`/`conftest.py`/`Makefile`/CI; `scripts/full_test.py` (636 lines) is the de-facto aggregate. Nothing in `app/` imports from `scripts/` (correctly standalone). **Cut:** `test_polish.py` (asserts `classify()=="normal"` — a string compare against a dict; can never pass), `test_phase0.py` + `test_phase1.py` (shipped-phase scaffolding superseded by `test_ladder.py`/`_bench_ollama.py`), `probe_neutts.py` (abandoned NeuTTS engine), `probe_capture.txt` (regenerable artifact). The other ~38 are a coherent manual regression suite.
- **`proposals/` — 4 stale pending, never resolved.** IDs `20260609-193300`, `20260610-015632`, `20260610-133819`, `20260610-134948` (created 06-09/06-10, untouched since). 5 rejected, 6 `.patch`. Same count PROJECT_STATE flagged — no progress.
- **Dead endpoints:** `GET /location` (238), `GET /memories/recent` (526), `POST /browser/kill` (807, unwired). The first two are reachable only by an unbuilt UI.
- **Legacy HUDs:** `index.html` (v1) is REST-only dead weight kept alive because its `/chat` + `/voice` double as the test-suite targets; `index_v2.html` is functional but **blind to the newer `{type:"trace"}` and `{type:"hands"}` frames** (its `handleFrame` has no branch). `manifest.json` + the flat `icon-192/512.png`/`apple-touch-icon.png` are the v1-era set, superseded by `manifest.webmanifest` + `icons/`.
- **Doc drift (the map no longer matches the territory):**
  - `PROJECT_STATE.md` — **partially stale**: omits orchestrator/confusion/usage/browser/actionlog entirely; wrong voice (`bm_george`→ actually `am_echo`), wrong VAD (Silero), route table missing `canvas`/`orchestrator`/`actions`. Still accurate on the core ladder models.
  - `ORCHESTRATOR_LOOP_DESIGN.md` — **mixed**: talk-to-edit (PART 2) is fully built (orchestrator.py:747-1006); the result/git feedback loop, propose→approve-from-results, and the **ASKS panel** are unbuilt (the doc itself reserves ASKS).
  - `VOICE_UPGRADE_PLAN.md` — **mostly shipped** but written as future tense; stale voice name.
  - `HUD_V4_DESIGN.md`, `ORCHESTRATOR_FEASIBILITY.md`, `RESEARCH_FINDINGS.md`, `VOICE_SPEECH_RESEARCH.md` — current/accurate.
- **Code-level debt markers worth tracking:** the **TEMPORARY Max account** time-bomb (agent.py:19,30 — must be deleted when it expires), localhost-only/"Phase B bind" interim (run_api.py:18,110; api.py:4), read-only-browser/"acting is later" (browser.py:46), **hardcoded Canvas DOM selectors** (browser.py:213-214), legacy music text parser (music.py:91), hardcoded geo coords (geo.py:12). Plus the PICOVOICE doc residue (PROJECT_STATE #11).

---

## 6. FRAGILITY & RISK

1. **[Highest] Routed turns 500 when Ollama or Supabase is down.** `embed()` (embeddings.py:9-14) and the DB session (retrieval.py:19-20) are bare awaits; `respond()`/`stream_reply()` build the system prompt inside an unguarded `asyncio.gather` (chat.py:275-277, streaming.py:332-333); endpoints catch only `RateLimitError`. So the "free local rung" being down — the exact restart/`forrtl` scenario in your own notes — takes the *whole assistant* down on any routed turn, and the friendly `LADDER_EXHAUSTED_MSG` never gets a chance to show. *(The pre-routing deterministic gates — kill-browser, setup, edit-confirm, pending, skill — still work, since they precede retrieval.)*
2. **Stale-server trap — improved, not closed.** New: boot log `logs/server.log`, `/health` reports booted git hash + boot time (api.py:47-55, 209), and a port-in-use preflight identifies the occupant via `/health` (run_api.py:91-102). But `uvicorn.run` has no reload (run_api.py:111); selfmod itself says "restart to load the change" (selfmod.py:248). The trap is now *detectable* if you look, not *prevented*.
3. **cp1252 console crash — fixed** at the stdout level (`reconfigure(encoding="utf-8", errors="replace")`, run_api.py:9-13). This one's genuinely closed.
4. **Long-lived in-process state.** Browser singleton context (orphan risk; mitigated by lifespan close, api.py:113-114, + kill switch); `confusion._PREV_BAD_END/_escalations` (a stuck bad-end bias persists until restart); `_windows` (lost on restart — documented, acceptable). All assume one user, one process.
5. **Event-loop blocking on a single-process server.** `play_youtube` (yt-dlp, 1-3s) and `_running` (tasklist, ~0.9s) run synchronously in `handle_control` (computer.py:278-302, 405-418); REST `/voice` runs STT+TTS inline (api.py:761, 793) while WS `/voice` correctly uses `to_thread` (api.py:891). Tolerable for one user; real latency spikes that block other requests briefly.
6. **Secret-scrub race** (agent.py:59) — safe only under strict sequential single-user use.
7. **Confusion spends most when capped** (§3) — a reliability-shaped money risk.
8. **Browser brittleness** — hardcoded Canvas selectors (browser.py:213-214, 263-264) break on any Canvas markup change; saved auth silently expires (handled honestly with needs-login, which is good).
9. **`memory_requeue.jsonl` grows unbounded** (§5).
10. **Restart-crash gotcha** (your own note: MKL `forrtl` window-CLOSE on restart mid voice-warm) — the launch/restart path is itself fragile; combined with #1, an Ollama/voice hiccup on restart is a multi-failure cluster.

---

## 7. WHAT'S MISSING for the stated goal

Not a feature wishlist — the structural/usability gaps actually between Nervice and "free, safe, understood, daily-reached-for":

- **A Claude spend governor.** The single biggest gap for *provably free*: a daily/session Claude budget, a **visible spend counter** (the data already exists in `agent.last_run` for build/browse/selfmod; extend it to `consult` and surface it on `/ladder`), and a "free-only mode" flag or confirm-before-Claude. Without this, "free" is a hope, not a guarantee.
- **Graceful degradation when Ollama/Supabase are down.** Memory recall should fail soft to "no memories this turn," not 500 the turn (§6.1). This is the difference between "the local rung is flaky and Nervice keeps working" and "Ollama hiccupped and the assistant is down."
- **A `memory_requeue` consumer.** Until something drains it, every locally-extracted fact is a permanent second-class citizen, and the capped-state memory path is write-only (§5).
- **A global kill/panic switch and a browser-navigation gate** — needed *before* the screen-control/closed-loop autonomy work, not after.
- **A test runner + pruned suite.** "Fully understood" is undermined by 44 ad-hoc scripts, broken assertions, and no CI. One `pytest` entry + deleting the 4 stale files would make "verify after a change" a single command.
- **Doc truth.** `PROJECT_STATE.md` is the canonical map and it's already wrong three days on. For "fully understood," regenerating it (this audit is a start) and keeping it in sync has to be part of the loop.
- **Closing the orchestrator loop** it was designed around (result feedback, ASKS) — today it's a planner that emits prompts Nate pastes; the "closed loop" remains aspirational (§5).

---

## 8. BLUNT VERDICT

**What's genuinely good (keep and build on):**
- The single-front-door router + shared `execute_route` across REST and WS. The architecture didn't fork; gates don't drift. Rare discipline.
- Safety gates that are *code*, not prompt-hope: the closed 5-action control set, the fail-closed selfmod allowlist with rollback + safety assertion, the isolated browse jail, read-only-by-construction Canvas perception.
- Honest-failure engineering is real and pervasive: launch verification via tasklist, the 4B action-claim guard, grounded synthesis + cross-model verify, honest needs-login, extractive (zero-LLM) capped news. The "real data or honest failure" value is actually implemented.
- `orchestrator.py` — disciplined, metering-aware, scope-guarded. The template for everything after it.

**What's a house of cards:**
- **Free-first post-June-15.** Eight paths reach metered Claude; several are silent; the only limiter is 4/hour on one of them; the most common path is uncosted. The system's defining promise is currently unenforced and unmeasured.
- **The "free local rung" as a hidden hard dependency.** It's sold as the free fallback but it's load-bearing for memory on every turn, and its absence is a 500, not a graceful degrade — while its *flakiness* silently routes to paid Claude. The fallback story has a hole exactly where reliability and cost meet.
- **Accretion since the last audit:** five new modules in three days, two browser systems with opposite credential postures, a stale canonical doc, dead endpoints, an unconsumed requeue, and a runner-less script pile. Individually minor; collectively, "fully understood" is slipping.

**Top 5, ranked, to address before building further:**

1. **Put a governor on Claude spend.** Daily/session budget + visible cost counter (extend `last_run` to `consult`, surface on `/ladder`/`usage.json`) + a free-only / confirm-before-Claude switch, and re-scope the confusion net's escalation so it can't auto-spend during the capped state. *This is the core identity; it's the one thing that makes "provably free" provable.*
2. **Make routed turns survive a down Ollama/Supabase.** Wrap retrieval to fail soft; let the turn proceed with no memories and let the ladder show its honest message. Fixes both the 500 (§6.1) and removes the worst silent-spend trigger (§3).
3. **Reconcile the browser story before screen-control.** Decide whether persistent Canvas credentials are acceptable, document the two-browser split, and add a navigation confirm + the global kill switch *now* — read-only-by-construction won't survive the first acting feature.
4. **Re-baseline the docs and prune dead weight.** Regenerate `PROJECT_STATE.md` (or adopt this audit as the map), resolve/clear the 4 stale proposals, delete the 3 dead endpoints + 4 stale scripts, and either build or delete the `memory_requeue` consumer. Stop the accretion before more lands.
5. **Add a test runner + the `memory_requeue` drainer, and put a free-first invariant in the selfmod gate.** Lower urgency, real payoff: one-command verification, memories that aren't permanently quarantined, and an automated check that a self-edit can't quietly weaken the ladder.

> **Confidence / not verified:** the §6.1 500-on-dependency-failure claim is inferred from the call paths and the absence of handlers (embeddings.py, retrieval.py, chat.py:275-277, api.py:740/784); I did not execute a down-Ollama turn to confirm the exact failure surface. Endpoint line numbers for `/location`, `/memories/recent` come from a focused read of `api.py`, not a line I re-counted by hand. Everything else is cited from code read directly during this audit.
