# NERVICE — PROJECT STATE (read-only audit, 2026-06-11)

Local-first personal AI assistant. Single user ("nate"). Windows 11, Python 3.14 (.venv),
RTX 4060 8GB shared by STT/TTS/local-LLM. FastAPI server on 127.0.0.1:8765, reached from the
phone over Tailscale. ~10.7k lines. This file is a state report for the next build phase
(screen control); it changes nothing.

## 1. REPO MAP (depth 3; .venv/__pycache__/.git skipped)

```
run_api.py            19   uvicorn runner, host/port, token preflight
nervice.py           122   text REPL (meta commands: proposals/show/approve/reject)
voice.py              67   local push-to-talk voice REPL (mic -> respond -> speak)
wake.py              218   wake-word loop: dormant (openWakeWord) -> active conversation -> sleep words
app/
  api.py             629   FastAPI app: all routes + WS, auth, voice warm thread, db keepalive
  agent.py           324   Claude Agent SDK: account ladder (ask_claude), builder/browse agents, jails
  chat.py            168   respond() orchestrator, build_system_prompt, execute_route, greeting
  computer.py        638   local machine control: closed action set, interpret(), gating, play_youtube
  db.py              ~20   async engine (Supabase pooler, pool_pre_ping)
  embeddings.py      ~15   nomic-embed-text via local Ollama, dim 768
  geo.py              59   phone-GPS location store + BigDataCloud reverse geocode
  llm.py             408   Groq client, tool loop, grounded synthesis+verify, tier-ladder fallbacks
  memory.py          194   extraction (RECONCILE_SYSTEM), dedupe/supersede, capped-quarantine
  models.py          ~40   SQLAlchemy: memories + messages tables (pgvector)
  music.py            96   favorite-artists store + deterministic voice management
  net.py              ~5   truststore.inject_into_ssl() (Norton TLS interception)
  ollama_client.py    84   hardened local-rung client (think:false, keep_alive:-1, is_up)
  persona.py          ~60  PERSONA + SYSTEM FACTS + brevity/assume rules; safety floor appended
  retrieval.py        26   two-tier recall: CORE (salience>=4) + topic (cosine)
  router.py          108   LLM route classify + sysinfo guard + keyword fallback + Ollama fallback
  safety.py            1   SAFETY_FLOOR string (one line, non-editable by selfmod)
  selfmod.py         248   self-modification gate: propose/approve/reject, allowlist, rollback
  skills.py          268   user-defined command chains (trigger -> steps), closed step set
  streaming.py       348   WS turn pipeline: token stream -> sentence TTS, fallback delivery, sync
  sysinfo.py         237   machine telemetry + direct-answer dispatcher
  tools.py           300   Groq tool schemas + funcs (weather/search/news/sysinfo/consult/build/...)
  turnlog.py          32   per-turn telemetry -> logs/turns.log (route|rung|seconds|path)
  voice.py           351   faster-whisper STT + Kokoro TTS + mic VAD capture + junk gate
  weather.py          90   Open-Meteo current + 7-day forecast (uses geo location)
  static/
    index.html       307   v1 phone client (legacy, untouched)
    index_v2.html    663   v2 glassmorphism dashboard (legacy, untouched)
    index_v3.html    885   v3 JARVIS HUD (active client; canvas bg, reactor, gauges, VAD)
alembic/             migrations (0001_init)
docs/VOICE_UPGRADE_PLAN.md  151  the original streaming-upgrade plan
data/                gitignored: skills.json, location.json, music_favorites.json, memory_requeue.jsonl
models/              gitignored: kokoro onnx, piper onnx (fallback), wakeword/hey_jarvis.onnx
proposals/           selfmod proposal records (4 pending, 5 rejected) + .patch files
scripts/             ~35 test/probe scripts (test_ladder, test_phase0/1, diag_master, canaries...)
logs/                turns.log (telemetry), computer_actions.log (audit), claude_calls.log
```

## 2. ENTRY POINTS + RUNTIME

Start: `python run_api.py` -> uvicorn `app.api:app` on **127.0.0.1:8765**. Refuses to boot without
NERVICE_API_TOKEN. Voice models warm in a background thread at startup; async DB keepalive pings
Supabase every 60s. Local REPLs: `python nervice.py` (text), `python voice.py` (push-to-talk),
`python wake.py` (wake word; `--setup` downloads the model).

