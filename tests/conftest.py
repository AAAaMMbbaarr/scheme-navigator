"""Shared fixtures. Tests never call the real Claude API and never read a real .env key."""
import json
import re
import socket
from types import SimpleNamespace

import dotenv
import pytest

# The app calls load_dotenv() when backend.main is imported. A developer's real .env (real API keys!) must never
# leak into tests, so turn it into a no-op before anything imports the app.
dotenv.load_dotenv = lambda *a, **k: False

LLM_ENV_VARS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL",
                "LLM_REASONING_EFFORT", "CLAUDE_MODEL", "LLM_TIMEOUT", "CLAUDE_TIMEOUT")


@pytest.fixture(autouse=True)
def no_real_ai(monkeypatch):
    """Every test starts with NO provider configuration and NO network access."""
    for name in LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")     # public-demo limiter is opt-in inside tests
    from backend import ratelimit
    ratelimit.reset()
    real_connect = socket.socket.connect

    def guarded(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"tests must not use the network (tried {host})")
        return real_connect(self, address, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", guarded)


class FakeClaude:
    """Stands in for anthropic.Anthropic. Records every request so tests can inspect what was sent.

    profiles: {exact user text: profile dict}    (extraction answers)
    fail:     exception to raise from every call
    raw:      override the tool payload for the given tool name, e.g. {"localized_text": {...}}
    """

    def __init__(self, profiles=None, fail=None, raw=None, fail_tools=()):
        self.calls = []
        self.profiles, self.fail, self.raw, self.fail_tools = profiles or {}, fail, raw or {}, set(fail_tools)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        tool = kw["tool_choice"]["name"]
        if self.fail is not None or tool in self.fail_tools:
            raise self.fail or RuntimeError("boom")
        content = kw["messages"][0]["content"]
        if tool in self.raw:
            payload = self.raw[tool]
        elif tool == "record_profile":
            text = re.search(r"<user_text>(.*)</user_text>", content, re.S).group(1)
            payload = self.profiles.get(text, {})
        else:
            data = json.loads(re.search(r"<data>(.*)</data>", content, re.S).group(1))
            lang = re.search(r"Language: (\w+)", content).group(1)
            payload = {
                "summary": f"[{lang}] summary",
                "follow_up_questions": [f"[{lang}] {q}" for q in data["follow_up_questions"]],
                "scheme_explanations": {s["id"]: f"[{lang}] {s['explanation']}" for s in data["schemes"]},
            }
        if payload == "NO_TOOL_BLOCK":
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)], stop_reason="tool_use")

    def requests_for(self, tool):
        return [c for c in self.calls if c["tool_choice"]["name"] == tool]


@pytest.fixture
def use_claude(monkeypatch):
    """Route all Claude access through a FakeClaude. Usage: fake = use_claude(profiles={...})"""
    def install(**kwargs) -> FakeClaude:
        fake = FakeClaude(**kwargs)
        monkeypatch.setattr("backend.profile_extractor.get_client", lambda: fake)
        monkeypatch.setattr("backend.explainer.get_client", lambda: fake)
        return fake
    return install


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)
