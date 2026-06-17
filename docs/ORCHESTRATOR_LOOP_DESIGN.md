# ORCHESTRATOR — CLOSED-LOOP DESIGN (research only, 2026-06-13)

Design for the path from today's planner to a **closed-loop** orchestrator: Nate edits the project
by **talking** and by **buttons** on the PROJECT Nerve Page, and Claude Code's results **flow back**
so Nervice can propose plan updates (Nate approves). No code here — this feeds the next build sessions.

**Grounding.** Builds on `app/orchestrator.py` (reads `docs/projects/ACTIVE.md`; state in
`data/orchestrator_state.json` = `{steps:[{title,detail,status}], current, gaps, plan_hash, prompts}`;
critique/plan/prompt-gen on the FREE rungs via `_ask_free` = Groq → Ollama → honest-empty, never
Claude) and `docs/ORCHESTRATOR_FEASIBILITY.md` (the SDK builder `agent_task` returns
`last_run = {cost_usd, num_turns, is_error, permission_denials}` + local git commits, but the SDK is
now **metered**). Free-first reality post-June-15:

| Channel | Cost |
|---|---|
| **Interactive Claude Code** in the terminal (Nate runs it) | **FREE** (subscription, unmetered) |
| **Groq / Ollama** (Nervice's planner/critic/parser brain) | **FREE** |
| **Agent SDK / `claude -p`** (`agent_task`, headless) | **METERED** (monthly Agent-SDK credit) — *verify; date-sensitive* |
| **Local file reads + workspace `git log/diff`** | **FREE** (no model at all) |

> **The one insight that settles everything (Part 1):** a free, structured closed loop does **not**
> need the metered SDK. It needs a **result-file + git convention** that *interactive* (free) Claude
> Code writes to, which Nervice reads locally. The future "hands" only automate Nate's *paste/run*
> step — they do **not** change the result channel. So even a hands-off loop stays free.

---

## PART 1 — CC ↔ NERVICE COMMUNICATION (the crux)

How a step's result actually gets back to Nervice. Every real channel, ranked.

### (a) Nate pastes CC's output back — *free, works today*
Nate copies Claude Code's final report and pastes it into Nervice. Nervice parses it on **Groq**
(grounded, like the critique) into a structured outcome: `{status: ok|partial|fail, files_changed:[],
tests: passed|failed|none, problems:[], summary}`. The orchestrator's generated prompts already end
with *"report what changed and WAIT for review"*, so CC's last message is a structured-ish report.
- **Cost:** free (Groq). **Now:** yes, zero new infrastructure.
- **Reliability:** medium — depends on Nate copying the right span and the model parsing prose. No
  ground truth (a model could over-read a "done" that didn't happen).
- **Structure:** medium (Groq extraction is good but it's interpreting prose, not reading facts).

### (b) Screen-control reads CC's terminal — *free, needs unbuilt hands, brittle*
The future "hands" read the Claude Code terminal directly.
- **Cost:** free (reading the screen, no API). **Now:** no — `PROJECT_STATE §10` confirms **no
  keyboard/mouse/OCR lib is installed** (no pyautogui/mss/uiautomation); `computer.py` is a closed
  action set and Phase-1 `browser.py` is read-only.
- **Reliability:** **poor / genuinely uncertain.** OCR of a terminal is fragile (font, ANSI color,
  scrollback, wrapping); true terminal-buffer access needs a PTY integration, not screen-scraping.
- **Structure:** low (a scraped blob, then re-parsed by a model). **Not recommended** as the result
  channel. (A future "watch CC live" HUD feed is the only place this earns its keep.)

### (c) A shared result-file the interactive CC writes — *free, structured, the real substrate*
Nervice **generates the prompt**, so it can make the prompt instruct Claude Code to, at the end,
**write a small result file** to a known path (e.g. `<workspace>/.nervice/result.json` or
`docs/projects/results/step-<n>.json`) with `{status, files_changed, tests, problems, summary}`.
Claude Code is a coding agent — writing a file is trivial and it follows end-of-task instructions
well. Nervice then **reads that file locally** (free, deterministic).
- **Cost:** free (interactive CC + local read). **Now:** yes — it's prompt-engineering + a file read,
  no new capability.
- **Reliability:** high for the *read* (a file is a fact); the *write* depends on CC complying every
  time (prompt-compliance — **flag: needs live testing**; git (d) is the honest backstop).
- **Structure:** **high** — CC writes the exact schema Nervice asked for.
- **Re: does the SDK already give structured results?** Yes — `agent_task` returns `last_run`
  (`is_error`, cost, turns) + the final text. But `agent_task` is the **metered** SDK. So the SDK is
  the *structured-but-paid* variant; the result-file is the *structured-and-free* variant. Same shape
  of result, different bill.

### (d) Git as ground truth — *free, objective, complements (c)*
After a step, Nervice reads `git -C <workspace> log -1` and `git diff` of the workspace repo. CC (or
the prompt) commits each step; the commit message is CC's own summary and the diff is **objective
evidence of what actually changed**.
- **Cost:** free (no model). **Now:** yes (a local git read; the workspace path is the only new
  convention).
- **Reliability:** **highest** — git doesn't lie. This is the *honesty backstop*: cross-check (c)'s
  self-report against the diff (don't trust a "done" with an empty diff).
- **Structure:** medium-high (files + message + diff; a model can summarize, but the facts are real).

### Ranking & recommendation

| Rank | Channel | Free? | Now? | Reliability | Structure |
|---|---|---|---|---|---|
| 1 | **(c) result-file + (d) git cross-check** | ✅ | ✅ | high (git verifies the self-report) | high |
| 2 | **(a) paste-back** | ✅ | ✅ | medium | medium |
| 3 | (d) git alone | ✅ | ✅ | high (facts) but no self-report | medium |
| 4 | (c2) SDK `agent_task` | ❌ metered | ✅ | high | high |
| 5 | (b) screen-read terminal | ✅ | ❌ needs hands | poor | low |

- **v1 (free, now):** start with **(a) paste-back** (zero new convention — ship the loop immediately),
  then graduate to **(c)+(d)** — the result-file CC writes, cross-checked against the workspace git
  diff. That pair is the durable free substrate: structured *and* honest.
- **v2 (with hands):** the hands automate **Nate's paste/run** (type the prompt into the CC terminal,
  press enter), but the **result still comes via (c)+(d)** — free. The metered SDK (c2) stays an
  explicit opt-in for when Nate *wants* to spend the credit for a fully headless step.

---

## PART 2 — TALK-TO-EDIT THE PROJECT (free, now)

Nate edits the project by conversation. Two edit surfaces, both Groq → Ollama, never Claude:
- **DOC edits** — `ACTIVE.md` sections: goal, context, requirements, out-of-scope, success-criteria.
- **PLAN edits** — `orchestrator_state.json` steps: add / edit / remove / reorder / mark-done.

### Intent → safe edit
A new orchestrator op (`edit`) routed like the others. Groq classifies the utterance into a
**structured operation** — `{surface: doc|plan, target: goal|requirement|step|…, op: set|rewrite|add|
remove|reorder, selector: <index/which>, value: <new text>}` — grounded on the *current* doc/state so
"requirement 2" and "step 3" resolve to real items. For generative edits ("add a step for error
handling"), Groq drafts the new step's title+detail from the goal/context (same engine as the
new-project draft).

### Confirm before writing (always)
Mirror the new-project **confirm gate** and the selfmod principle: Nervice shows the **before → after**
(the field's old vs proposed text, or the step operation in plain terms) and asks *"save this?"*. Only
on **yes** does it write. "Cancel" aborts; Nate can revise the proposal first. Every write is a
deliberate human-approved act — no silent edits.

### Interaction with the plan state (the subtle part)
The orchestrator already stamps `plan_hash` = the doc hash at plan time (`_op_plan` compares it to
decide show-vs-regenerate). Use that:
- **Editing a DOC field** (goal/requirements) can make the existing **plan stale**. On a doc write,
  `plan_hash` no longer matches → Nervice **does NOT silently regenerate**. It flags drift — *"the doc
  changed since your plan was made — want me to re-plan?"* — and re-plans only on Nate's yes (a
  proposal, not an action). This honors "never silent self-editing."
- **Editing a STEP directly** (add/remove/reorder/edit) is Nate manipulating the plan himself — no
  re-plan needed; just update `steps` and fix the `current` index if a removal/reorder shifts it.
  Mark-done is the existing `done` op.

### Safety
Writes touch **only `docs/projects/` files + `orchestrator_state.json`** — nothing else. Reuse the
existing `_SCOPE_FLAG_RE`: if an edit (a new requirement or step) references Nervice's own
safety/self-mod/core code, **flag it, don't apply it** — that stays behind the selfmod gate. Never
write outside `docs/projects/`.

---

## PART 3 — EDIT BUTTONS ON THE PROJECT NERVE PAGE

The PROJECT overlay already shows goal / critique gaps / step list (done·current·pending) / current
CC prompt + copy, in the established Nervice JARVIS system (purple = identity, cyan = data, corner
brackets, **type-on-void, no boxes-on-boxes**). Buttons share the **same backend ops as Part 2** —
talk and click are two triggers for one edit layer (DRY).

### Minimal real set (only what genuinely beats talking)
| Control | Where | Why a button wins over talking |
|---|---|---|
| **Mark done** (toggle the step marker) | each step row | One tap vs a sentence; frequent, atomic |
| **Reorder** (⌃ ⌄ on the row) | each step row | Spatial — far better than *"move step 3 before 2"* |
| **Remove step** (✕ at row end, **confirm**) | each step row | One tap; talking is fine too but slower |
| **Regenerate prompt** (↻) | current step | One tap to re-draft the CC prompt |
| **Re-plan / re-critique** (thin text-link) | section header, shown only on drift | Acknowledge the *"doc changed"* proposal in place |

### Where buttons are redundant (keep conversational)
- **Edit goal / rewrite a requirement / add a richly-described step** — these are *prose*. On a
  voice-first HUD, *saying* it (Groq drafts it, Part 2) beats opening a text field. A button here just
  spawns a mini text editor — real frontend for marginal gain. **Talk wins for generative/text edits.**

### Honest tradeoff
Buttons turn the read-only HUD panel into a **document editor**: inline editable state, optimistic
updates, drag/▲▼ reorder, confirm dialogs, the drift→re-plan flow, and mobile touch — non-trivial
frontend, and the conversational path (Part 2) already covers *all* of it. **Recommendation:**
talk-to-edit first (most capability, least surface); then add only the 3–4 atomic buttons above (mark
done, reorder, remove, regenerate) where a tap is genuinely better than a sentence.

### Aesthetic + UX rules (Nervice design system + ui-ux-pro-max)
- **No boxes-on-boxes** — controls are inline **SVG glyphs** on the existing rows (a faint ↻ / ✕ /
  ⌃⌄), not buttons-in-cards. Reveal on row hover/focus (`progressive-disclosure`) so the read view
  stays clean.
- **Touch targets** ≥ 44 px hit area even when the glyph is small (`touch-target-size`, hit-slop) —
  critical on the mobile HUD.
- **One primary action per context** (`primary-action`): the **Copy prompt** button stays the single
  primary CTA; edit glyphs are subordinate/ghost.
- **Destructive = confirm + danger color** (`confirmation-dialogs`, `destructive-emphasis`): Remove
  step asks first and uses a faint `--crit`, spatially separate from benign controls.
- **State clarity** (`state-clarity`): done = dim+checked cyan, current = bright cyan, pending = faint;
  hover/pressed states distinct but on-style.
- **Reduced motion** (`reduced-motion`) and **no-emoji icons** (`no-emoji-icons`) — already the HUD's
  defaults; keep them.

---

## PART 4 — FEEDBACK → PLAN UPDATE (the loop, human as judge)

**Hard rule (Nate's standing principle, = the selfmod gate applied to the plan):** Nervice **PROPOSES**
plan changes from CC's feedback; **Nate APPROVES**. Nervice **never silently edits the project.**

### The propose → approve flow
1. **Result arrives** via Part 1 — Nate pastes the report (a), or Nervice reads the result-file (c) +
   workspace `git diff` (d).
2. **Nervice interprets it on Groq** (grounded): success / partial / fail, what changed, problems —
   and **cross-checks the self-report against the git diff** (an honest "done" requires a real diff;
   never accept a claim the facts don't support).
3. **Nervice proposes**, it does not act:
   - *Success* → "Step 2 looks done — the diff added `parser.py` and tests pass. Mark it complete and
     here's the step-3 prompt?"
   - *Failure* → "Step 2 failed — `X`. Add a fix step / revise the prompt / retry?"
   - *Partial* → "Step 2 mostly worked but `Y` — add a follow-up step?"
4. **Nate approves / edits / rejects.** Only on **approval** does Nervice mutate
   `orchestrator_state.json`. Rejection or edit loops back to (3).

### Where it surfaces
- **Chat** — the propose→approve *is* a conversation (reuse the existing confirm-gate pattern from
  new-project / `computer.resolve_pending`). Free, works now, voice-friendly.
- **The reserved ASKS panel** — the natural home for the *queue* of pending proposals needing Nate's
  judgment: plan-change proposals, scope flags, "re-plan?" drift nudges (and conceptually the selfmod
  proposals too). **PROJECT panel = the plan/state (read + direct edits); ASKS panel = the
  human-as-judge inbox (proposals awaiting approve/reject).** Clean separation. *(ASKS is reserved,
  not built — flag.)*

This is the project-level twin of the existing selfmod gate (propose a diff → human approves). The
human stays the judge of "is this step actually done", "should we add a fix step", "is the plan still
right".

---

## PART 5 — SEQUENCED ROADMAP

### NOW + FREE (no hands, no metered SDK)
| # | Build | Free? | Human-must-judge |
|---|---|---|---|
| 1 | **Talk-to-edit** the doc/plan (Part 2) — Groq, propose→confirm→write | ✅ | every write (confirm) |
| 2 | **Paste-back feedback** (1a) + **propose→approve** (Part 4) in chat | ✅ | every plan update (approve) |
| 3 | **Result-file + git convention** (1c+1d) — prompts instruct CC to write `result.json` + commit; Nervice reads file + `git diff`, cross-checked | ✅ | confirm the interpreted outcome + the proposed update |
| 4 | **ASKS panel** — the propose/approve queue surfacing #2/#3 | ✅ | it *is* the judgment surface |
| 5 | **Edit buttons** (Part 3) — the 3–4 atomic controls | ✅ | confirm destructive (remove); re-plan on drift |
| 6 | **Auto-fetch result** — a file-watcher on the result path (no hands needed!) auto-reads (c) when it appears → proposes | ✅ | still approves the plan update |

### LATER + needs HANDS (screen control; still free)
| # | Build | Free? | Human-must-judge |
|---|---|---|---|
| 7 | **Auto-run** — hands type the generated prompt into the interactive CC terminal + enter. Needs the **unbuilt** keyboard-synthesis hands (`PROJECT_STATE §10`: not installed). Result still via (c)+(d). | ✅ (no API) | approves each resulting plan change |
| 8 | **Near-hands-off loop** — chain auto-run → auto-fetch → propose → *(Nate approves)* → next. The approval gate stays, so it is never *fully* hands-off. | ✅ | **every plan mutation** |

### METERED alternative (opt-in only)
- **SDK `agent_task` per step** → structured `last_run` returned directly (no paste/run), files via
  git. **Metered** (the Agent-SDK credit). The cleanest *structured headless* path, but it costs —
  reserve as an explicit *"run this step on the paid builder?"* opt-in, never the default.

### Every place a human MUST stay the judge
- Every **doc/plan write** (talk-to-edit) — confirm before write.
- Every **plan update from feedback** — propose → approve, **never silent**.
- **"Is the step actually done?"** — Nervice cross-checks report vs git diff, **Nate confirms**.
- **"Re-plan after a doc change?"** — proposed on `plan_hash` drift, Nate approves (no silent regen).
- **Scope flags** — anything touching Nervice's own code → flag, stays behind the selfmod gate.
- **Anything leaving the jail** (push / PR / deploy) — human (per the feasibility doc).
- **Spending the metered credit** (SDK path) — explicit opt-in.

---

## Uncertainties (flagged honestly)
- **CC prompt-compliance for the result-file (1c):** whether interactive Claude Code reliably writes
  `result.json` every run is unverified — **needs live testing**; the git diff (1d) is the honest
  backstop if it doesn't.
- **Workspace/results path convention:** the orchestrator today reads only `docs/projects/`. A result
  channel needs a *known* workspace + results path that both CC writes to and Nervice reads — a small
  new convention to settle (where the project's code actually lives vs the planner's docs).
- **Per-step commits:** relies on the prompt instructing CC to commit (and Nate not skipping it).
- **The hands are entirely unbuilt** — auto-*run* (typing into the terminal) is the single biggest
  missing piece. Note that auto-*fetch* (file-watch) is free **and** buildable now without hands.
- **Terminal screen-reading (1b)** reliability is genuinely unknown and likely poor — do not depend
  on it for results.
- **The June-15 SDK metering** (from the feasibility doc) is the premise for "SDK = metered" —
  **verify directly**; it shifts the free/paid line for the whole loop.
