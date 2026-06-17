# NERVICE — NEXT-STEPS SCAN

**Date:** 2026-06-14 · **Scope:** research only, zero code · **Branch:** main (HEAD `c4802ef`, post spend-guard merge)
**Lens:** free-first, local, single-user. Every item below is grounded in code that exists on main — what's *already partly built*, a *cheap extension of the real architecture*, or a *real gap between built and daily-useful*. No generic features. Where a direction isn't worth it, it says so.

> **Grounding note.** The Claude spend guard just merged (`usage.claude_blocked_reason` enforced at the four `agent.py` SDK entry points; cap + free-only on `/ladder` + `POST /free-only`). So "free-first" is now code-bounded: every suggestion here runs on Groq/Ollama + local reads, and the few that could touch Claude are explicitly flagged.

---

## 1. ALMOST-DONE — 80–90% built, one small step from useful

Ranked by **usefulness ÷ effort** (highest first).

| # | Thing | What exists | Small remaining step | Daily usefulness | Effort |
|---|---|---|---|---|---|
| 1 | **Orchestrator result-loop (paste-back)** | prompt-gen ends "report what changed and WAIT" (`orchestrator._gen_step_prompt`); `_ask_free` Groq parsing; the confirm-gate (`_PENDING`/`resolve_edit`, orchestrator.py:944-1006); `_op_done` advances state | one new op that takes Nate's pasted CC output → Groq-parse to `{status,files,tests,problems}` → **propose** mark-done + next prompt (or a fix step) through the existing confirm gate | **High** — turns the planner into an actual *loop* (the whole point of the middleman) | **S–M** |
| 2 | **Free general web-read route** | `browser.open_page`+`read_page` (browser.py:147,184) — generic, https-only, read-only — exist with **zero consumers**; `_execute_canvas` (chat.py:193) is the free read→Groq-synth template | point a route at an arbitrary URL using the *free* reader + Groq synth, instead of the **metered** `browse_agent` | **Med–High** — "read this page and tell me X," free | **S** |
| 3 | **/memories/recent → a MEMORY panel** | endpoint built (api.py:526), returns newest+salient active memories; **no UI calls it**; PROJECT/ACTIONS Nerve Pages are the pattern | a third read-only Nerve Page (or a chat "what do you know about me") that fetches it | **Low–Med** — makes recall *visible/trustable* (today it's invisible) | **S** |
| 4 | **ASKS panel** (the judge inbox) | reserved, not built (loop-design Part 4); `/pending` already returns real selfmod proposals; PROJECT/ACTIONS panels are the pattern | a Nerve Page listing pending proposals awaiting approve/reject | **Med** — one "needs your call" inbox; but only pays off once #1 produces plan-proposals | **M** |
| 5 | **memory_requeue consumer** | 63 quarantined entries (`data/memory_requeue.jsonl`), written by `memory.py`, **never re-verified** | a drain job that re-runs extraction on free rungs when Groq is up, promotes/dedupes | **Low** — invisible hygiene; improves memory slowly | **M** |
| 6 | **/browser/kill → HUD button** | authed endpoint works (api.py:807); `browser.is_kill_request` already kills by voice/text | a HUD button | **~Zero marginal** — the capability already exists conversationally | **S** |

**Honest cuts from this list:** #5 (memory_requeue) and #6 (kill button) are real but low-payoff. #5 is hygiene with no daily-visible benefit — defer until memory volume actually matters. #6 duplicates a capability that already works by voice; skip. #4 (ASKS) is worth it *after* #1 exists (it needs a queue of plan-proposals to hold; today only selfmod proposals would populate it).

**The clear winners are #1 and #2** — both are small, both are free, both convert something already built into daily value.

---

## 2. UNDERUSED CAPABILITIES — real powers barely paying off

**a) A free, read-only web reader with no consumers (the biggest one).**
`browser.open_page`/`read_page` (browser.py:147,184) read *any* https page's text on the persistent profile — free, deterministic, no model. They are **never called**. The only wired free reader is `read_canvas` (chat.py:210); the general "browse" route spends **metered Claude** through `browse_agent` (tools.py:151). So Nervice can already read the web for free and doesn't. *Cheap win:* a "read `<url>` and answer/summarize" path that uses the free reader + Groq synthesis (exactly `_execute_canvas`'s shape, generalized). It adds a real daily power (read a syllabus page, a GitHub README, a docs page, an article) **and cuts Claude spend** by reserving the metered agent for true click-through flows. Single-page read covers most "what does this page say" asks; the agent is only needed for multi-step interaction.

**b) Canvas reading is built and seasonal-but-real.** `read_canvas` (browser.py:432) authenticates to Nate's MTU Canvas (persistent session) and returns due-dates/assignments, free, with honest needs-login. It's genuinely useful to a student — *in the semester*. It's summer, so it's low-season now. Worth keeping warm; it's a real daily reach Sept–May ("what's due this week"). The same machinery (a) generalizes it to any site.

