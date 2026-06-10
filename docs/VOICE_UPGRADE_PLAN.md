# Nervice Voice Upgrade — Audit & Phased Plan

*2026-06-10 · audit-only task: no feature code written. All numbers measured live on this box (RTX 4060 Laptop 8GB, warm server, Groq, Supabase, ollama).*

---

## 1. Current architecture (as read, not as remembered)

**Request pipeline.** Every face (REPL [nervice.py](../nervice.py), local voice loop [voice.py](../voice.py), API [app/api.py](../app/api.py)) funnels into `respond()` in [app/chat.py](../app/chat.py): `build_system_prompt` (persona + two-tier memory retrieval + clock) → `classify()` router LLM hop ([app/router.py](../app/router.py), llama-3.3-70b JSON) → `chat_with_tools()` ([app/llm.py](../app/llm.py), gpt-oss-120b, max 4 rounds). Forced routes (hard/build/selfmod/browse) get a spoken ack + per-tool timeout (180–600s). If `web_search` fired, the tool-loop's own answer is **discarded** and `_grounded_synthesis` re-answers from sources (gpt-oss synth → llama-3.3 cross-model verifier, `CLAIMS:`/`FINAL:` format). Build/selfmod/browse results are terminal (returned verbatim, no chat rewrite).

**Memory loop.** `save_exchange` (fire-and-forget per turn) writes both Messages and runs `remember()` ([app/memory.py](../app/memory.py)): embed exchange (ollama nomic-embed-text) → top-10 related → LLM reconcile (add/update/supersede, validated ops) → pgvector on Supabase. Retrieval ([app/retrieval.py](../app/retrieval.py)) = core (salience≥4) + topic (cosine).

**Agents & jails** ([app/agent.py](../app/agent.py)): `ask_claude` (no tools, isolated CLAUDE_CONFIG_DIR), `agent_task` (builder: workspace cwd, fail-closed Bash allowlist, secret scrub `_SCRUB_KEYS`, no remotes), `browse_agent` (Playwright MCP, isolated profile, `tools=[]`, JS-eval tools disallowed, injection-hardened prompt), `propose_agent` (read-only, staged copies of `EDITABLE` only). The selfmod gate ([app/selfmod.py](../app/selfmod.py)) is code-enforced: path allowlist, clean tree, git-apply exact, py_compile, safety-floor assertion, rollback, human approval. **None of this is touched by this plan.**

**Voice path** ([app/voice.py](../app/voice.py)): faster-whisper base.en CUDA fp16 + Kokoro ONNX on CUDA EP (am_onyx), webrtcvad mic gating locally; `speak()` already does sentence-pipelined gapless playback **locally**. The API path instead uses `synth_to_pcm` — renders the whole reply, returns one base64 WAV inside JSON.

**API & phone** ([app/api.py](../app/api.py), [app/static/index.html](../app/static/index.html)): bearer-token (constant-time) on every route, static client unauthenticated-but-inert, tailnet-only. Phone: MediaRecorder → POST blob → wait → play one WAV. Conversation mode = client RMS VAD (1.2s trailing silence). iOS autoplay unlocked via persistent `<audio>` element.

**Relevant skills in the catalog** (~/.claude/skills): no dedicated realtime-audio/voice-agent skill exists. Useful for this work: `fastapi-patterns` (WS endpoints), `frontend-design` + `make-interfaces-feel-better` + `motion-foundations/patterns` (UI overhaul — adapt React guidance to vanilla), `webapp-testing`/`browser-qa` (client verification), `council` (used in this audit), `verification-before-completion`, `e2e-testing`, `systematic-debugging`.

---

## 2. Measured latency — where every second goes

Throwaway probes (now deleted): per-Groq-call spans via client wrap, component timings, one live `/voice` e2e.

### Typical **normal** voice turn (phone → first audio)

