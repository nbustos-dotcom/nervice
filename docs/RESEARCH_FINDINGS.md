# NERVICE — RESEARCH FINDINGS (Batch C, 2026-06-13)

Research-only document. **No code, no installs, no config changes.** It feeds future build
sessions: principles, honest tradeoffs, and free-first options for capabilities Nate has floated
(creator/news feeds, fireable monitoring agents, voice). Every external claim is linked at the
bottom. Where something is risky or not worth it, this says so plainly.

Nervice context this is written against (see `docs/PROJECT_STATE.md`): local-first, single-user,
Windows + RTX 4060 8 GB shared by STT/TTS/LLM, FastAPI on 127.0.0.1:8765 reached from the phone over
Tailscale, Groq→Ollama→Claude ladder, Supabase+pgvector memory, **faster-whisper + Kokoro + Silero**
voice (Silero added in this branch), a Claude Agent SDK already wired with **jailed** builder/browse
agents, a closed computer-control action set, a selfmod gate, and BBC RSS news already in `/news`.

---

## PART 1 — Relevant skills in the local catalog (`~/.claude/skills`, 426 skills)

Most of the 426 are generic stubs (`Agent skill for X — invoke with $agent-X`) or domain packs
(healthcare, SEO, logistics) irrelevant here. Below are the ones that actually touch Nate's five
areas. **Blunt meta-note:** the big `agent-*`/`swarm`/`hive-mind` family is cloud/multi-agent
"ECC" tooling built for fan-out at scale — mostly *overkill or wrong shape* for a single-user local
box. The genuinely Nervice-shaped ones are the lightweight, local, cost-aware patterns, flagged ⭐.

### Agents / task-runners
- ⭐ `autonomous-agent-harness` — turn Claude Code into a self-directed agent: persistent memory, **timed/scheduled ops**, task queue (positions itself as an AutoGPT/Hermes replacement). Closest fit for "fireable agents."
- ⭐ `dispatching-parallel-agents` — fan out 2+ independent tasks with no shared state. Simple, useful.
- ⭐ `cost-aware-llm-pipeline` — complexity-based model routing, budget tracking, retries, prompt caching. Mirrors Nervice's own Groq→Ollama→Claude ladder; good reference if the ladder is reworked.
- ⭐ `agentic-engineering` — eval-first execution, decomposition, cost-aware routing (principles).
- `autonomous-loops` / `continuous-agent-loop` — patterns for autonomous loops with quality gates, eval, and recovery control (sequential → DAG).
- `agent-builder` — design/build an agent for a domain (the design checklist, not code).
- `agent-coordination` — spawn/lifecycle for 60+ agent types (heavier than a single-user box needs).
- `plan-orchestrate` / `subagent-driven-development` / `workflow-automation` — decompose a plan into per-step agent chains; execute plan steps via subagents.
- `enterprise-agent-ops` — long-running agent ops: observability, security boundaries, lifecycle (relevant *principles* for monitoring agents; the implementation is enterprise-scale).
- `agentic-os`, `swarm-orchestration`, `swarm-advanced`, `hive-mind`(+`-advanced`), `team-builder`, `stream-chain` — multi-agent / swarm frameworks. **Inspiration only** for Nervice; too heavy for one user.