**c) Memory recall is invisible, so it under-pays.** CORE + topic injection runs every turn (`retrieval.retrieve`), but PERSONA says *never volunteer memories* (persona.py:21), so recall only ever helps implicitly (slightly better answers) and Nate can't tell it's working. Combined with a summer lull (little personal conversation to recall against), it's paying off least right now. *Cheap addition:* make it visible (#3 above) so trust builds; the only place it currently surfaces is the session greeting ("reference one thing you know about him"). Don't over-invest — the deeper issue is there isn't much daily conversation yet, which is a usage gap, not a code gap.

**d) Skills are built and completely unused.** `data/skills.json` is empty (2 bytes). The whole trigger→steps chain engine (`skills.py`) works and has no skills defined. That's a *usage* gap — nothing to build, but worth noting the engine is idle. A couple of seeded default chains *could* prime it, but which ones is Nate's call.

**e) The daily-touchpoint endpoints already exist, uncombined.** `greeting` (chat.py:311), `/weather`, `/daily-summary` (yesterday's real aggregate), `orchestrator.state_snapshot` (where the project stands), `read_canvas` (due-soon) are all built and free. None of them is a *reason to open Nervice*; combined into one "morning brief" they could be (see §4).

**f) The orchestrator brain, pre-screen-control.** Today it plans + emits CC prompts — useful *once per project*, not daily. The single smallest thing that makes it useful **today** (no hands needed) is closing the loop (#1 / §3): plan → paste result → next step → repeat. That converts a one-shot planner into a companion you keep open *while coding* — the activity Nate actually does most.

---

## 3. THE ORCHESTRATOR PATH — the concrete next increment (Nate's stated goal)

The loop-design doc already settled the channel question: **a free closed loop does not need the metered SDK** — it needs the result to flow back via (a) paste-back now, graduating to (c) a result-file + (d) git cross-check. The realistic, free, buildable-now next increment is **result-loop v1 = paste-back + propose→approve**, reusing three things already on main.

**The increment, concretely:**

1. **Generate + run (already works).** `_op_next` / `_gen_step_prompt` hand Nate the step's CC prompt (ending "report what changed and WAIT for review"). Nate runs it in his **interactive (free) Claude Code**.
2. **Paste back (new, small).** Nate pastes CC's final report into Nervice. A new orchestrator op (e.g. `result`) — routed like the others, or detected the way `edit` is — receives the pasted text.
3. **Interpret on the free rung (reuse).** `_ask_free` (Groq→Ollama, never Claude) parses the report into `{status: ok|partial|fail, files_changed, tests, problems, summary}` — grounded, exactly like `_op_critique`.
4. **Propose, don't act (reuse the confirm gate).** Use the existing `_PENDING` + `resolve_edit` mechanism (orchestrator.py:944-1006) — the same propose→confirm pattern talk-to-edit already ships — to offer:
   - *ok* → "Step N looks done — mark it complete and here's the step N+1 prompt? (yes/no)"
   - *fail* → "Step N failed: `X`. Add a fix step / revise the prompt / retry?"
   - *partial* → "Mostly worked but `Y` — add a follow-up step?"
5. **Mutate only on yes.** On approval, reuse `_op_done` (+ `_gen_step_prompt` for the next) or insert a fix step into `state["steps"]`. On no, loop back to the proposal. Never silent.

**Why this is the right next step, grounded:**
- It's the **smallest delta that makes it a loop** — the difference between "generate a plan once" and "I use it every coding session."
- **Free** — interactive CC is free (subscription), the parse is Groq/Ollama, no SDK call.
- **Reuses what's built** — prompt-gen, `_ask_free` parsing, and the confirm gate all exist; this is wiring, not new capability.
- **No hands** — it needs nothing from the unbuilt screen-control.
- The loop-design doc itself ranks paste-back as v1 ("ship the loop immediately").

**The honesty backstop (next, after a workspace convention exists):** add the free git cross-check (d) — read `git -C <workspace> log -1`/`diff` to verify a self-reported "done" against a real diff (don't trust "done" with an empty diff). This needs one unsettled convention: *where the project's code actually lives* (the orchestrator reads only `docs/projects/`; the Pomodoro demo's code path isn't pinned). v1 can ship paste-back **without** git; add git the moment the workspace path is decided. Then the result-file (c) is a refinement of paste-back, not a prerequisite.

**Don't chase the hands first.** Auto-*run* (typing into the CC terminal) is the biggest missing piece and the most brittle; the loop is fully free and useful *without* it. Auto-*fetch* (a file-watcher on the result path) is free and buildable later with no hands — but only matters after the result-file convention lands.

---

## 4. HONEST DAILY-USEFULNESS GAP

Blunt version: Nervice is an **impressive thing Nate built**, not yet a tool he reaches for — and he's said he builds faster than he uses. The friction is real and not about features:

- **It's not ambient.** It's a server you must have running + a HUD/phone you open deliberately. You reach for it only when you *remember* to. A developer's daily tools (terminal, editor, browser) are already open; Nervice isn't in that loop.
- **Its unique value doesn't yet intersect a daily habit.** The free brain ladder, safe actions, and memory are real, but none of them is *why you'd open it at 9am or keep it open at 9pm*.
- **The most natural daily hook is the orchestrator — and it isn't a loop yet.** You plan once, then you're back in Claude Code alone. Closing the loop (§3) is the one change that puts Nervice *next to the thing Nate does most* (building), earning a place on the second monitor.
- **Reliability is the silent killer.** The live server I restarted today was running git `290eddd` — **5 commits stale** (the stale-server trap, caught in the act), and the audit's fragility #1 is that a down Ollama/Supabase **500s a routed turn** instead of degrading. If he reaches for it and it's stale or errors, he won't reach again. This isn't a feature; it's the floor.

**Smallest change, biggest "I actually use this now" payoff — two honest candidates:**
- **The orchestrator result-loop (§3).** Highest ceiling and the best fit for *who Nate is* (a builder). Kept open while coding, it becomes a companion, not a demo. This is also his stated goal.
- **A "morning brief"** that's worth opening: greeting + weather + Canvas due-soon + project status + yesterday's summary — **every piece is already a built, free endpoint** (§2e); it's mostly composition. Cheaper to try, lower ceiling, competes with existing phone habits.

My read: the **loop** is the higher-value daily hook for a builder; the **brief** is the cheaper ambient experiment. But the real prerequisite under both is **reliability** — auto-start the server on boot + fail-soft when Ollama/DB hiccup. A tool that's sometimes stale or 500s never becomes a daily habit, no matter how good the features are.

---

## 5. RANKED SHORTLIST

| # | What it is | Why it's real (grounded) | Effort | Cost | Needs hands? |
|---|---|---|---|---|---|
| 1 | **Orchestrator result-loop v1** (paste CC's output → Groq parse → propose mark-done/next/fix via the confirm gate) | reuses `_gen_step_prompt` + `_ask_free` + `_PENDING`/`resolve_edit`; loop-doc's v1 | **S–M** | Free | **No** |
| 2 | **Free web-read route** (wire `open_page`/`read_page` + Groq synth for "read this URL"); reserve metered `browse_agent` for click-through | the free readers exist with zero consumers (browser.py:147,184); `_execute_canvas` is the template; also trims Claude spend | **S** | Free | **No** |
| 3 | **Reliability floor**: fail-soft retrieval (Ollama/DB down ≠ 500) + lean on the `/health` git banner / boot log already added | audit fragility #1; the live `290eddd` stale-server catch today | **M** | Free | **No** |
| 4 | **Morning brief**: compose greeting + weather + Canvas due-soon + project status + daily-summary into one view/voice line | every input is a built free endpoint (§2e) | **S–M** | Free | **No** |
| 5 | **MEMORY Nerve Page** (wire `/memories/recent`) — make recall visible | endpoint built (api.py:526), unused; follows the existing panel pattern | **S** | Free | **No** |

**I'd do #1 first.** It's Nate's stated goal, it has the highest daily-usefulness ceiling for a builder, it's free, it needs no hands, and it's mostly *wiring parts that already exist* (prompt-gen, free-rung parsing, the confirm gate) rather than new capability — so the effort is low for the payoff. It's the smallest change that turns the planner into the middleman loop.

**But two caveats, honestly:** (2) is the cheapest high-leverage item and *reduces* Claude spend as a side effect — if "least effort for a real new power" matters more than "advance the main goal," do (2) first. And (3) is the unglamorous prerequisite: none of (1)/(2)/(4)/(5) becomes a *daily* habit if the server is stale or 500s when he reaches for it. **The final call is Nate's** — I can't see his actual routine (does he code daily this summer? open the HUD at all? keep the server running?), and that routine, not this ranking, determines which of these earns its place.

**Explicitly not worth it right now:** the memory_requeue consumer (invisible hygiene), the `/browser/kill` button (already works by voice), the PROJECT edit-buttons (the loop-doc itself says talk-to-edit covers it — non-trivial frontend for marginal gain), and chasing screen-control "hands" before the free loop exists (the loop is free and useful *without* them; the hands are the brittlest, largest unbuilt piece). The metered SDK `agent_task`-per-step stays an explicit opt-in, never a default.
