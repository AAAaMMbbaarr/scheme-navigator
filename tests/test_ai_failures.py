"""Claude failures must be diagnosable (specific codes), safe (no key leaks) and honest (never blame the language)."""
import logging

import anthropic
import httpx
import pytest

from backend.llm import AIError, call_tool
from tests.conftest import FakeClaude

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
BN = "আমি পশ্চিমবঙ্গের একজন কৃষক।"
BAD_WORDS = ("unsupported", "not supported", "language is")
# a statement that EXPLICITLY says who owns the land ("আমার নামে" = "in my name"), so an owner claim is legitimate
OWN_BN = "আমার নামে ২ একর জমি আছে।"


def _err_body(r):
    return r.json()["error"]


# ---- taxonomy: one code per failure kind ----
@pytest.mark.parametrize("exc, code", [
    (anthropic.APITimeoutError(request=REQ), "timeout"),
    (anthropic.APIConnectionError(request=REQ), "api_error"),
    (anthropic.InternalServerError("x", response=httpx.Response(500, request=REQ), body=None), "api_error"),
    (anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body=None), "api_error"),
    (RuntimeError("weird"), "api_error"),
])
def test_call_tool_error_codes(exc, code):
    with pytest.raises(AIError) as e:
        call_tool(FakeClaude(fail=exc), system="s", tool={"name": "record_profile"}, user_content="x",
                  max_tokens=10, purpose="t")
    assert e.value.code == code


def test_status_code_is_recorded_without_body():
    exc = anthropic.InternalServerError("secret body", response=httpx.Response(529, request=REQ), body=None)
    with pytest.raises(AIError) as e:
        call_tool(FakeClaude(fail=exc), system="s", tool={"name": "record_profile"}, user_content="x", max_tokens=10, purpose="t")
    assert "529" in e.value.detail and "secret body" not in e.value.detail


@pytest.mark.parametrize("payload", ["NO_TOOL_BLOCK", "not a dict", ["list"]])
def test_malformed_response_is_invalid_response(payload):
    with pytest.raises(AIError) as e:
        call_tool(FakeClaude(raw={"record_profile": payload}), system="s", tool={"name": "record_profile"},
                  user_content="x", max_tokens=10, purpose="t")
    assert e.value.code == "invalid_response"


# ---- F. API behaviour ----
def test_missing_key_non_english_gives_clear_error_not_language_blame(client):
    r = client.post("/api/analyze", json={"text": BN, "language": "bn"})
    assert r.status_code == 503
    e = _err_body(r)
    assert e["code"] == "missing_api_key" and e["language"] == "bn"
    assert "not configured" in e["message"] and not any(w in e["message"].lower() for w in BAD_WORDS)


def test_missing_key_english_still_works_but_is_reported(client):
    d = client.post("/api/analyze", json={"text": "I am a farmer from West Bengal."}).json()
    assert d["extraction_source"] == "offline_english"
    assert d["ai"] == {"ok": False, "code": "missing_api_key", "message": d["ai"]["message"]}
    assert d["profile"]["occupation"] == "farmer"


@pytest.mark.parametrize("exc, code", [
    (RuntimeError("down"), "api_error"),
    (anthropic.APITimeoutError(request=REQ), "timeout"),
])
def test_request_failure_gives_specific_code_and_friendly_message(client, use_claude, exc, code):
    use_claude(fail=exc)
    r = client.post("/api/analyze", json={"text": BN, "language": "hi"})
    assert r.status_code == 503
    e = _err_body(r)
    assert e["code"] == code and e["language"] == "hi"
    assert not any(w in e["message"].lower() for w in BAD_WORDS)
    if code == "api_error":
        assert "temporarily unavailable" in e["message"] and "English" in e["message"]


def test_malformed_extraction_reports_invalid_response(client, use_claude):
    use_claude(raw={"record_profile": "NO_TOOL_BLOCK"})
    r = client.post("/api/analyze", json={"text": BN, "language": "bn"})
    assert r.status_code == 503 and _err_body(r)["code"] == "invalid_response"


