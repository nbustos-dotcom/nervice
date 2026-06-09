import app.net  # noqa

import os
import pathlib
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import query, ClaudeAgentOptions

CONFIG_DIR = pathlib.Path("~/.claude-nervice").expanduser()


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
