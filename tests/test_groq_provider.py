"""Groq (OpenAI-compatible) provider. All HTTP is mocked; no network, no real key."""
import json
import logging
import re

import httpx
import pytest

import backend.llm as llm
from backend.eligibility_engine import evaluate
from backend.llm import AIError, AnthropicProvider, OpenAICompatProvider, get_client
from backend.models import Profile
from backend.profile_extractor import EXTRACT_TOOL

SECRET = "gsk_TEST_SECRET_1234567890"
BN = "আমি পশ্চিমবঙ্গের একজন কৃষক। আমার ২ একর জমি আছে।"
HI = "मैं बिहार का किसान हूँ और मेरे पास 2 एकड़ जमीन है।"
PROFILES = {
    BN: {"state": "West Bengal", "occupation": "farmer", "land_area_acres": 2, "land_ownership": "owner"},
    HI: {"state": "Bihar", "occupation": "farmer", "land_area_acres": 2, "land_ownership": "owner"},
    "I am a farmer from West Bengal with 2 acres of land.": {
        "state": "West Bengal", "occupation": "farmer", "land_area_acres": 2, "land_ownership": "owner"},
}


class GroqMock:
    """Fake api.groq.com: records requests, answers like a chat-completions tool call."""

    def __init__(self, profiles=None, status=200, args_override=None, raise_exc=None, raw_body=None):
        self.requests: list[httpx.Request] = []
        self.profiles, self.status, self.args_override = profiles or {}, status, args_override or {}
        self.raise_exc, self.raw_body = raise_exc, raw_body
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": f"boom, key was {request.headers['authorization']}"}})
        if self.raw_body is not None:
            return httpx.Response(200, json=self.raw_body)
        body = json.loads(request.content)
        name = body["tool_choice"]["function"]["name"]
        content = body["messages"][-1]["content"]
        if name in self.args_override:
            args = self.args_override[name]
        elif name == "record_profile":
            text = re.search(r"<user_text>(.*)</user_text>", content, re.S).group(1)
            args = self.profiles.get(text, {})
        else:
            data = json.loads(re.search(r"<data>(.*)</data>", content, re.S).group(1))
            lang = re.search(r"Language: (\w+)", content).group(1)
            args = {"summary": f"[{lang}] summary",
                    "follow_up_questions": [f"[{lang}] {q}" for q in data["follow_up_questions"]],
                    "scheme_explanations": {s["id"]: f"[{lang}] {s['explanation']}" for s in data["schemes"]}}
        arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]})

    def bodies(self, tool):
        return [json.loads(r.content) for r in self.requests if json.loads(r.content)["tool_choice"]["function"]["name"] == tool]


@pytest.fixture
def groq_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def groq(monkeypatch, groq_env):
    """Full path: env -> get_client() -> OpenAICompatProvider, with only the HTTP transport mocked."""
    def install(**kw) -> GroqMock:
        mock = GroqMock(**kw)
        real = httpx.Client
        monkeypatch.setattr(llm.httpx, "Client", lambda **k: real(**{**k, "transport": mock.transport}))
        return mock
    return install


def post(client, text, lang="en", **extra):
    return client.post("/api/analyze", json={"text": text, "language": lang, **extra})


# 1. initialization
def test_groq_provider_initialization(groq_env):
    c = get_client()
    assert isinstance(c, OpenAICompatProvider) and c.name == "groq"
    assert c.model == "openai/gpt-oss-20b"
    assert llm.ai_configured() is True and llm.provider_name() == "groq"