### Web scraping / retrieval
- ⭐ `data-scraper-agent` — "fully automated AI data-collection agent for any public source (jobs, prices, news, GitHub, sports); scheduled scraping, enrich with a free LLM, store to Notion/Sheets/Supabase." The most on-point skill for creator/news monitoring — and it already stores to Supabase, which Nervice uses.
- `deep-research` — multi-source research via Firecrawl + Exa **MCP** servers, cited reports.
- `exa-search` — neural web/code/company search via Exa MCP.
- `seo-firecrawl` — full-site crawl/scrape/map via Firecrawl MCP.
- `webapp-testing`, `browser-qa` — Playwright-driven page interaction (Nervice's browse agent is the in-house analog).
- `search-first`, `iterative-retrieval` — research-before-coding and staged-context-refinement patterns.
> Caveat: Exa/Firecrawl/DataForSEO skills assume **paid MCP servers / API keys**. Not free-first.

### Data viz
- `dashboard-builder` — operator-useful monitoring dashboards (Grafana/SigNoz), explicitly "not vanity boards." Good model if Nervice ever exposes its own metrics.
- `graphify` — turn any input (code/docs/papers) into a knowledge graph.
- `manim-video` — animated technical explainers; `remotion-video-creation` — React-based video.
- Charting also lives inside `web-artifacts-builder` and `anthropic-skills:ui-ux-pro-max` (25 chart types).

### Frontend
- `frontend-design`, `frontend-design-direction`, `frontend-patterns` — production UI + React/Next patterns.
- `motion-foundations` / `motion-patterns` / `motion-ui` / `motion-advanced` — React/Next animation system (the HUD is hand-rolled canvas/JS, so these are reference, not drop-in).
- `design-system`, `theme-factory`, `make-interfaces-feel-better`, `liquid-glass-design` — design systems, theming, polish, iOS-glass.
- `web-artifacts-builder`, `ui-to-vue`, `frontend-slides`, `accessibility`, `anthropic-skills:ui-ux-pro-max` — artifact building, screenshot→Vue, slides, WCAG, big UI/UX pack.
> The active HUD (`index_v3.html`) is a single-file, zero-dependency canvas app — these skills are **patterns/inspiration**, not directly applicable without a framework.

### Voice
- **Effectively nothing local.** No STT/TTS/VAD/wake-word skill exists in the catalog. The nearest, all **cloud/paid or tangential**: `fal-ai-media` (image/video/**audio TTS** via paid fal.ai MCP), `videodb` (paid video/audio understanding+indexing), `brand-voice` (writing-style profile — *text*, not speech), `remotion-video-creation` (audio inside React video).
> Honest takeaway: the catalog adds **zero** to Nervice's local Kokoro/Whisper/Silero stack. Voice work stays in-house.

---

## PART 2 — Web research (free-first, honest pros/cons/risk)

### A. Open-source "JARVIS-style" assistants — borrow patterns or code?

| Project | License | Reuse verdict |
|---|---|---|
| **Leon** (leon-ai/leon) | MIT | Code-reusable. Node+Python, modular skill/intent/NLU architecture, privacy-first. Closest "personal assistant" peer; its skill-plugin shape is a useful reference for Nervice's `skills.py`. |
| **Rhasspy** (rhasspy/rhasspy) | MIT | Code-reusable. Fully **offline** voice-assistant *services* for many languages, aimed at private home-automation control. Best reference for an offline voice pipeline / intent handling. |
| **OpenVoiceOS / OVOS** (successor to Mycroft) | Apache-2.0 | Code-reusable. Privacy-respecting voice OS + skills framework. Heavier (a whole OS), but the skill/intent and wake-word plumbing is mature. |
| **Open Interpreter** | **AGPL-3.0** | **Inspiration, not copy.** Natural-language → code/computer control. AGPL is strong copyleft — linking its code would force Nervice to AGPL. Study the pattern (it parallels Nervice's `computer.py`), don't vendor it. |
| **Clawdbot** (P. Steinberger, late 2025) | verify before use | **Inspiration.** The breakout "Claude with hands" local assistant (connects WhatsApp/Telegram/Discord). This is *literally Nervice's direction*; worth studying its tool/permission model. New project — confirm license + maturity before borrowing code. |
| **Home Assistant** (voice) | Apache-2.0 | Inspiration for the home-control surface; far larger scope than Nervice needs. |

**How it'd fit Nervice:** Nervice already has the hard parts (jailed agents, closed action set, ladder, memory). The value here is *pattern theft* — Leon's skill plugins, Rhasspy/OVOS's offline intent+wake handling, Open Interpreter's confirm-before-act loop. **Risk: LOW** (reading/learning). **Design conversation: only if** adopting an architecture wholesale; AGPL code must not be vendored.

### B. Scraping PUBLIC data Nate can already see (YouTube / X / Instagram)

**The blunt hierarchy: YouTube = easy & free. X = expensive or illegal-ish. Instagram = don't.**

**YouTube — ✅ free and clean.**
- **RSS (best, free, no key):** `https://www.youtube.com/feeds/videos.xml?channel_id=UC...` returns the channel's **15 most recent uploads** (title, link, thumbnail, description, publish date, view count). No sign-up, no quota. Playlist (`?playlist_id=`) variants exist.
- **Data API v3 (richer, free tier):** 10,000 quota units/day (read = 1 unit, search = 100). Needs a Google Cloud key; fine for occasional richer pulls.
- **What breaks:** RSS caps at 15 newest and gives no historical/analytics depth; the API needs a key and has a daily quota. Neither requires touching Nate's account. **Risk: LOW.**

**X / Twitter — ⚠️ not worth it.**
- **Free tier was discontinued (Feb 2026).** New developers are pay-per-use: ~$0.005 per post **read** (cap 2M/mo), legacy Basic $200/mo, Pro $5,000/mo, Enterprise ~$42k/mo. There is no free read path anymore.
- **Scraping is now contractually dangerous:** X's ToS bans automated crawling/scraping without written consent and sets **$15,000 liquidated damages** for accessing >1M posts/24h via automation. Logged-out scraping is heavily bot-blocked; Nitter-style mirrors are largely dead.
- **Touching Nate's logged-in X account = the worst option:** ToS violation → **account suspension/ban**, plus storing his session cookie/credentials is a real **security liability**. **Risk: HIGH. Recommendation: skip X**, or accept it's paid-API-only and budget for it.

**Instagram — ⛔ avoid.**
- **No personal-account API** since 2020 (Graph API is Business/Creator-only, throttled to ~200 calls/hr).
- ToS prohibits scraping; aggressive bot-blocking + login walls. US case law (hiQ v LinkedIn; Meta v Bright Data, 2024) defends **logged-out public** scraping against CFAA **only** — it does **not** cover GDPR/other regimes, private data, fake accounts, or **logged-in** scraping. A **17.5M-record IG scraping breach** surfaced Jan 2026 — this is an actively policed, high-blowback surface.
- **Touching Nate's logged-in IG = ban + security risk**, same as X but with no viable free public path even read-only. **Risk: HIGH. Recommendation: don't.**

**Security/legality flag (applies to X and IG):** automating a *logged-in* personal account means (1) storing long-lived session credentials on the box — a juicy target, and (2) the platform attributes the automated traffic to Nate and can ban him. Nervice's whole safety posture (jails, secret-scrubbing, closed action set) argues **against** wiring it to log into his social accounts. Keep social to **public, logged-out, official-feed** paths only.

### C. "Change the news channel / see creator updates" — realistic free implementations

- **RSS is the answer, and Nervice already does it** (`/news` reads BBC RSS, 10-min cache). "Change the channel" = swap/extend the feed list: Reuters/AP/NPR/Ars/Hacker News all publish RSS; most outlets and **every YouTube channel** (above) and most podcasts expose RSS. Reddit offers `.rss`/`.json` per subreddit. All free, no keys, low-risk.
- **How it'd fit Nervice:** generalize `/news` from one hard-coded BBC feed to a small user-editable feed list (a `feeds.json` like `music_favorites.json`), with category tags ("world", "tech", "<creator>"). "Show me creator updates" = the YouTube-RSS subset; "change the news channel" = pick a feed/category. Reuses the existing cache + extractive-headlines path (which already runs zero-LLM when capped).
- **Limits:** RSS gives titles/links/summaries, not full articles or real-time push (poll every N min). Some sites have dropped or rate-limit RSS. X/IG have **no** RSS — that gap is exactly why B above is hard. **Risk: LOW. Design conversation: light** (just the feed-list schema + how voice selects a channel).

### D. Fireable AGENTS (Nervice spawning sub-tasks that monitor / run checks)

- **Nervice already has the substrate:** `agent.py` runs the Claude Agent SDK with **jailed** builder/browse agents (cwd-jailed, tool-allowlisted, secrets scrubbed, no push). A "fireable agent" = a **scheduled or triggered** sub-task on that same jail — e.g. "every morning, pull my feeds and summarize," "watch this page and tell me if X changes."
- **Patterns that fit:** (1) **scheduled trigger** — Claude Code has native scheduled-task/cron support, and the `autonomous-agent-harness` skill is built precisely around persistent-memory + timed ops + a task queue; (2) **read-only monitors** — an agent that *only* fetches a feed/page, extracts, and reports, never acts; (3) **propose-don't-apply** — anything that would *act* routes through Nervice's existing confirmation gate / selfmod-style human approval.
- **The core risk — misreading the page / acting on bad data — is real and rated MED–HIGH.** An LLM agent reading a scraped page can hallucinate a "change," misattribute a number, or be fed adversarial page content (prompt injection from the web). If such an agent can *act* (send, buy, post, modify), a misread becomes a wrong action. **Mitigations, all already idiomatic in Nervice:** keep monitors **read-only**; require a confirmation gate before any outward action; show the *evidence* (the quote/headline it acted on) so Nate can sanity-check; never let a web-fed agent touch the selfmod or computer-control surfaces unattended; treat scraped text as untrusted input (it can contain injected instructions).
- **How it'd fit Nervice:** a small "watchers" registry (trigger → read-only fetch+summarize → notify), running on the existing jail, surfacing results into the HUD/voice. **Design conversation: YES — before any code.** The scheduling, the notification surface, and especially the read-only/confirm boundary need to be agreed, because this is the first capability where Nervice acts *on its own clock* on *external, untrusted* data.

### E. Free voice upgrades beyond Kokoro — do any genuinely exist?

- **First, the honest validation:** **Kokoro-82M reached #1 on the TTS Arena leaderboard (Jan 2026)**, beating much larger models (XTTS 467M, MetaVoice 1.2B) at RTF ~0.03 on GPU. Nervice's current choice is, by community blind-ranking, *the* free-local sweet spot for speed/quality. We're not on something second-rate.
- **The one genuine upgrade candidate — Chatterbox / Chatterbox-Turbo (Resemble AI, MIT):** won a blind preference test **65.3% vs ElevenLabs 24.5%**; Turbo is a 350M-param English model billed as lower-compute than prior versions; a 23+ language V3 also exists. **MIT = commercial-OK.** This is the only free-local model with a credible "beats ElevenLabs" claim.
  - **Cost on Nervice's box:** 350M params vs Kokoro's 82M → more VRAM + slower, on a **4060 8 GB already shared by Whisper + Kokoro + Silero + sometimes a local LLM**. The likely play is the "stacking" pattern the field uses in 2026: **Kokoro for fast/default, Chatterbox only when premium quality is wanted** — not a wholesale swap.
- **Others, ranked for Nervice:** **StyleTTS 2** (near-human MOS; Kokoro is *already* built on it, so limited upside), **XTTS v2** (best zero-shot voice cloning but 4–6 GB VRAM — heavy on this box, and a *non-MIT* license historically), **F5-TTS** (3–5 GB), **Piper** (CPU, already Nervice's fallback), **MeloTTS**/**Parler-TTS** (Parler = Apache but heavier). Voice-cloning models (XTTS/F5) also carry **consent/impersonation** ethics flags.
- **Hard ceiling (unchanged from Batch B):** truly ElevenLabs-class emotional expressiveness and long-form naturalness remain **paid/cloud**. Among *free local*, Kokoro and Chatterbox are the frontier. **Risk: MED** (VRAM/latency contention, integration). **Design conversation: YES** — and it should start with a **benchmark of Chatterbox-Turbo on Nate's actual 4060** (latency + VRAM headroom alongside Whisper) before any integration.

---

## PART 3 — Capability summary: fit, risk, design-gate

| Capability | How it'd fit Nervice (one paragraph) | Risk | Design convo first? |
|---|---|---|---|
| **More RSS feeds / "change the news channel"** | Generalize the existing `/news` BBC reader into a small user-editable feed list (like `music_favorites.json`) with categories; reuse the cache + zero-LLM extractive path; voice picks a channel/category. | **Low** | Light (feed schema only) |
| **YouTube creator updates (RSS)** | Subset of the feed list: poll `feeds/videos.xml?channel_id=…` (free, no key) for a watchlist of channels; surface "newest from your creators" in HUD/voice. | **Low** | Light |
| **YouTube Data API v3 (richer)** | Optional deeper pulls (view counts, search) behind a Google key + 10k/day quota; only if RSS proves too thin. | **Low–Med** | Light (key + quota mgmt) |
| **X / Twitter reading** | No free read path post-Feb-2026; paid API or ToS-risky scraping; logged-in = ban + credential-security risk. | **High** | Yes — to decide *not* to, or to budget paid API |
| **Instagram reading** | No personal API, login walls, active bans, breach precedent; logged-in automation endangers Nate's account. | **High** | Yes — recommend skip |
| **Fireable / monitoring agents** | Scheduled, **read-only** sub-tasks on the existing jailed Agent SDK that fetch→summarize→notify; anything that *acts* goes through the confirmation gate; scraped text treated as untrusted. | **Med–High** (misread/inject → wrong action) | **Yes** — read-only/confirm boundary + scheduling + notify surface |
| **Open-source assistant patterns** (Leon/Rhasspy/OVOS/Clawdbot) | Borrow skill-plugin, offline-intent, and confirm-before-act patterns; vendor MIT/Apache code only, treat AGPL (Open Interpreter) as inspiration. | **Low** (study) | Only if adopting an architecture |
| **Chatterbox-Turbo TTS (MIT)** | "Stack" alongside Kokoro: Kokoro default/fast, Chatterbox for premium lines; benchmark on the 4060 first for VRAM/latency vs Whisper. | **Med** | **Yes** — benchmark before integrating |
| **Browser-side Silero VAD** (already queued as a batch) | Move speech detection into `index_v3.html` so room noise is rejected at the source, not just by the server gate. | **Med** | **Yes** (already chipped) |

### Guiding principles for future sessions
1. **Free-first, public-only, logged-out.** Never wire Nervice into Nate's logged-in social accounts — it trades the project's entire safety posture for data that's barely reachable anyway.
2. **RSS/official feeds beat scraping** every time they exist; scraping is the fallback for surfaces that *have* no feed (X/IG) — which are exactly the surfaces not worth the risk.
3. **Monitors read; actions confirm.** A web-fed agent may misread or be prompt-injected; keep it read-only and make any outward action pass the existing human gate, showing its evidence.
4. **Don't churn the voice stack.** Kokoro is the validated free-local frontier; the only justified move is *adding* Chatterbox as a premium option after an on-box benchmark — not replacing.
5. **This box is VRAM-bound (8 GB).** Every new local model (TTS upgrade, browser-VAD aside) competes with Whisper/Kokoro/LLM. Measure before adding.

---

## Sources
- Open-source assistants: [Leon (leon-ai/leon, MIT)](https://github.com/leon-ai/leon) · [Rhasspy (MIT)](https://github.com/rhasspy/rhasspy) · [OpenVoiceOS (Apache-2.0)](https://github.com/openVoiceOS) · [getleon.ai](https://getleon.ai/) · [Clawdbot guide](https://www.techprotocol.blog/blog/clawdbot-open-source-self-hosted-ai-assistant-guide) · [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) · [Home Assistant](https://en.wikipedia.org/wiki/Home_Assistant)
- X/Twitter API + scraping: [X API pricing 2026 (Postproxy)](https://postproxy.dev/blog/x-api-pricing-2026/) · [X API tiers $0–$42K (xpoz)](https://www.xpoz.ai/blog/guides/understanding-twitter-api-pricing-tiers-and-alternatives/) · [X bans scraping/crawling (nftnow)](https://nftnow.com/news/x-updates-terms-of-service-to-ban-unauthorized-data-crawling-scraping/) · [TechCrunch: X ToS update](https://techcrunch.com/2023/09/08/x-updates-its-terms-to-ban-crawling-and-scraping) · [Is web scraping legal 2026 (ScraperAPI)](https://www.scraperapi.com/web-scraping/is-web-scraping-legal/)
- Instagram: [IG scraping guide 2026 (Phyllo)](https://www.getphyllo.com/post/instagram-scraping) · [IG API scraping breach, 17.5M (Security Boulevard)](https://securityboulevard.com/2026/03/the-instagram-api-scraping-crisis-when-public-data-becomes-a-17-5-million-user-breach/) · [IG API deprecated? alternatives (SociaVault)](https://sociavault.com/blog/instagram-api-deprecated-alternative-2026)
- Free local TTS: [Local TTS guide 2026 (LocalClaw)](https://localclaw.io/blog/local-tts-guide-2026) · [Best open-source TTS 2026 / Chatterbox 65.3% (FindSkill)](https://findskill.ai/blog/best-open-source-tts-2026/) · [Chatterbox repo (MIT)](https://github.com/resemble-ai/chatterbox) · [TTS leaderboard / Kokoro #1 (CodeSOTA)](https://www.codesota.com/text-to-speech) · [Piper vs XTTS vs F5 vs StyleTTS2 (PromptQuorum)](https://www.promptquorum.com/power-local-llm/local-tts-voice-cloning-piper-coqui-xtts)
- YouTube feeds/API: [YouTube Data API quotas (Google)](https://developers.google.com/youtube/v3/getting-started) · [API limits/quota 2026 (Phyllo)](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota) · [YouTube channel RSS format (chuck.is)](https://chuck.is/yt-rss/) · [YouTube RSS without tools (gHacks)](https://www.ghacks.net/2022/08/01/how-to-subscribe-to-youtube-rss-feeds-without-third-party-services/)
- Web-scraping legality (general): [Is web scraping legal 2026 — US+EU (cloro)](https://cloro.dev/blog/website-scraping-legal/)
