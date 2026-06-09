import app.net  # noqa

import os
import pathlib
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

CONFIG_DIR = pathlib.Path("~/.claude-nervice").expanduser()
WORKSPACE = pathlib.Path.home() / "nervice-workspace"

last_run: dict = {}  # metadata from the most recent agent_task: cost_usd, num_turns, is_error, permission_denials


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
    """File-capable agent: builds/edits files inside WORKSPACE only. No shell."""
    WORKSPACE.mkdir(exist_ok=True)
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN not set — Nervice's Pro-account token is required")
    opts = ClaudeAgentOptions(
        system_prompt=("You are Nervice's builder agent. Work ONLY inside the current working directory. "
                       "Never create, read, or modify anything outside it. "
                       "Create real, complete, working files — no placeholders, no TODOs, no lorem ipsum. "
                       "When done, summarize what you built and list every file created/modified."),
        cwd=str(WORKSPACE),
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="acceptEdits",
        max_turns=30,
        env={"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CONFIG_DIR": str(CONFIG_DIR)},
    )
    parts = []
    final = None
    last_run.clear()
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
    return (final or "\n".join(parts)).strip()
