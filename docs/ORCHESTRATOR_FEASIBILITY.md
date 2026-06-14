# ORCHESTRATOR — FEASIBILITY (research only, 2026-06-13)

Planning an orchestrator: Nervice reads a project doc → a **critic** agent reviews it for gaps →
Nervice breaks it into steps and produces Claude Code prompts. **v1 = Nate stays the paste-hands.**
This doc answers what's actually possible, grounded in THIS repo's code plus current Claude Code docs.

**Sourcing & confidence.** Two kinds of claims here:
- **[REPO]** = read directly from `app/agent.py` / callers. High confidence — this is what runs today.
- **[DOCS]** = from a web-research pass over the current Claude Code docs, retrieved 2026-06-13 (URLs
  inline). Docs change; a couple of points the researcher flagged as unverified are marked *(verify)*.

> ⚠️ **Two caveats to verify before betting on them (both date/billing-sensitive):**
> 1. **Subscription metering change ~2026-06-15 (2 days out).** Per the docs pass, Agent-SDK / `claude -p`
>    usage on a *subscription* begins drawing from a *separate monthly Agent-SDK credit* pool around
>    June 15, 2026. This repo's builder/consult/browse/selfmod all run on Pro/Max **subscriptions**
>    (token-free, `CLAUDE_CONFIG_DIR`), so this directly affects orchestrator economics. **Verify directly.**
> 2. **SDK auth.** The docs pass claimed the SDK is "API-key-only." **The repo disproves that:** every
>    Claude path here authenticates from per-account stored `claude /login` creds via `CLAUDE_CONFIG_DIR`,
>    with `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` *scrubbed* — and it works in production. Trust the
>    working code; the public docs are just ambiguous on subscription/stored-cred SDK auth.

---

## 1. The SDK Nervice ALREADY has (`app/agent.py`) — the most important answer

**Yes. The orchestrator's "hands" already exist, in production, today.** `agent.py` exposes four SDK
entry points (Python `claude_agent_sdk`: `query`, `ClaudeAgentOptions`, `ClaudeSDKClient`, `ResultMessage`):

| Function | What it is | Tools | Returns |
|---|---|---|---|
| `ask_claude(task, system, messages)` | One-shot reasoning, **laddered** Pro→Max | none (`allowed_tools=[]`, `max_turns=1`) | final text |
| `agent_task(task)` | **Headless builder**, jailed to `~/nervice-workspace` | Read/Write/Edit/Glob/Grep + Bash(allowlist) | final text + `last_run` metadata |
| `browse_agent(task)` | Playwright-MCP browser (read/observe) | `mcp__playwright__*` only | final text |
| `propose_agent(instruction, staging_dir)` | Read-only self-mod **diff** proposer | Read/Glob/Grep | a unified diff |

### `agent_task` — can it "do this coding task, report back"? **[REPO]**
Yes, end-to-end, headlessly:
- **Takes a coding task**, runs it **end-to-end in the jailed workspace** (`cwd=~/nervice-workspace`,
  `permission_mode="acceptEdits"` so **no human approval in the loop**, `max_turns=40`).
- **Writes real files + commits to a LOCAL git repo** (the system prompt makes it `git init`, set a
  local identity, and commit — but **never** `git remote`/`git push`).
- **Returns a result Nervice can read:**
  - **Final text** = `ResultMessage.result` (the agent's own summary: what it built, files
    created/modified, commits).
  - **Run metadata** = `agent.last_run` = `{cost_usd, num_turns, is_error, permission_denials}` —
    so Nervice has a **cost**, a **turn count**, an **error flag**, and a **denials list** per run.
- **It's already wired as a live capability**, not a latent function: `tools.py:agent_build` calls
  `agent_task`, exposed as the Groq tool **`agent_build`**, forced by the **"build" route**
  (`chat.py:_FORCE`), with a **600 s** ceiling (`llm.py:TOOL_TIMEOUTS`) and "its result **IS** the
  reply" (`_TERMINAL_TOOLS`). Today, "Nervice, build X" already does exactly "do this task, report back."

### What `agent_task` does **NOT** give you yet (the real gaps for an orchestrator) **[REPO]**
- **Free-text result, not structured.** It returns the agent's prose summary, not a machine-readable
  `{files: [...], status: ...}`. The *actual* diffs exist in the workspace git repo — Nervice could
  read them (`git -C ~/nervice-workspace diff/log`) but that is **not currently wired**.
- **One shared workspace.** Everything runs in the single `~/nervice-workspace` dir — no per-step or
  per-project isolation. Two projects would collide.
- **No remote/push, by jail design.** It cannot open a PR or push — a human (or a separate, gated step)
  has to move work out of the jail.
- **Pro-only, not laddered.** Unlike `ask_claude`, the builder runs on the primary account dir only;
  a Pro-exhausted state fails the build deliberately (no silent Max retry on a non-quota failure).
- **No session continuity wired.** Each `agent_task` is a fresh `query()`. Multi-step state today must
  be carried by the **workspace files** + whatever context the orchestrator threads into each prompt.