| Span | Measured | Notes |
|---|---|---|
| Client trailing-silence wait | **1.2 s** (convo mode) / 0 (hold-to-talk) | `SILENCE_MS=1200` in index.html |
| Blob upload (tailnet) | ~0.1–0.3 s | ~20–50 KB opus |
| STT (whisper GPU, warm) | **0.41 s** (3.5 s audio) | |
| Memory retrieve (ollama embed + Supabase ×2) | **0.31 s warm / 2.1–2.3 s after idle** | pool reconnect; embed itself 0.13 s |
| Router classify (llama-3.3-70b) | **0.29–0.79 s** | serial, before generation |
| LLM generate (gpt-oss-120b + tools, non-streaming) | **0.40–0.90 s** short replies; ~2 s longer | TTFT when streamed: **0.63 s** (content, tools attached) |
| TTS full reply before send | **~1.0 s/sentence** (3-sentence reply = 3.03 s) | RTF ≈ 0.35; fixed ~1 s overhead per call |
| JSON download (base64 WAV) | 0.1–1 s | **469 KB for a 25-word reply**; 623 KB for 3 sentences |
| **Measured e2e** (localhost, short reply) | **3.67 s** | phone realistic: **4–8 s** to first audio |

### **Grounded** turn (news question, measured live): **39.1 s**
classify 0.25 + tool-pick 0.45 + web_search **8.9** (3 page fetches serial, 8 s timeout each) + **5.2 wasted** (tool-loop draft that `chat_with_tools` then discards) + synthesis **20.8** (gpt-oss over ~15 KB sources, 282-word output) + verify **3.0**. Reply was **282 words in voice mode** — the 2–4-sentence cap never reaches `_grounded_synthesis`.

Control measurements: same synth call with small sources = 1.1 s (default) / **0.71 s with `reasoning_effort="low"`** → the 20.8 s is source-size + output-length driven, fully fixable. 8b-instant classifies correctly in 0.35–0.43 s (and got `build` right).

### Architectural blockers to streaming (specific)
1. `respond()` awaits the **complete** reply; nothing in the request path streams ([app/chat.py:59-69](../app/chat.py)). `chat_stream` exists but only `greeting()` uses it.
2. Router classify is a **serial LLM hop** before generation ([app/chat.py:51](../app/chat.py)).
3. `chat_with_tools` uses non-streaming `create()` even when no tool fires ([app/llm.py:81](../app/llm.py)).
4. Grounded path: synth and verify are two more **serial, full** completions; verifier needs the whole draft; plus the discarded round-1 draft ([app/llm.py:146-149](../app/llm.py)).
5. `/voice` renders **all** sentences to one WAV, then base64s it into one JSON ([app/api.py:172-184](../app/api.py)) — first byte of audio waits for the last sentence.
6. HTTP request/response only — no server push, no interrupt channel; client uploads one blob only after its own 1.2 s silence wait.
7. Forced-route **ack never reaches the phone** — `respond()` is called without `speak` in the API ([app/api.py:165](../app/api.py)); ack only prints to server stdout. Phone user sits in silence up to the 600 s build timeout.

### Other audit findings (fix in passing)
- **Auto-fired `consult_claude` has no timeout** in the chat path (`_TIMEOUTS` covers forced routes only) — a hung call wedges an API turn indefinitely; phone fetch has no client timeout either.
- Supabase pool idle-reconnect (~2 s) hits real usage constantly (minutes between voice turns) — `pool_pre_ping=True` + `pool_recycle` on the engine ([app/db.py](../app/db.py)) is a one-liner.
- `web_search` fetches 3 pages **serially** (up to 8 s timeout each) ([app/tools.py:15-47](../app/tools.py)).
- In-process conversation windows lost on restart — accepted/documented, no change.

---

## 3. Track f — the weather/reply-length bug, diagnosed

Two distinct bugs, both confirmed live:

**(1) Re-asks for location.** `get_weather()` with hardcoded Oswego coords exists in [app/weather.py](../app/weather.py) but is **only called by `greeting()`** — it is not a tool. When Nate asks "what's the weather like?", the router says `normal`, gpt-oss has no weather tool and no location memory, so it answers: *"just need to know where you want the forecast. Which city or region?"* (measured, 1.77 s). Fix: expose `get_weather` as a real tool (`TOOLS`/`TOOL_FUNCS` in [app/tools.py](../app/tools.py)) — instant, keyless, returns the compact string. Optionally a router/keyword fast-path so weather never even hits the tool loop.