def test_model_and_effort_overridable(groq_env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    c = get_client()
    assert c.model == "openai/gpt-oss-120b" and c.reasoning_effort == "low"


# 2. base URL
def test_groq_base_url_default_and_endpoint(groq, client):
    mock = groq(profiles=PROFILES)
    assert post(client, BN, "bn").status_code == 200
    urls = {str(r.url) for r in mock.requests}
    assert urls == {"https://api.groq.com/openai/v1/chat/completions"}
    assert all(json.loads(r.content)["model"] == "openai/gpt-oss-20b" for r in mock.requests)


def test_base_url_override_and_trailing_slash(groq, client, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/openai/v1/")
    mock = groq(profiles=PROFILES)
    post(client, BN, "bn")
    assert str(mock.requests[0].url) == "https://example.test/openai/v1/chat/completions"


# 3. key read from environment (never hard-coded)
def test_api_key_comes_from_environment(groq, client, monkeypatch):
    mock = groq(profiles=PROFILES)
    post(client, BN, "bn")
    assert mock.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    monkeypatch.setenv("GROQ_API_KEY", "gsk_another_key")   # read per request, not cached at import
    post(client, BN, "bn")
    assert mock.requests[-1].headers["authorization"] == "Bearer gsk_another_key"


def test_no_key_literal_in_source():
    import pathlib
    for f in pathlib.Path("backend").rglob("*"):
        if f.suffix in {".py", ".yaml"} or f.name == ".env.example":
            assert "gsk_" not in f.read_text(encoding="utf-8"), f
    assert "gsk_" not in pathlib.Path(".env.example").read_text(encoding="utf-8")


# 4. key never leaks
def test_key_never_in_logs_responses_or_health(groq, client, caplog):
    groq(status=401, profiles=PROFILES)  # the mock even echoes the key back in its error body
    with caplog.at_level(logging.DEBUG):
        r = post(client, BN, "bn")
        h = client.get("/api/health")
    assert r.status_code == 503 and r.json()["error"]["code"] == "api_error"
    assert SECRET not in r.text and SECRET not in h.text and SECRET not in caplog.text
    assert "status=401" in caplog.text            # diagnosable...
    assert "boom" not in caplog.text              # ...without the provider's error body
    assert h.json()["ai_provider"] == "groq" and h.json()["ai_configured"] is True
    assert h.json()["ai_model"] == "openai/gpt-oss-20b"


def test_key_not_in_repr_of_provider(groq_env):
    c = get_client()
    assert SECRET not in repr(c.__dict__.get("base_url", "")) and SECRET not in c.base_url


# 5. selected language reaches the LLM (both calls)
@pytest.mark.parametrize("code,english,locale", [("bn", "Bengali", "bn-IN"), ("hi", "Hindi", "hi-IN"),
                                                 ("en", "English", "en-IN"), ("ta", "Tamil", "ta-IN"),
                                                 ("ur", "Urdu", "ur-IN"), ("or", "Odia", "or-IN")])
def test_selected_language_reaches_groq(groq, client, code, english, locale):
    mock = groq(profiles={"txt": {"occupation": "farmer", "state": "West Bengal"}})
    d = post(client, "txt", code).json()
    for tool in ("record_profile", "localized_text"):
        sent = mock.bodies(tool)[0]["messages"][-1]["content"]
        assert f"Language: {english} ({locale})" in sent
    assert d["localized"]["summary"] == f"[{english}] summary" and d["ai"]["ok"] is True


def test_no_previous_profile_sent_to_groq_on_fresh_request(groq, client):
    mock = groq(profiles={**PROFILES, "I am a student from Bihar.": {"state": "Bihar", "occupation": "student"}})
    a = post(client, "I am a student from Bihar.").json()
    b = post(client, BN, "bn", profile=a["profile"]).json()
    assert b["profile"]["state"] == "West Bengal"
    sent = json.dumps([m for m in mock.bodies("record_profile")[-1]["messages"]], ensure_ascii=False)
    assert "Bihar" not in sent and "student" not in sent


# 6. structured extraction: same schema in, same strict validation out
def test_groq_receives_the_exact_existing_schema(groq, client):
    mock = groq(profiles=PROFILES)
    post(client, BN, "bn")
    body = mock.bodies("record_profile")[0]
    fn = body["tools"][0]["function"]
    assert fn["parameters"] == EXTRACT_TOOL["input_schema"]                    # not weakened or rewritten
    assert body["tool_choice"] == {"type": "function", "function": {"name": "record_profile"}}
    assert body["messages"][0]["role"] == "system" and "never guess or infer eligibility" in body["messages"][0]["content"]


def test_bengali_hindi_english_extraction_through_groq_path(groq, client):
    groq(profiles=PROFILES)
    bn = post(client, BN, "bn").json()
    hi = post(client, HI, "hi").json()
    en = post(client, "I am a farmer from West Bengal with 2 acres of land.", "en").json()
    assert (bn["profile"]["state"], bn["profile"]["occupation"], bn["profile"]["land_area_acres"]) == ("West Bengal", "farmer", 2)
    assert (hi["profile"]["state"], hi["profile"]["occupation"], hi["profile"]["land_area_acres"]) == ("Bihar", "farmer", 2)
    assert en["profile"]["state"] == "West Bengal" and en["extraction_source"] == "claude"


def test_groq_output_is_still_strictly_validated(groq, client):
    groq(args_override={"record_profile": {"occupation": "wizard", "age": -3, "state": "West Bengal",
                                           "land_area_acres": "৩", "bogus": 1, "has_aadhaar": "maybe"}})
    p = post(client, "x", "bn").json()["profile"]
    assert p["state"] == "West Bengal" and p["land_area_acres"] == 3   # Bengali digit accepted
    assert p["occupation"] is None and p["age"] is None and p["has_aadhaar"] is None  # invalid fields dropped


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '"a string"'])
def test_malformed_tool_arguments_are_invalid_response(groq, client, bad):
    groq(args_override={"record_profile": bad})
    r = post(client, "x", "bn")
    assert r.status_code == 503 and r.json()["error"]["code"] == "invalid_response"