- **Sequential only.** Secrets are stripped from the *global* `os.environ` around each call; concurrent
  `agent_task` calls would race that strip/restore window (`_SCRUB_KEYS`).

**Bottom line:** the "do a coding step and report back" primitive is **already built and battle-tested**.
The orchestrator is mostly *planning + sequencing + reading results around it*, not new agent plumbing.

---

## 2. Claude Code CLI — headless / non-interactive mode **[DOCS, retrieved 2026-06-13]**

Yes, the CLI has a first-class headless mode runnable as a subprocess with no human in the terminal.

- **`claude -p "prompt"` / `--print`** — takes a prompt, prints the result to stdout, no UI interaction;
  all flags (incl. `--continue`/`--resume`) work with it.
  https://code.claude.com/docs/en/headless.md, https://code.claude.com/docs/en/cli-reference.md
- **`--output-format text|json|stream-json`** — `json` returns a `result` field plus `session_id`,
  `total_cost_usd`, and a `usage` token dict; `stream-json` is newline-delimited events. *(num_turns in
  CLI json: likely, by SDK parity — verify.)* https://code.claude.com/docs/en/headless.md
- **Exit codes** — `0` success / non-zero failure (e.g. `claude auth status` documents 0/1). Specific
  codes per failure type are **not** published; infer the reason from the JSON `subtype`
  (`error_max_turns`, `error_max_budget_usd`, `error_during_execution`). *(verify)*
- **Permissions with no human** — `--permission-mode default|acceptEdits|plan|auto|dontAsk|bypassPermissions`
  plus `--allowedTools "…"` / `--disallowedTools "…"`. A tool call that's neither pre-allowed nor in the
  mode's auto-approve set **aborts the run** (there's no one to prompt). Safe headless pattern:
  `acceptEdits` + explicit `--allowedTools`, or `dontAsk` + pre-set allow rules. Dangerous:
  `--dangerously-skip-permissions` / `bypassPermissions` (isolated containers only).
  https://code.claude.com/docs/en/permission-modes.md, https://code.claude.com/docs/en/headless.md
- **Auth headlessly** — `ANTHROPIC_API_KEY` (pre-approved, no prompt) **or** a subscription token from
  `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`. Note `--bare` mode does *not* read the OAuth token
  (needs `ANTHROPIC_API_KEY`). https://code.claude.com/docs/en/authentication.md
- **Multi-step state** — `--continue` (most recent convo in this dir) or `--resume <session_id>`;
  capture `session_id` from the json output to resume a specific run. Scoped to the project dir/worktrees.
  https://code.claude.com/docs/en/headless.md
- **MCP** — `--mcp-config <file|json>`, `--strict-mcp-config` (ignore all other MCP config — reproducible).
  https://code.claude.com/docs/en/cli-reference.md
- **Cost/turn guards** — `--max-turns`, `--max-budget-usd` (SDK: `maxTurns`, `maxBudgetUsd`).

So: a program **can** drive the CLI headlessly. But note (see §4) that Nervice already drives the *same
runtime* via the Python Agent SDK in-process — so the CLI subprocess is usually the *long* way around.

---

## 3. The critic agent (v1's actual first feature) — trivially doable **[REPO]**

**Confirmed trivial with what's already in `agent.py`.** A "review this project doc, list gaps/risks"
call is exactly the shape of **`ask_claude`**:
- `ask_claude(task=<the doc + a critic system prompt>, system=…)` → runs `max_turns=1`, `allowed_tools=[]`
  (pure reasoning, no tools, no jail needed), **laddered Pro→Max**, returns the critique as **text**
  Nervice displays. No new infrastructure, no workspace, no permissions to manage.
- The **planner** (break the doc into steps + emit Claude Code prompts) is the *same* primitive: one more
  `ask_claude` (or a Groq `TOOL_MODEL`) call that returns structured text. Ask for a JSON/numbered step
  list and parse it.

v1 (critic + planner) is **pure prompt-engineering on rails that already exist** — zero new capability.
The only real work is prompt design and a small UI to show the critique/steps. If you want the critic to
*read the repo itself* (not just a pasted doc), use a read-only `query()` like `propose_agent`'s shape
(`allowed_tools=[Read,Glob,Grep]`, a project `cwd`) instead of `ask_claude`.

---

## 4. The v2 driving question — can Nervice RUN the steps it generates? **[REPO + DOCS]**

Three options to actually execute generated steps:

