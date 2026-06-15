"""Deterministic news-routing checks (keyword net + _coerce; no network). Run:
   .venv/Scripts/python.exe scripts/test_news_routing.py
The Groq-router path + live fetch are exercised by the adversarial /chat test against a real server.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(str(pathlib.Path(__file__).resolve().parent.parent / ".env"))
from app import router

P = F = 0
def check(desc, got, want):
    global P, F
    okq = got == want
    print(f"  {'PASS' if okq else 'FAIL'} {desc}: {got}")
    P += okq; F += not okq

print("[keyword-net] genuine news cues -> route news")
for msg in ["what's the news", "what's happening today", "anything happening?",
            "what's going on", "catch me up", "any news"]:
    check(f"{msg!r}", router._keyword_route(msg).get("route"), "news")

print("[keyword-net] disambiguation: 'news'/project words must NOT become world news")
check("'what's the news with my project'", router._keyword_route("what's the news with my project").get("route"), "orchestrator")
check("'how's my project going'", router._keyword_route("how's my project going").get("route"), "orchestrator")

print("[_coerce] news topic extraction")
check("news+topic", router._coerce({"route": "news", "topic": "the election"}), {"route": "news", "topic": "the election"})
check("news no topic", router._coerce({"route": "news"}), {"route": "news", "topic": ""})
check("'news' in _ROUTES", "news" in router._ROUTES, True)

print(f"\n=== {P} passed, {F} failed ===")
sys.exit(1 if F else 0)