def test_response_without_tool_call_is_invalid_response(groq, client):
    groq(raw_body={"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "hi"}}]})
    r = post(client, "x", "bn")
    assert r.status_code == 503 and r.json()["error"]["code"] == "invalid_response"


def test_garbage_http_body_is_invalid_response(groq, client):
    groq(raw_body={"unexpected": True})
    assert post(client, "x", "hi").json()["error"]["code"] == "invalid_response"


# 7. API failure -> api_error / timeout (never "language unsupported")
@pytest.mark.parametrize("kw,code", [
    ({"status": 500}, "api_error"), ({"status": 429}, "rate_limited"), ({"status": 401}, "api_error"),
    ({"raise_exc": httpx.ConnectError("no route")}, "api_error"),
    ({"raise_exc": httpx.ReadTimeout("slow")}, "timeout"),
])
def test_groq_failures_map_to_codes(groq, client, kw, code):
    groq(**kw)
    r = post(client, BN, "bn")
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == code and e["language"] == "bn"
    assert "unsupported" not in e["message"].lower() and "only english" not in e["message"].lower()


def test_groq_failure_english_degrades_but_is_reported(groq, client):
    groq(status=500)
    d = post(client, "I am a farmer from West Bengal with 2 acres of land.").json()
    assert d["extraction_source"] == "offline_english" and d["ai"]["code"] == "api_error"


def test_explanation_failure_keeps_engine_results(monkeypatch, groq, client):
    mock = groq(profiles=PROFILES)
    orig = mock.handle
    mock.transport = httpx.MockTransport(lambda r: httpx.Response(500, json={}) if b"localized_text" in r.content[:400] + r.content[-400:] and json.loads(r.content)["tool_choice"]["function"]["name"] == "localized_text" else orig(r))
    real = httpx.Client
    monkeypatch.setattr(llm.httpx, "Client", lambda **k: real(**{**k, "transport": mock.transport}))
    d = post(client, BN, "bn").json()
    assert d["extraction_source"] == "claude" and d["ai"]["code"] == "api_error" and d["report"]["results"]


# 8. missing key
def test_missing_groq_key_gives_missing_api_key(monkeypatch, client):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-irrelevant")  # a key for the OTHER provider must not be used
    assert llm.ai_configured() is False
    r = post(client, BN, "bn")
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == "missing_api_key" and "not configured" in e["message"]
    assert "only english" not in e["message"].lower() and "unsupported" not in e["message"].lower()
    d = post(client, "I am a farmer from West Bengal.", "en").json()
    assert d["ai"]["code"] == "missing_api_key" and d["extraction_source"] == "offline_english"


def test_missing_key_is_logged_naming_the_variable(monkeypatch, caplog):
    monkeypatch.setenv("LLM_PROVIDER", "groq"); monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="scheme_navigator.ai"):
        with pytest.raises(AIError):
            get_client()
    assert "GROQ_API_KEY" in caplog.text and "provider=groq" in caplog.text


def test_unknown_provider_is_config_error_not_language_blame(monkeypatch, client):
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    r = post(client, BN, "bn")
    assert r.status_code == 503 and r.json()["error"]["code"] == "config_error"


# 9. Anthropic still works
def test_anthropic_is_still_the_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    c = get_client()
    assert isinstance(c, AnthropicProvider) and c.name == "anthropic"
    assert c.model == "claude-sonnet-5" and llm.provider_name() == "anthropic"


def test_anthropic_ignores_groq_model_setting(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy"); monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    assert get_client().model == "claude-sonnet-5"


def test_anthropic_missing_key_still_missing_api_key(monkeypatch, client):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert post(client, BN, "bn").json()["error"]["code"] == "missing_api_key"


def test_anthropic_end_to_end_via_provider_abstraction(monkeypatch, client, use_claude):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    use_claude(profiles=PROFILES)      # anthropic-SDK-shaped fake, wrapped by call_tool()
    d = post(client, BN, "bn").json()
    assert d["profile"]["state"] == "West Bengal" and d["ai"]["ok"] is True


# 10. the LLM cannot change eligibility; providers are interchangeable
def test_llm_text_cannot_change_status_groq(groq, client):
    groq(profiles={"txt": {"occupation": "student", "state": "Bihar"}},
         args_override={"localized_text": {"summary": "ELIGIBLE for everything!", "follow_up_questions": [],
                                           "scheme_explanations": {"krishak_bandhu": "ELIGIBLE, apply now"}}})
    d = post(client, "txt", "hi").json()
    assert {r["scheme_id"]: r["status"] for r in d["report"]["results"]}["krishak_bandhu"] == "NOT_ELIGIBLE"


def test_report_identical_across_providers_and_languages(groq, client, monkeypatch, use_claude):
    text = "I own 2 acres of land in West Bengal."
    prof = {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2, "land_ownership": "owner"}
    llm_out = {**prof, "land_ownership_evidence": "I own"}
    groq(profiles={text: llm_out})
    via_groq = {c: post(client, text, c).json()["report"] for c in ("en", "bn", "hi", "ta")}
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    use_claude(profiles={text: llm_out})
    via_anthropic = post(client, text, "bn").json()["report"]
    direct = json.loads(evaluate(Profile(**prof)).model_dump_json())
    for rep in [*via_groq.values(), via_anthropic]:
        assert rep == direct


def test_eligibility_engine_is_independent_of_the_llm_layer():
    import pathlib
    import ast
    tree = ast.parse(pathlib.Path("backend/eligibility_engine.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    banned = ("llm", "anthropic", "httpx", "openai", "explainer", "profile_extractor")
    assert not [m for m in imported if any(b in m for b in banned)], imported