**(2) Far-too-long replies.** Any `web_search` turn re-answers via `_grounded_synthesis`, whose `SYNTH_SYSTEM`/`VERIFY_SYSTEM` messages are built fresh — **`VOICE_ADDENDUM` (2–4 sentence cap) never reaches them** ([app/llm.py:38-56](../app/llm.py) vs [app/chat.py:24-27](../app/chat.py)). A voice weather/news question that triggers search gets a 282-word essay. Fix: thread `voice_mode` into `chat_with_tools`/`_grounded_synthesis` and append a hard brevity clause to the synth + verify prompts. (Weather itself stops hitting this path once it's a tool, but the bug bites every news/current-events voice turn.)

---

## 4. Council verdict (skeptic / pragmatist / critic vs. architect)

- **Consensus:** ship the free latency fixes first, independent of any streaming work; never speak tokens until the **first sentence boundary** resolves with no tool-call delta (prose-before-tool-call is common, not an edge case); iOS `echoCancellation` is a hint, not a guarantee → **tap-to-interrupt is the barge-in baseline**, VAD barge-in is an enhancement.
- **Biggest disagreement:** skeptic called the WebSocket over-engineered (SSE + pipelining gets most of the latency win) — but conceded barge-in genuinely needs a duplex channel. Since barge-in is a goal, WS stands; the skeptic's sequencing point is accepted: the protocol ships minimal (audio out + interrupt in), not a grand redesign.
- **Premise check:** skeptic challenged the FINAL:-marker stream parse → resolved: anchored parse (line-start `FINAL:` after a seen `CLAIMS:` section), and on any miss fall back to exactly today's behavior (wait for full output, use draft on format failure). Failure mode = slower, never less-verified speech.
- **Router:** critic warned a diverged voice path will silently mis-route "browse that" → resolved by **parallel speculation** (below) instead of skipping classification.
- **Critic's catch nobody else had:** Whisper STT and Kokoro TTS share the 4060 — under barge-in + rapid turns, STT can queue behind TTS and the 1.5 s budget silently doubles. Mitigations: interrupt cancels TTS synthesis *first* (frees the GPU before the new turn's STT), and Phase 1 exit criteria include STT-under-concurrent-TTS measurement.

---

## 5. The plan — ranked phases

> Per-track keys: **(a)** streaming **(b)** barge-in **(c)** latency **(d)** voice quality **(e)** UI **(f)** weather/length bugs.
> Hard invariants for every phase: bearer auth on every channel (WS included), tailnet-only, no public bind; agent jails, secret scrub, safety floor, selfmod gate, EDITABLE allowlist untouched; verifier still gates every spoken specific from web material.

### Phase 0 — "Free seconds" (tracks c+f; ~half a day; zero new architecture, all reversible)
1. **Weather tool** — add `get_weather` to TOOLS; description says "Nate's local weather, no location needed". Files: [app/tools.py](../app/tools.py). Fixes f(1). *Feel: weather turns 1.8 s → instant correct answer.*
2. **Voice-aware grounded synthesis** — thread `voice_mode` through `respond → chat_with_tools → _grounded_synthesis`; brevity clause in synth+verify prompts; `reasoning_effort="low"` on the synth call; cap source material per voice turn (e.g. 2 fetched pages, 1.5 KB each). Files: [app/chat.py](../app/chat.py), [app/llm.py](../app/llm.py). Fixes f(2). *Measured headroom: synth 20.8 s → 1–3 s.*
3. **Drop the discarded draft round** — when `web_search` fired and grounded synthesis will run, don't ask the tool model for another full completion first. Files: [app/llm.py](../app/llm.py). *−5.2 s on every grounded turn.*
4. **Parallel page fetches** in `web_search` (httpx async or thread pool). Files: [app/tools.py](../app/tools.py). *8.9 s → ~3–4 s.*
5. **DB pool keepalive** — `pool_pre_ping=True`, `pool_recycle=300` in [app/db.py](../app/db.py). *−2 s on most real turns.*
6. **Parallelize retrieve + classify** with `asyncio.gather` in `respond()`. Files: [app/chat.py](../app/chat.py). *−0.3–0.8 s.*
7. **Timeout auto-fired `consult_claude`** (wrap tool execution in `wait_for`, reuse `_TIMEOUTS["hard"]`). Files: [app/llm.py](../app/llm.py). Reliability, not latency.

