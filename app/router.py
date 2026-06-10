from app.llm import chat_json

ROUTER_SYSTEM = """Classify the user's message into exactly one route. Output ONLY JSON: {"route":"hard"} or {"route":"build"} or {"route":"selfmod"} or {"route":"browse"} or {"route":"normal"}.
The key test for selfmod/build/browse is whether the user is making an EXPLICIT ACTION REQUEST, not merely discussing, asking an opinion, or asking how something could be done. Discussion routes to normal.

selfmod = an explicit request to change Nervice's OWN behavior, personality, rules, or code ("stop ending sentences with questions", "change your persona to be warmer", "update yourself to do X"). Asking Nervice's opinion of itself, discussing self-improvement, or asking what could be improved is NOT selfmod — it's normal.
  selfmod: "stop being so wordy from now on"   vs   normal: "what do you think could be improved about yourself?"
build = an explicit request to create, edit, or fix actual FILES/projects (a script, a webpage, a config). Explaining, designing, or discussing code in chat is NOT build.
  build: "build me a landing page in the workspace"   vs   normal: "how would you structure a landing page?"
browse = an explicit request to visit/open/check a SPECIFIC site or do something ON a site. General factual questions or current events are NOT browse.
  browse: "open hacker news and tell me the top story"   vs   normal: "what's the latest AI news?"
hard = formal logic puzzles/riddles/brainteasers with interacting constraints; mathematical proofs or multi-step quantitative problems beyond basic algebra; design or review of nontrivial code architecture or database schemas; long rigorous analysis where wrong answers are costly; or the user explicitly asks for Claude.
normal = everything else: chat, opinions (including opinions ABOUT Nervice itself), simple facts, news/current events, everyday tasks, and any DISCUSSION (as opposed to an explicit action request) of building, browsing, or self-change.
Classify the TASK TYPE — an explicit action request vs. a discussion. Ignore whether the question seems easy or famous."""


async def classify(user_message: str) -> str:
    try:
        out = await chat_json(ROUTER_SYSTEM, user_message)
        route = out.get("route")
        return route if route in ("hard", "build", "selfmod", "browse") else "normal"
    except Exception:
        return "normal"   # fail open to the free path