def test_extraction_ok_but_explanation_fails_still_returns_engine_results(client, use_claude):
    use_claude(profiles={OWN_BN: {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2,
                                  "land_ownership": "owner", "land_ownership_evidence": "আমার নামে"}},
               fail_tools=["localized_text"])
    d = client.post("/api/analyze", json={"text": OWN_BN, "language": "bn"}).json()
    assert d["extraction_source"] == "claude"  # extraction really came from Claude
    assert d["localized"]["localized"] is False and d["localized"]["error_code"] == "api_error"
    assert d["ai"]["ok"] is False and d["ai"]["code"] == "api_error"
    assert d["language"] == "bn"                                     # language preserved
    assert {r["scheme_id"]: r["status"] for r in d["report"]["results"]}["pm_kisan"] == "LIKELY_ELIGIBLE"


COMPLETE = {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2, "land_ownership": "owner",
            "land_ownership_evidence": "আমার নামে",
            "gender": "female", "age": 30, "is_govt_employee": False, "is_income_taxpayer": False}


def test_invalid_explanation_shape_is_invalid_response(client, use_claude):
    use_claude(profiles={OWN_BN: COMPLETE}, raw={"localized_text": {"nonsense": True}})
    d = client.post("/api/analyze", json={"text": OWN_BN, "language": "bn"}).json()
    assert d["ai"]["code"] == "invalid_response" and d["localized"]["localized"] is False
    assert d["extraction_source"] == "claude" and d["report"]["results"]


def test_wrong_number_of_translated_questions_is_rejected(client, use_claude):
    use_claude(profiles={BN: {"occupation": "farmer"}},   # leaves follow-up questions to translate
               raw={"localized_text": {"summary": "s", "follow_up_questions": [], "scheme_explanations": {}}})
    d = client.post("/api/analyze", json={"text": BN, "language": "bn"}).json()
    assert d["report"]["follow_up_questions"] and d["ai"]["code"] == "invalid_response"
    assert d["localized"]["follow_up_questions"] == d["report"]["follow_up_questions"]  # English fallback


def test_partial_translation_is_flagged_and_falls_back_per_scheme(client, use_claude):
    use_claude(profiles={OWN_BN: COMPLETE},
               raw={"localized_text": {"summary": "s", "follow_up_questions": [],
                                       "scheme_explanations": {"pmfby": "translated", "ghost": "x"}}})
    d = client.post("/api/analyze", json={"text": OWN_BN, "language": "bn"}).json()
    assert d["report"]["follow_up_questions"] == []
    assert d["ai"]["code"] == "translation_failure" and d["localized"]["localized"] is True
    ex = d["localized"]["scheme_explanations"]
    assert ex["pmfby"] == "translated" and "ghost" not in ex
    assert ex["pm_kisan"] == next(r["explanation"] for r in d["report"]["results"] if r["scheme_id"] == "pm_kisan")


def test_explain_endpoint_reports_failure_and_keeps_report(client, use_claude):
    use_claude(fail=RuntimeError("down"))
    d = client.post("/api/explain", json={"profile": {"occupation": "farmer", "state": "West Bengal"}, "language": "ta"}).json()
    assert d["ai"]["code"] == "api_error" and d["report"]["results"] and d["language"] == "ta"


# ---- the key never leaks ----
def test_api_key_never_logged_or_returned(client, use_claude, monkeypatch, caplog):
    secret = "sk-ant-SECRET-1234567890"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    use_claude(fail=RuntimeError(f"auth failed for key {secret}"))
    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/analyze", json={"text": BN, "language": "bn"})
        h = client.get("/api/health")
    assert secret not in r.text and secret not in h.text and secret not in caplog.text
    assert h.json()["ai_configured"] is True
    assert "api_error" in caplog.text  # ...but the failure IS diagnosable from logs


def test_failures_are_logged_with_code(client, use_claude, caplog):
    use_claude(fail=anthropic.APITimeoutError(request=REQ))
    with caplog.at_level(logging.WARNING, logger="scheme_navigator.ai"):
        client.post("/api/analyze", json={"text": BN, "language": "bn"})
    assert "timeout" in caplog.text and "extract[bn]" in caplog.text


def test_health_reports_ai_not_configured(client):
    assert client.get("/api/health").json()["ai_configured"] is False


# ---- G. unsupported speech language must never block typing ----
@pytest.mark.parametrize("code", ["or", "ur", "ml"])
def test_typed_input_works_in_every_language_regardless_of_speech_support(client, use_claude, code):
    use_claude(profiles={"typed text": {"occupation": "farmer", "state": "West Bengal"}})
    r = client.post("/api/analyze", json={"text": "typed text", "language": code})
    assert r.status_code == 200 and r.json()["profile"]["occupation"] == "farmer"
