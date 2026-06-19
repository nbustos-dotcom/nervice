# Nervice — Backlog

Canonical backlog. One place for "known, not yet done / deferred." Live current state lives in
`docs/PROJECT_STATE.md`; this file is the durable to-do/parked list, seeded 2026-06-18 from the
14 known items. It is one of the markdown files Nervice may self-edit through the default-deny
allowlist (`app/self_edit.py`) — drafts only, applied manually by Nate.

Status legend: **OPEN** = not started / not built · **IN PROGRESS** = partially done · **DONE
(verify)** = built, wants a real-world retest · **PARKED** = deliberately deferred.

---

## Open

- **Self-build path (code-edit)** — PARKED. The markdown-allowlist self-edit (this branch, subject B)
  is built; the broader *code* self-edit path is intentionally not built. The proposal/selfmod machinery
  exists but the full "Nervice edits its own `.py`" loop is parked behind the safety review.
- **Memory pipeline** — OPEN. `memory_requeue.jsonl` is *written* but never *consumed* — the clunkiest
  part of the system. Needs a consumer that drains the queue into durable memory.
- **CORE always-on coupling (Part 1 retrieval fix → Part 2 salience)** — OPEN. Part 1
  (`fix/memory-retrieval`, `9eb091f`) made the always-injected CORE tier hold *every* fact at
  `salience >= 4` (was `== 5` + identity/preference only). Today that is just 6 facts, so it is trivial —
  but Part 2 (salience re-score) will pull facts out of the `s3` cluster (52 facts, ~60% of the store) up
  to `>= 4`, and each promoted fact then rides on **every** prompt, unconditionally. So Part 2 is *not* just
  "re-score salience" — it has two coupled consequences it must design for, not discover later:
  - **Prompt bloat.** The always-on set grows with every promotion. Once it passes `CORE_LIMIT` (10), CORE
    starts truncating by `salience DESC` — silently dropping facts, with no signal to anyone that it happened.
  - **Relevance assumption.** Always-on encodes "high salience = always relevant," which is wrong for
    *boundary* facts. "Nate does not like Hentai" is high-signal precisely *as* a boundary/guardrail — not
    something to surface when Nate asks about hockey.
  Part 2's design must answer both in one breath: (a) what gets re-scored up, and by what rule; and (b)
  whether always-on CORE needs a relevance gate or a category exclusion, so that re-scoring doesn't fix
  recall and manufacture prompt bloat in the same move.
- **HUD / UI one-pass redesign** — OPEN. The HUD (`app/static/index_v3.html`) wants a single coherent
  redesign pass rather than the incremental accretion it has now.
- **Email send** — OPEN. Free SMTP, send-only, confirm-gated. Not built. (Send-only by design; no inbox.)
- **Router sees only the latest message** — OPEN. `app/router.py` `classify()` receives only the newest
  user turn, so multi-turn intent ("do that for the other file too") can be misread. Needs a small,
  bounded context window into the classifier without letting it drift.
- **Delete temp Max account** — OPEN, dated. Remove the `"max"` line from `CLAUDE_ACCOUNTS` in
  `app/agent.py` ~Aug 2026 when the temporary account expires (set up 2026-06-10, ~2-month term).

## Deferred / parked (far-future)

- **Screen control beyond Phase-1** — PARKED, far-future. The read-only Phase-1 screen reader is the
  current ceiling; an *acting* screen path stays parked until there's a safe-by-architecture design.
- **Semantic self-knowledge index** — DEFERRED. A semantic/embedding index over the codebase isn't worth
  the staleness cost at this repo size; the read-only self-read (subject A, `app/self_read.py`) +
  `git grep` cover "find the file" well enough for now.

## Recently resolved (loop-hardening branch, merged as 7a9e5ec — kept here for the record, verify if noted)

- **No stop-mid-step for the watcher** — DONE. The headless watcher can now stop between steps.
- **Test gate: Python-only + runs in the Nervice env** — DONE. The loop test gate is scoped to Python and
  executes in the project venv.
- **Sandboxes accumulate uncleaned** — DONE. Stale `nervice-cc-sandbox*` clones are cleaned up.
- **`/diag/claude-spawn` auth** — DONE. The spawn-diagnostic endpoint is now gated.
- **News-vs-web-reader edge** — DONE. The misclassification edge between "news" and the web reader is
  handled.
- **M1 multi-part voice** — DONE (verify). Multi-part voice replies were fixed; still wants a real-mic retest.