### (a) Drive the SDK builder agent headlessly, per step — **possible NOW; cleanest**
This is *literally `agent_task`*, called once per step. Already headless, already jailed, already returns
cost/turns/error. **Gotchas (all from §1's gap list):**
- One shared `~/nervice-workspace` → give each project/step run its **own `cwd`** (one-line change to a
  per-project dir, or use the SDK/CLI **git-worktree** isolation).
- **Free-text result** → for reliable sequencing, capture **structured** status: read `last_run`
  (`is_error`, `permission_denials`, cost) and `git -C <cwd> diff/log` after each step.
- **Multi-step state** → either keep state in the workspace files (current model) or add SDK
  **`resume=session_id`** *(DOCS: supported; not used in `agent.py` today)* to continue one agent across steps.
- **No push/remote** (jail) → moving results out stays a separate, gated step (human or audited push).
- **Pro-only + sequential** → the builder isn't laddered and the env-scrub is global; serialize steps
  (the server is already single-user/sequential) and accept Pro-exhaustion failing a build honestly.
- **Approvals** → `acceptEdits` already auto-approves edits headlessly; the Bash **allowlist** is the
  real boundary. For finer control the SDK exposes a programmatic **`can_use_tool` callback** *(DOCS)* —
  a natural place for an orchestrator to gate risky steps without a human, if desired.

### (b) Subprocess `claude -p` headless — **possible, but redundant here**
Everything in §2 works. But it **re-implements what `agent.py` already does in-process** and is strictly
worse for this repo: you'd lose the in-process secret-scrub + jail discipline, have to parse stdout JSON,
re-wire permissions as flags, and solve auth separately (CLI `--bare` won't read the OAuth token → you'd
likely need an `ANTHROPIC_API_KEY`, i.e. metered API billing instead of the subscription the repo uses).
Only reach for (b) if you specifically need CLI-only features the Python SDK doesn't expose. Otherwise (a)
is the same engine with less surface area.

### (c) Screen-control typing into an interactive CC terminal — **NOT possible now; don't**
- **Not currently possible without new deps.** Per `PROJECT_STATE.md §10`, there is **no keyboard/mouse
  synthesis library installed** (no pyautogui/uiautomation/mss); `computer.py` and the Phase-1 `browser.py`
  are read-only/closed-action. Driving a terminal by synthetic keystrokes isn't on the table without
  installing and jailing new capability.
- **Even if built, it's the worst option:** brittle (screen-scraping a TUI), no parseable result, no
  cost/turn metadata, no jail, and it fights the read-only-by-construction stance of the current
  screen-control work. **Strongly advise against.**

---

## 5. Honest recommendation

### Realistic **v1** (build now, no new capability)
Planner + critic, **Nate pastes**:
1. Nervice ingests the project doc (paste, or read a file/repo read-only).
2. **Critic** = one `ask_claude` call → gaps/risks/ambiguities as text.
3. **Planner** = one `ask_claude`/`TOOL_MODEL` call → ordered steps, each with a ready-to-paste Claude
   Code prompt (and acceptance criteria).
4. Nervice shows critique + steps; **Nate runs each in his own Claude Code and pastes results back**;
   Nervice tracks progress and re-plans.

This is entirely on existing rails. The work is prompt design + a small UI/loop — **not** agent plumbing.

### Realistic **v2** (Nervice drives execution) — path **(a)**
Promote `agent_task` into a per-step **runner**, smallest delta from what already works:
- per-project `cwd` (isolation) → run step → read `last_run` + `git diff/log` for a **structured** result
  → feed that into planning the next step (or `resume` the session). Keep the jail, the Bash allowlist,
  the secret-scrub, and `acceptEdits` exactly as they are.
- Add a thin **gate layer** for anything that leaves the jail (push/PR/deploy) and for self-mod (below).
- (b) only if a CLI-only need appears; (c) never.

**Cleanest path to v2 = (a).** It reuses the one primitive this repo has already hardened and proven.

### Where a human MUST stay in the loop — regardless of version
- **Anything leaving the jail.** The builder can't push by design; a person (or an explicit, audited,
  separately-gated step) reviews and moves work out. No silent `git push`/PR/deploy.
- **Self-modification of Nervice's own code** stays behind the existing **selfmod approval gate**
  (`propose_agent` emits a diff; a human `approve`s; `SAFETY_FLOOR`/jails/selfmod gate are never
  auto-applied). The orchestrator must not become a back door around that gate.
- **Irreversible / outward-facing actions** (deploys, sending, purchases, account changes) — explicit
  human confirmation.
- **Reviewing the critic + plan before execution.** This *is* v1, and it stays the default checkpoint
  even in v2 (approve the plan, then let steps run).
- **Account/auth setup** — `claude /login` per `CONFIG_DIR` is manual by design; never automate it.
- **The ~2026-06-15 subscription-metering change** (caveat #1) — confirm the billing model before scaling
  any *unattended* multi-step runs, since v2 multiplies SDK calls on the subscription accounts.

---

### Ambiguities I did not guess
- **CLI exit-code semantics** and whether `num_turns` appears in `claude -p --output-format json` are not
  clearly documented — flagged *(verify)* rather than asserted.
- **Public-docs SDK auth** language ("API key only") **conflicts with this repo's working
  `CLAUDE_CONFIG_DIR` subscription auth**; I trusted the running code and flagged the docs as ambiguous.
- **The June-15 Agent-SDK credit change** is from the docs pass and is imminent + material — treat as
  "verify before relying," not as settled fact.