Auth: every data route `Depends(auth)` — Bearer token, constant-time compare, 401 otherwise.
Static pages (/, /v2, /v3) are unauthenticated but inert without a token.

| Route | Purpose |
|---|---|
| GET / , /v2 , /v3 | static clients (v1/v2 legacy, v3 active HUD) |
| GET /health | status + voice warm state + STT/TTS strings |
| GET /weather | current + 7-day forecast for stored location, incl. `place` |
| POST /location, GET /location | phone GPS store (lat/lon -> reverse-geocoded name) |
| GET /system | psutil+nvidia-smi telemetry (CPU/RAM/GPU/disk/net/uptime/procs) |
| GET /updates | last 8 git commits {date, summary} (changelog panel) |
| GET /last-turn | most recent turns.log entry {rung, route, seconds, path} |
| GET /nervice-stats | memory count, messages, today count, voice engine |
| GET /news | BBC RSS headlines, 10-min cache |
| GET /activity | merged feed: computer_actions.log + claude_calls.log + DB messages |
| POST /chat | one text turn (respond()) |
| POST /voice | audio upload -> STT -> respond -> WAV base64 reply |
| WS /ws/chat | streamed text turn; token in FIRST frame, else close 1008 |
| WS /ws/voice | audio frames -> STT -> stream_reply (text+audio frames) |

WS frame types: ready / transcript / ack / text / audio / done.

## 3. BRAIN LADDER

Models: Groq `llama-3.3-70b-versatile` (chat + verify), Groq `openai/gpt-oss-120b` (tool loop +
token streaming), Ollama `qwen3.5:4b` (local rung; think:false, keep_alive:-1 enforced in
app/ollama_client.py), Claude via Agent SDK accounts `CLAUDE_ACCOUNTS = [("pro",
~/.claude-nervice), ("max", ~/.claude-nervice-max)]` (agent.py; Max marked temporary).

Turn flow (respond() in chat.py / stream_reply() in streaming.py, same gates):
1. computer.resolve_pending — a yes/no answers a pending risky-action ask.
2. skills.handle — saved trigger runs a skill; create/list/delete handled.
3. music.handle — favorite-artists management (deterministic).
4. sysinfo fast path — router.is_machine_question() -> sysinfo.answer_machine_question(),
   ~1.7s, ZERO LLM (`route=system rung=direct`).
5. build_system_prompt + router.classify in parallel -> execute_route.