**Phase 0 result:** normal turns ~4–8 s → **2.5–4 s**; grounded turns 39 s → **~8–12 s**, replies actually short. Risk: trivial. Cost: free (same API usage, less of it).

### Phase 1 — Streaming voice over WebSocket (track a; the core build; ~2–4 days)
**Protocol** (one WS endpoint, e.g. `/ws/voice`): client connects, **first frame is the bearer token** (never in the URL; constant-time compare; close on failure — same boundary as today). Then per turn: client streams mic chunks (MediaRecorder timeslice → binary frames) + `{end_of_speech}`; server replies with ordered events: `{transcript}` → `{ack}` (if slow route) → per sentence `{sentence, text}` + binary PCM/WAV frame → `{turn_done, full_text}`. Client may send `{interrupt}` anytime (Phase 2 uses it).
**Server engine:** new `app/stream.py` — a turn = async pipeline: STT on buffered audio → parallel(classify-speculation, see Phase 1b) → `chat.completions.create(stream=True, tools=...)` → **holdback buffer**: emit nothing until first sentence boundary; if a tool-call delta arrives first, discard buffer and divert to tool lane (existing `chat_with_tools` semantics, including forced-route timeouts) with an immediate `{ack}` event — this also finally delivers the slow-tool ack to the phone (blocker #7). Sentence chunks → Kokoro (existing `_synth`) → binary frames as each sentence finishes. Cancellation-safe: one `asyncio.Task` per turn, cancel scope covers generation+TTS only.
**Grounded lane:** ack immediately ("let me check — one sec"), run Phase-0-fast search+synth, then **stream the verifier**: anchored `FINAL:` parse, speak post-marker sentences as they emerge; any parse anomaly → wait-for-complete fallback (today's exact behavior). The verifier boundary is preserved byte-for-byte: nothing from web material is ever spoken pre-verification.
**Client:** Web Audio playback (decode each frame → schedule `AudioBufferSourceNode`s back-to-back; works on iOS Safari after the existing gesture unlock; no MSE needed). Keep the HTTP `/voice` endpoint as fallback so the old flow still works.
**Files:** new `app/stream.py`, [app/api.py](../app/api.py) (WS route), [app/voice.py](../app/voice.py) (chunk-synth helper), [app/static/index.html](../app/static/index.html), [app/llm.py](../app/llm.py) (streaming tool-loop entry).
**Safety not broken:** auth (first-frame token), grounding (verifier still gates), jails (untouched — tool lane calls the same functions), selfmod (untouched).
**Feel:** first audio after speech end — **typical 2.0–2.5 s, best ~1.5 s** (STT 0.5 + retrieve 0.3 + TTFT 0.6 + first-sentence gen ~0.3 + TTS 0.7–1.0 + transport ~0.1). Honest note: **consistently <1.5 s needs Phase 3's endpointing**; this phase gets you "feels immediate", not yet "feels instant". Grounded turns *feel* fast because the ack lands in ~1.5 s and verified sentences start ~6–10 s in.
**Risk:** medium — iOS WS lifecycle (background/screen-lock kills sockets; need reconnect logic), GPU contention (exit criterion: measure STT while TTS streams), sentence-split edge cases (reuse `_clean_for_speech` + `_SENT_SPLIT`). Cost: free (same Groq usage; streaming costs nothing extra).

### Phase 1b — Router despecialization for voice (track c; rides inside Phase 1)
Don't skip classification — **race it**: fire classify (switch to `llama-3.1-8b-instant`, measured 0.35–0.43 s and correct on test cases) and the speculative fast-lane stream simultaneously. If classify returns a forced route before the holdback releases, discard speculation and run the forced path (ack + timeout as today). A 5-line keyword pre-gate ("build", "browse", "open ", "change your", "yourself", "claude") decides whether classify is even needed — pure-chat turns skip the hop entirely. *Zero added latency on every path; routing fidelity preserved (the critic's mis-route warning addressed). Wasted speculative tokens on rare forced turns: negligible on Groq.* Files: [app/router.py](../app/router.py), `app/stream.py`.

### Phase 2 — Barge-in (track b; ~1–2 days on top of Phase 1)
**Step 1 (reliable core): tap-to-interrupt.** The orb/screen is a giant button during playback → `{interrupt}` → server cancels the turn task (generation+TTS only), client flushes scheduled Web Audio buffers, recording starts. Dead simple, no false positives, ships first.
**Step 2 (hands-free): VAD barge-in.** Mic stays open during playback (`echoCancellation:true`), require **sustained** speech (~250–300 ms above an adaptive RMS floor measured during playback) before triggering; tune on the actual iPhone; auto-disable VAD-barge-in (fall back to tap) if self-interrupt rate is high — council unanimously expects device-specific AEC flakiness.
**Truth-in-window:** server tracks which sentences were actually sent/confirmed spoken; on interrupt, the assistant window entry is the **spoken prefix + " — [cut off by Nate]"**, and `save_exchange` stores the same. Keeps memory and context honest about what Nate actually heard.
**Cancellation safety:** interrupt cancels the *speech pipeline*. Forced-route agent work (build/selfmod/browse/hard) is **not** cancelled mid-flight — the turn task detaches agent awaits (they keep their own timeouts; selfmod proposals complete and wait for approval as ever); only their eventual spoken delivery is dropped, with a `{notice}` event so the UI can show "build still running…". Interrupt-vs-done race: turn tasks are id'd; `{interrupt, turn_id}` for a finished id is a no-op ack.
**Files:** `app/stream.py`, index.html. **Safety:** none of the cancelled scopes touch jails/gate; agent side effects can't be half-killed. **Feel:** this is the single biggest "it's alive" feature. **Risk:** medium (AEC tuning); mitigated by tap fallback. Free.

### Phase 3 — Sub-1.5s polish + endpointing (track c stretch; ~1 day)
- **Server-side endpointing:** server runs webrtcvad on the live mic stream and declares end-of-speech itself (~400–600 ms trailing) instead of the client's 1.2 s — cuts 0.6–0.8 s and removes the client/server double-wait.
- **Early STT:** transcribe rolling chunks while Nate is still mid-utterance (whisper on 2–3 s windows), final pass on stop — shaves most of the 0.4–0.6 s STT to near-zero perceived.
- **Speculative prompt build:** kick retrieval as soon as a partial transcript exists.
- **First-clause TTS:** synthesize the first comma-clause (~0.5–0.7 s) instead of the full first sentence.
*Combined: typical first-audio ~1.2–1.8 s → target met. Risk: low-medium (incremental-STT correctness); each knob independent and revertible.*

### Phase 4 — UI overhaul (track e; ~2–3 days; can start in parallel with Phase 2)
Direction: **one self-contained dark file, zero CDN, tailnet+token unchanged** — but built around a central **audio-reactive orb** instead of a mic button. States: idle (slow breathing gradient), listening (live waveform ring fed by the mic AnalyserNode), thinking (orbiting shimmer + elapsed hint; shows the grounded-lane ack text), speaking (orb pulses driven by the *playing* audio's amplitude — we have the PCM, compute per-frame RMS server-side or analyze client-side), interrupted (snap animation). Canvas-based (60 fps, no DOM thrash), `prefers-reduced-motion` respected. Conversation view: streaming transcript bubbles that fill **sentence-by-sentence as spoken** (we have per-sentence events — sync text highlight to audio), day-grouped history via a small read-only authed `GET /history` (Messages table), pull-to-refresh, haptic-free (iOS PWA limitation) but with tactile-feeling spring animations. Tap-anywhere-on-orb = barge-in (Phase 2 affordance). Token modal, PWA manifest, safe-area handling all kept. Skills to lean on: `frontend-design`, `make-interfaces-feel-better`, `motion-foundations` (token/spring discipline, adapted to vanilla JS). *Risk: low — pure client; the WS protocol from Phase 1 already carries everything the UI needs.* Free.

### Track d — voice quality: **recommendation = stay on GPU Kokoro, tuned; park NeuTTS-GGUF**
Reasoning (decision delegated, so deciding): streaming + barge-in make **per-sentence synth latency the gating constraint** — Kokoro GPU is measured at ~1.0 s/sentence (RTF 0.35) and already integrated, warm, and stable on this box. NeuTTS-Air cloning sounds more human but: the GGUF/llama-cpp path is unproven on this machine (probe ran the torch-CPU variant; llama-cpp-python on Windows+CUDA is its own yak), the 0.5B backbone + codec would fight whisper+kokoro for the 8 GB GPU (the critic's contention warning, squared), and per-sentence latency is unmeasured here. Kokoro's robotic edge is mostly *delivery*, addressable free: (1) audition `BLEND` presets — the hook already exists ([app/voice.py:57](../app/voice.py)); try `("am_onyx",0.6,"am_michael",0.4)` and 2–3 others; (2) `speed≈1.06–1.1` (pairs with the pending tempo proposal `20260610-133819`); (3) feed TTS *cleaner text* — normalize numbers/units, strip parentheticals in `_clean_for_speech`. **Re-evaluate NeuTTS after Phase 3**: if a probe shows GGUF-on-GPU < 0.8 s/sentence warm without starving whisper, it becomes a swap-in behind the same `_synth` interface. Until then, naturalness comes from streaming prosody (short sentences, barge-in) more than from the vocoder.

---

## 6. What each phase delivers (pick-the-order summary)

| Phase | Delivers | First audio (typical) | Risk | Cost |
|---|---|---|---|---|
| **0** | Bugs f fixed, grounded 39→~10 s, every turn −2–3 s | 2.5–4 s | trivial | free |
| **1(+1b)** | Token→sentence→audio streaming over WS, phone finally hears acks, router raced not skipped | **2.0–2.5 s** | medium | free |
| **2** | Barge-in (tap, then VAD), honest interrupted-turn memory | — | medium | free |
| **3** | Server endpointing, early STT, first-clause TTS | **~1.2–1.8 s** | low-med | free |
| **4** | Orb UI, live waveform, spoken-sync transcript, history | — | low | free |
| **d** | Kokoro blend+tempo tuning now; NeuTTS gate after P3 | — | low | free |

Recommended order: **0 → 1(+1b) → 2 → 4 → 3** (UI before the last latency squeeze — feel improves more per day), with d's blend audition any idle afternoon. 0+1 alone turn "ask, wait, listen" into a conversation.

## 7. Open items for Nate
0. **Live finding during the audit:** at 13:49 a garbled voice transcript ("Have in yourself right now to declutter your space") routed straight into selfmod and produced junk proposal `20260610-134948` (cost $0.07). The gate held — it sits pending — but Phase 1's design should add a **spoken confirm before forced routes fire from a voice turn** ("You want me to change my own behavior — right?"). Cheap, and it also covers misheard "build/browse" commands. Probably reject that proposal.
1. Pending proposal `20260610-133819` (persona tempo) — approve/reject independently; Kokoro `speed` is the bigger tempo lever.
2. This doc + any new files must be committed (or stashed) before `approve <id>` — the selfmod gate requires a clean tree outside `proposals/`.
3. Phase 1 exit criteria to hold ourselves to: first-audio P50 ≤ 2.5 s on the phone, STT-under-TTS-load measured, WS reconnect after screen lock verified.
