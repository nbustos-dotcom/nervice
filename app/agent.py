import app.net  # noqa

import os
import pathlib
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

CONFIG_DIR = pathlib.Path("~/.claude-nervice").expanduser()
WORKSPACE = pathlib.Path.home() / "nervice-workspace"

last_run: dict = {}  # metadata from the most recent agent_task: cost_usd, num_turns, is_error, permission_denials

# Bash command prefixes the builder may run. Anything not listed fails closed (denied).
# Claude Code parses shell operators (; && || | & newlines) and matches each subcommand
# independently, so e.g. "echo hi ; rm x" is rejected — the allowlist is the boundary, not cwd.
_BASH_ALLOW = ["git", "node", "npm", "npx", "python", "python3", "pip", "pip3",
               "ls", "cat", "pwd", "mkdir", "touch", "echo", "head", "tail", "wc", "grep", "find"]
_BUILDER_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"] + [f"Bash({c}:*)" for c in _BASH_ALLOW]

# Secrets stripped from the spawned agent's environment. The SDK MERGES our env dict into
# os.environ, so any of these left in os.environ would leak into the agent's shell. We pop
# them for the call and restore in a finally. CLAUDE_CODE_OAUTH_TOKEN is included so the agent
# authenticates ONLY from stored creds in CONFIG_DIR — its shell never holds the Pro token.
# Caveat: this is single-user sequential (one REPL turn at a time); concurrent agent_task calls
# would race on the global os.environ during the strip/restore window.
_SCRUB_KEYS = ["GROQ_API_KEY", "DATABASE_URL", "DATABASE_URL_MIGRATIONS", "CLAUDE_CODE_OAUTH_TOKEN"]


async def ask_claude(task: str, system: str | None = None) -> str:
    """Send a hard task to Claude via the Agent SDK (Pro account token). Text in, text out. No tools."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN not set — Nervice's Pro-account token is required")
    CONFIG_DIR.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(
        system_prompt=system or "You are a precise, rigorous reasoner. Answer directly.",
        max_turns=1,
        allowed_tools=[],
        # isolated config dir: the spawned CLI must never see the machine's stored login
        env={"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CONFIG_DIR": str(CONFIG_DIR)},
    )
    parts = []
    async for message in query(prompt=task, options=opts):
        # collect assistant text blocks
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


async def agent_task(task: str) -> str:
    """Bash+git-capable builder agent, jailed to WORKSPACE. Token-free auth (stored creds),
    secret-scrubbed shell, fail-closed Bash allowlist, per-command Bash timeout."""
    WORKSPACE.mkdir(exist_ok=True)
    CONFIG_DIR.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(
        system_prompt=(
            "You are Nervice's builder agent. Work ONLY inside the current working directory; "
            "never create, read, or modify anything outside it. "
            "Use git for version control INSIDE the workspace. If there is no git repo yet, run "
            "`git init`, set the LOCAL identity `user.name` to \"Nervice Builder\" and `user.email` "
            "to \"builder@nervice.local\", then commit your work with a clear message. "
            "You must NOT configure or push to any remote — no `git remote`, no `git push`, ever. "
            "You must NOT attempt to read environment variables or credentials. "
            "Create real, complete, working files — no placeholders, no TODOs, no lorem ipsum. "
            "When done, summarize what you built and list every file created/modified and every commit."),
        cwd=str(WORKSPACE),
        allowed_tools=_BUILDER_TOOLS,
        permission_mode="acceptEdits",
        max_turns=40,
        # token-free: auth comes from stored creds in CONFIG_DIR. Per-command Bash timeout 180s
        # (default == max, so the agent cannot raise its own ceiling). No secrets here.
        env={"CLAUDE_CONFIG_DIR": str(CONFIG_DIR),
             "BASH_DEFAULT_TIMEOUT_MS": "180000",
             "BASH_MAX_TIMEOUT_MS": "180000"},
    )
    parts = []
    final = None
    last_run.clear()
    # Strip secrets (incl. the Pro token) from os.environ so the env-merge can't leak them into
    # the agent's shell; restore unconditionally afterward.
    saved = {k: os.environ.pop(k) for k in _SCRUB_KEYS if k in os.environ}
    try:
        async for message in query(prompt=task, options=opts):
            if isinstance(message, ResultMessage):
                final = message.result
                last_run.update(cost_usd=message.total_cost_usd, num_turns=message.num_turns,
                                is_error=message.is_error, permission_denials=message.permission_denials)
                continue
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
    finally:
        os.environ.update(saved)
    return (final or "\n".join(parts)).strip()


async def propose_agent(instruction: str, staging_dir: str) -> str:
    """READ-ONLY self-modification proposer. cwd is a staging copy of editable files only —
    no Write, no Edit, no Bash. Emits a unified diff for OUR gate to validate and apply."""
    CONFIG_DIR.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(
        system_prompt=(
            "You are Nervice's self-modification proposer. The cwd contains the only files you may "
            "propose changes to. Read what you need, then output a single unified diff (git format, "
            "a/ b/ prefixes, correct relative paths) implementing the requested change, inside one "
            "```diff fence. Minimal, surgical changes only.\n"
            "The diff MUST apply cleanly with `git apply`. To guarantee that:\n"
            "- Include at least 3 lines of UNCHANGED context above and below every change.\n"
            "- Reproduce context lines BYTE-FOR-BYTE from the file — never reword, reflow, or change "
            "quotes/dashes/whitespace on unchanged lines.\n"
            "- Make each @@ hunk header's line counts exactly correct for the lines shown.\n"
            "- Do NOT emit an 'index <hash>..<hash>' line (you cannot know the real hashes).\n"
            "After the fence, 2-3 sentences explaining the change. Never propose changes to files not "
            "present in the cwd."),
        cwd=staging_dir,
        allowed_tools=["Read", "Glob", "Grep"],
        max_turns=15,
        # token-free auth from stored creds; no secrets, same scrub as the builder
        env={"CLAUDE_CONFIG_DIR": str(CONFIG_DIR)},
    )
    parts = []
    final = None
    last_run.clear()
    saved = {k: os.environ.pop(k) for k in _SCRUB_KEYS if k in os.environ}
    try:
        async for message in query(prompt=instruction, options=opts):
            if isinstance(message, ResultMessage):
                final = message.result
                last_run.update(cost_usd=message.total_cost_usd, num_turns=message.num_turns,
                                is_error=message.is_error, permission_denials=message.permission_denials)
                continue
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
    finally:
        os.environ.update(saved)
    return (final or "\n".join(parts)).strip()
