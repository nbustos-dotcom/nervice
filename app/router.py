from app.llm import chat_json

ROUTER_SYSTEM = """Classify the user's message into exactly one route. Output ONLY JSON: {"route":"hard"} or {"route":"normal"}.
hard = formal logic puzzles/riddles/brainteasers with interacting constraints; mathematical proofs or multi-step quantitative problems beyond basic algebra; design or review of nontrivial code architecture or database schemas; long rigorous analysis where wrong answers are costly; or the user explicitly asks for Claude.
normal = everything else: chat, opinions, simple facts, news/current events, everyday tasks.
Classify the TASK TYPE only. Ignore whether the question seems easy or famous."""


async def classify(user_message: str) -> str:
    try:
        out = await chat_json(ROUTER_SYSTEM, user_message)
        return "hard" if out.get("route") == "hard" else "normal"
    except Exception:
        return "normal"   # fail open to the free path