router.classify: Groq chat_json with ROUTER_SYSTEM -> routes hard/build/selfmod/browse/control/
normal. Deterministic pre-guards: _FOLLOWUP complaints -> normal; machine questions -> normal.
On Groq failure: ollama.chat_json same prompt -> on failure _keyword_route() (regexes built from
computer.py's own verb/risk vocabularies; play/open/screenshot/risky -> control). Control never
silently dies.

Cap detection: groq.RateLimitError caught (no header probing); NOTE the residue effect — tiny
probes can pass while real ~3k-token turns 429 when daily TPD remainder is small. 429 handlers
(llm.chat_with_tools, streaming 429 branch, _grounded_synthesis):
  news? -> extractive headlines (zero LLM, rung=extractive)
  -> llm._ollama_fallback (same system+window; local tool subset get_weather/get_system_info/
     get_top_processes/count_files; 3-round tool loop; rung=ollama; broad-catch -> ladders on)
  -> llm._claude_fallback -> agent.ask_claude ladder pro->max (compose_claude_prompt renders the
     window into one prompt; context preserved)
  -> LADDER_EXHAUSTED_MSG (+ "start Ollama" hint if local rung is down).
Hard route (consult_claude) always goes straight to Claude; grounded web synthesis falls back to
Claude only (4B never paraphrases sources). Builder/browse/selfmod agents are pro-only, not laddered.
Telemetry: every turn appends `route=X rung=groq|ollama|claude-pro|claude-max|direct|extractive|
exhausted|control|skill|music 1.23s path=rest|ws` to logs/turns.log (app/turnlog.py).

## 4. MEMORY

Supabase Postgres + pgvector via asyncpg (DATABASE_URL; pool_pre_ping; 60s keepalive in api.py).
Tables (app/models.py):
- `memories`: id uuid PK, user_id, content, category (identity/preference/project/relationship/
  goal/fact), salience smallint 1-5, embedding Vector(768), source_conv_id, source_snippet,
  is_active bool, superseded_by FK(memories.id), created_at/updated_at, access_count.
- `messages`: id, user_id, conversation_id, role, content, created_at.

Embeddings: `nomic-embed-text` (768-dim) via LOCAL Ollama (app/embeddings.py).
Recall (app/retrieval.py): CORE = all active salience>=4 (always injected, recency-ordered, cap 15)
+ TOPIC = cosine top-8 of salience<4. Injected by chat.build_system_prompt as a "WHAT YOU KNOW
ABOUT NATE" block in the system prompt.
Extraction (app/memory.py): on save_exchange, chat_json(RECONCILE_SYSTEM) emits ops
add/update/supersede vs the 10 nearest existing memories; _validate_op schema-gates; supersede
deactivates the old row (superseded_by link). Captures identity/preferences/projects/ideas/
decisions/goals (widened); "significant only" guard.
Capped quarantine: on Groq 429 -> ollama.chat_json same prompt; ops salience-capped <=3 (never
enters CORE) and logged to data/memory_requeue.jsonl kind=verify (extractor tagged); if both rungs
fail, raw exchange queued kind=deferred. NOTE: no batch re-verification job exists yet — the
requeue file accumulates until one is built.

## 5. VOICE

STT: faster-whisper `base.en`, CUDA float16 (CPU int8 fallback), loaded at module import
(app/voice.py); transcribe_file() decodes webm/opus via PyAV — no ffmpeg binary.
TTS: Kokoro ONNX (kokoro-v1.0.onnx) on the CUDA EP, voice `bm_george` (British), 24kHz, en-gb
phonemization; Piper en_US-ryan-high is the fallback engine if kokoro-onnx is missing. ~0.8-1s
per sentence on GPU. CUDA DLLs registered from pip nvidia-* packages (_register_cuda_dlls).
Server-side delivery: WS streaming path synthesizes per sentence via _AudioPipeline (serialized
worker, first-sentence holdback until a second sentence exists); complete-reply/fallback path is
also sentence-pipelined; TEXT frames are sent paired right before each sentence's AUDIO frame
(transcript reveals in sync with speech; `done` carries the full reply as client safeguard).
Junk-transcript gate (voice paths only): is_junk_transcript — empty / <3 chars (non-command) /
whisper hallucination phrases ("thank you", "thanks for watching"...) / all-filler utterances;
whitelist of real short commands ("yes","stop","open"...). Junk -> spoken "Didn't catch that",
no turn, no memory.
Wake word (wake.py): openWakeWord, ONNX-only backend on the existing onnxruntime-gpu; model
models/wakeword/hey_jarvis.onnx (bundled "Hey Jarvis"; loader prefers any *nervice*.onnx if a
custom model is dropped in; training Colab documented in models/wakeword/README). Detection
threshold 0.5, 1280-sample frames, ~6% of one CPU core. Sleep words constant (stand by/goodbye/
go to sleep/...); FOLLOWUP window 6s; falls back to the plain voice loop if no model present.
Client VAD (index_v3.html, conversation mode): adaptive — 20th-percentile calibration of first
500ms; floor follows drops instantly, rises 0.02/tick idle / 0.002 during speech; speech =
sustained >=150ms above max(floor*3.5, floor+0.015); silence budget accumulates below
max(floor*2.0, floor+0.008), dead zone counts 0.4x, never hard-resets; END_SIL 1350ms,
MIN_SPEECH 400ms, NO_SPEECH 8s, hard cap 30s; setTimeout(50ms) loop (not rAF); countdown label.
Push-to-talk is tap-to-toggle (no VAD).

## 6. COMPUTER CONTROL (app/computer.py)

Closed action set (the ENTIRE physical surface; no freeform shell):
- open_app(name) — WHITELIST dict (notepad, calc, explorer, paint, chrome, brave, edge, firefox,
  spotify, vscode); non-whitelisted = RISKY -> confirmation gate.
- open_url(url, browser) — SITES dict + bare domains; default browser BROWSER="brave" with
  fallback brave->chrome->OS default; a NAMED browser is honored or reported missing.
- play_youtube(query) — yt-dlp ytsearch1 resolves a real video id+title; opens watch URL
  (auto-plays); bare "play music/it/something" picks a random artist from app/music.py favorites
  (data/music_favorites.json, set by voice); empty list -> asks. Honest failure on resolve/launch.
- screenshot() — PIL ImageGrab full screen -> screenshots/shot-<ts>.png.
- list_windows() — ctypes EnumWindows visible titled windows.
- focus_window(substr) — EnumWindows match -> SW_RESTORE + SetForegroundWindow.

interpret(message) -> Decision(action, target, risk, supported): deterministic regexes, NO model.
Order: screenshot -> list_windows -> _RISKY_WORDS (delete/close/send/buy/install/shell tokens...
-> unsupported, one honest refusal quoting _CAPS) -> play -> switch/focus -> open verbs (URL/site
safe; whitelisted app safe; unknown app risky-but-doable) -> unparseable -> honest question.
Risky gate: _pending dict per user, 300s TTL; resolve_pending() consumes yes/no BEFORE routing;
yes executes, no cancels, anything else abandons. Audit: every decision/exec appended to
logs/computer_actions.log (ASK/CONFIRM-EXEC/CANCEL/ABANDON/REFUSE/EXEC/PLAY/BROWSER/SKILL-*).
Launch honesty: _spawn (no shell) + tasklist polling (_running) -> "ok"/"unverified"/"fail";
never claims success without a confirmed process.

Skills (app/skills.py): data/skills.json, each {trigger, steps[{action,arg}], created}. Closed
step set: open_app/open_url/youtube/weather/news ONLY. Create by voice -> chat_json parse ->
confirm -> save on yes; unsupported steps reported, never stored. Run: exact-normalized trigger
match pre-router; steps re-gated at run (non-whitelisted app skipped, urls forced https). Create
detection requires teach-cue AND action verb. list/delete by phrase. Runs audited as SKILL-RUN.

## 7. SYSINFO (app/sysinfo.py — read-only)

- system_telemetry(): psutil per-core CPU (0.15s sample), RAM, swap, disk C:, net up/down rates
  (delta tracking, null on first poll), uptime, process count + gpu_telemetry() (nvidia-smi CSV:
  util, VRAM used/total, temp, name; {available:false} if unreachable). Shared by /system + tools.
- get_system_info(): one-paragraph summary (CPU name from registry, cores, %, RAM, GPU, disk, OS).
- get_top_processes(by=memory|cpu, n): psutil process_iter, never kills.
- count_files(path): os.scandir walk, names only, ~4s/300k cap, resolves "Downloads" under home.
- answer_machine_question(text): deterministic dispatcher -> natural one-liner templates (CPU/GPU/
  RAM/disk/OS/uptime/specs/top-proc/file-count); None when unsure (falls through to LLM).
Registered as Groq tools (tools.py): get_system_info/get_top_processes/count_files + get_news.

## 8. HUD (/v3)

File: app/static/index_v3.html (single file, ZERO external resources; canvas-drawn bg).
Feeds: /system (2s), /health (15s), /nervice-stats (30s), /activity (30s), /weather (10min),
/news (10min), /updates (5min — the changelog panel reads REAL `git log -8` summaries),
/last-turn (4s + after each done frame -> "BRAIN Ollama (local) · LAST 9.8s" strip + per-reply
meta line), /location (POST on load/geolocation button). Voice: /ws/voice; text: /ws/chat.
Layout: 3-col desktop (system+activity+updates | clock/reactor/spectrum/convo | weather+core+news),
single column mobile. Theme: purple #b07cff = identity/structure, cyan #19e3e3 = live data; one
hero gradient on the reactor (coreGrad/reactorGrad) + per-gauge sweeps. Reactor states: idle
purple breathe / listening cyan / thinking violet shimmer / speaking warm pulse; amplitude
clamped (min(var(--amp),1), core <=1.12) + overflow circle clip.

## 9. SAFETY LAYER

- SAFETY_FLOOR (app/safety.py): one-line refusal floor; appended to PERSONA at import
  (persona.py) so a persona self-edit cannot drop it.
- Self-mod gate (app/selfmod.py): propose() runs a Claude agent that emits a unified diff ->
  fail-closed PATH ALLOWLIST (selfmod cannot touch .env, safety.py, selfmod.py itself, etc.),
  proposal record + .patch written to proposals/. apply() requires explicit human approval
  (nervice.py REPL: approve <id>): clean-tree check, git apply --check dry-run, apply, py_compile
  every changed file, then hard-asserts `SAFETY_FLOOR in PERSONA` imports cleanly; ANY failure
  rolls back. reject() marks rejected. Nothing self-applies.
- Builder jail (agent.py agent_task): cwd jailed to ~/nervice-workspace; allowed_tools = Read/
  Write/Edit/Glob/Grep + Bash allowlist (git/node/npm/python/pip/ls/...; Claude Code parses shell
  operators so chains are matched per-subcommand); no remotes/push; secrets scrubbed from env
  (_SCRUB_KEYS incl. GROQ_API_KEY, DATABASE_URL, CLAUDE_CODE_OAUTH_TOKEN); token-free auth from
  CONFIG_DIR stored creds.
- Browse jail (agent.py browse_agent): Playwright MCP server only (`@playwright/mcp@latest
  --isolated`), strict_mcp_config=True, allowed_tools mcp__playwright__* only; no login/identity
  actions per system prompt.
- Control boundary (computer.py): closed action list + deterministic interpret + risky
  confirmation gate + audit (section 6).
- WS auth: token in first frame else close 1008. API: Bearer on every data route.

## 10. SCREEN-CONTROL READINESS

Already present and reusable for a "hands" build:
- **pywin32 312** installed (win32api/win32gui available; currently unused — window ops use ctypes).
- **Pillow 12.2.0** — ImageGrab already takes full-screen screenshots (computer.screenshot()).
- **ctypes window management** — EnumWindows/SetForegroundWindow/ShowWindow working
  (list_windows/focus_window).
- **Playwright via MCP** — used ONLY inside the jailed Claude browse agent (npx @playwright/mcp,
  isolated); NOT importable as a Python lib (not in pip list); the visible-browser automation
  path exists but is agent-mediated, not direct.
- **tasklist polling** for process verification (_running()).
- MISSING: pyautogui/mss/uiautomation/pygetwindow — no direct mouse/keyboard synthesis lib is
  installed. No coordinate clicking, no key events, no OCR. The Claude-in-Chrome/computer-use
  style MCP tooling is not wired into the app itself.
- Audit + confirmation-gate plumbing (computer.py) is the natural extension point for any new
  acting capability.

## 11. CONFIG + DEPS

Env var NAMES (.env; values never printed): NERVICE_API_TOKEN, GROQ_API_KEY, DATABASE_URL,
DATABASE_URL_MIGRATIONS, CLAUDE_CODE_OAUTH_TOKEN (scrubbed from agent env at call time).
Optional referenced: PICOVOICE_ACCESS_KEY (legacy, no longer used).
Key deps (requirements.txt): fastapi/uvicorn, sqlalchemy/alembic/asyncpg/pgvector, groq, ollama,
claude-agent-sdk, faster-whisper, kokoro-onnx + onnxruntime-gpu + nvidia-* CUDA wheels,
piper-tts (fallback), sounddevice, webrtcvad-wheels, openwakeword (+scikit-learn/scipy),
ddgs + trafilatura (web search), httpx + truststore (Norton TLS), psutil, yt-dlp, python-dotenv.
External services touched: Groq API, Supabase Postgres, local Ollama (127.0.0.1:11434), Anthropic
via Claude Agent SDK (two local CONFIG_DIRs), Open-Meteo, BigDataCloud reverse geocode, BBC RSS,
DuckDuckGo search, YouTube (yt-dlp search), Tailscale (network layer only).

## 12. GIT HISTORY (last 25)

```
d69e040 ui(v3): VAD defeats steady background noise — fan/AC no longer blocks end-of-speech (Fix 3)
85fe7ea streaming: text reveals in sync with the voice, not before it (Fix 2)
46ede9c music: voice-set favorite artists drive "play some music" (Fix 1)
d212b65 memory: capped extraction runs on the local rung with quarantine (Fix 5 / Phase 2)
2c9a5a9 persona: stop self-narration drift (Fix 4)
fc92199 streaming: sentence-pipeline TTS on the complete-reply path (Fix 3 — ~2.8s faster capped voice)
fabe329 computer: real "play <X> on YouTube" capability (Fix 1)
8169890 hud: RECENT UPDATES panel + live brain/response-time readout (all real telemetry)
46e4a63 ui(v3): adaptive VAD — fix conversation mode recording forever
8c89ed0 router: classify through local Ollama when Groq is capped, keywords as the final net (Phase 1.3)
299f42e ladder: Groq 429 -> local Ollama FIRST, Claude only after (Phase 1.2)
48ff2c5 ollama: hardened local client for the qwen3.5:4b rung (Phase 1.1)
fb09339 news: capped state answers extractively from REAL headlines — zero LLM, zero fabrication (Phase 0.3)
bc170c6 sysinfo: direct-answer fast path — machine questions in ~1.7s with ZERO LLM calls (Phase 0.2)
7488988 router: deterministic keyword fallback when the LLM router is unavailable (Phase 0.1)
4d32f1b persona: SYSTEM FACTS explicitly denies email/text/post/account tools (diag finding)
2733e78 ui(v3): fix the REAL orb-overdrive cause — state rules were FILLING the open amp-arc path
e8760db ui(v3): clamp the orb overdrive + deliberate purple/cyan two-tone theme
a213467 sysinfo: read-only machine + self awareness — answer "what CPU/RAM/files/process/model" with REAL data
30e3b88 wake: document the recommended free custom "Hey Nervice" path (openWakeWord training Colab)
9b28385 wake: swap Porcupine -> openWakeWord (free, no account/key); ship bundled "hey jarvis"
8414a3f wake: "Hey Nervice" wake-word + sleep-word for the local always-on loop (Porcupine, on-device)
06fb672 skills: user-defined command chains composing existing safe actions only (Build B)
4e04f5f weather: real GPS location (POST /location) replacing hardcoded Oswego coords (Build A)
723534d ui(v3): tap-to-talk, smooth warm speaking pulse, better VAD endpointing
```

## 13. KNOWN ISSUES (blunt; none fixed in this audit)

1. **Recurring stale-server trap.** run_api.py holds old modules; multiple sessions found the
   live server missing recent commits. No auto-reload, no version banner mismatch warning.
2. **No memory re-verification job.** data/memory_requeue.jsonl accumulates verify/deferred
   entries; nothing consumes it. Local-extracted (quarantined) memories stay salience<=3 forever.
3. **Groq cap detection is reactive and lumpy.** The residue effect (1-token probes pass while
   real turns 429) means /health-style probes can't see the capped state; each capped turn pays
   a failed Groq round-trip before laddering. No sticky cap-state with reset-time parsing
   (planned Phase 3, unbuilt).
4. **handle_control + yt-dlp resolution run synchronously on the event loop** (tasklist polls
   ~1s, yt-dlp search 1-3s) — blocks other requests briefly on a single-user server. Pre-existing
   pattern, now slightly heavier with play_youtube.
5. **Ollama rung latency on real turns is 7-17s** (3k-token persona+memories prefill on a 4B +
   tool round + TTS). Expected, not a bug; trimming the local-rung system prompt is an unbuilt
   quality/latency tradeoff.
6. **4 stale selfmod proposals pending** in proposals/ (incl. throwaway-looking ones like "/quit"
   already rejected; pending ones from 06-09/06-10 were never approved/rejected).
7. **Voice-timing telemetry ([voice-timing] stt/llm/tts) prints to server stderr only** — not in
   a file, so per-stage latency can't be audited after the fact (turns.log covers the LLM span only).
8. **wake.py imports the root voice.py REPL as its fallback** (`import voice as _voiceloop`) —
   name collision with app/voice.py is avoided only by import order; fragile if run from another cwd.
9. **index_v3.html is 885 lines of single-file JS/CSS** — no tests beyond preview probes; VAD
   logic is only verifiable via simulated-energy harnesses.
10. **The legacy v1/v2 clients and piper fallback are untested dead weight** kept for safety;
    index.html still references the oldest API shapes.
11. **PICOVOICE_ACCESS_KEY doc lines remain** in wake-word docs though Porcupine was removed.
12. **scripts/ contains ~35 throwaway test/probe files** with overlapping coverage and no runner;
    several (probe_capture.txt, _bench_ollama.py) are artifacts, not tests.
13. **CLAUDE_CODE_OAUTH_TOKEN lives in .env** and is scrubbed at agent-call time; if any new
    subprocess path skips _SCRUB_KEYS it would leak into child envs. All current paths scrub.
```
