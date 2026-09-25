"""Conservative extraction: having land is not owning land.

Architecture under test:  text -> conservative extraction -> UNKNOWN when evidence is insufficient
                          -> eligibility rules -> follow-up question if ownership matters.
The deterministic eligibility engine is not changed to compensate for extraction.
"""
import json
import logging

import httpx
import pytest

from backend import ownership
from backend.llm import OpenAICompatProvider
from backend.models import Profile
from backend.profile_extractor import EXTRACT_TOOL, SYSTEM, extract_profile, parse_llm_profile

# ---- the wordings from the bug report --------------------------------------------------------
AMBIGUOUS = {
    "en": ["I have 2 acres of land.", "I have 2 acres of my land.", "My 2 acres of land are in the village.",
           "I cultivate 2 acres of land.", "I farm 2 acres."],
    "bn": ["আমার ২ একর জমি আছে।", "আমি ২ একর জমিতে চাষ করি।"],
    "hi": ["मेरे पास 2 एकड़ जमीन है।", "मेरी 2 एकड़ जमीन है।", "मैं 2 एकड़ जमीन पर खेती करता हूँ।"],
}
EXPLICIT_OWNER = {
    "en": ["I own 2 acres of land.", "I am the owner of 2 acres.", "2 acres of land is in my name."],
    "bn": ["আমার নামে ২ একর জমি আছে।", "আমি ২ একর জমির মালিক।"],
    "hi": ["मेरे नाम पर 2 एकड़ जमीन है।", "मैं 2 एकड़ जमीन का मालिक हूँ।"],
}
TENANT = {"en": "I rented 2 acres of land.", "hi": "मैंने 2 एकड़ जमीन किराए पर ली है।", "bn": "আমি ২ একর জমি ভাড়ায় নিয়েছি।"}
SHARECROPPER = {"en": "I am a bargadar and farm 2 acres.", "hi": "मैं बटाई पर 2 एकड़ जमीन पर खेती करता हूँ।",
                "bn": "আমি ২ একর জমিতে বর্গাদার হিসেবে চাষ করি।"}
# words a well-behaved model would quote
EVIDENCE = {"I own 2 acres of land.": "I own", "I am the owner of 2 acres.": "the owner",
            "2 acres of land is in my name.": "in my name", "আমার নামে ২ একর জমি আছে।": "আমার নামে",
            "আমি ২ একর জমির মালিক।": "মালিক", "मेरे नाम पर 2 एकड़ जमीन है।": "मेरे नाम पर",
            "मैं 2 एकड़ जमीन का मालिक हूँ।": "मालिक", TENANT["en"]: "rented", TENANT["hi"]: "किराए पर",
            TENANT["bn"]: "ভাড়ায়", SHARECROPPER["en"]: "bargadar", SHARECROPPER["hi"]: "बटाई पर",
            SHARECROPPER["bn"]: "বর্গাদার"}


class FakeClient:
    def __init__(self, payload):
        from types import SimpleNamespace
        self.payload = payload
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=self.payload)], stop_reason="tool_use"))


def extract(text, payload, language="en"):
    return extract_profile(text, language, client=FakeClient(payload)).profile


ALL_AMBIGUOUS = [(lang, t) for lang, ts in AMBIGUOUS.items() for t in ts]


# ============================================================ 1. the deterministic reader
@pytest.mark.parametrize("lang,text", ALL_AMBIGUOUS)
def test_possession_or_farming_is_not_ownership(lang, text):
    assert ownership.detect(text) is None


@pytest.mark.parametrize("text", [t for ts in EXPLICIT_OWNER.values() for t in ts])
def test_explicit_ownership_wording_is_owner(text):
    assert ownership.detect(text) == "owner"


@pytest.mark.parametrize("lang", ["en", "hi", "bn"])
def test_tenancy_and_sharecropping_are_recognised(lang):
    assert ownership.detect(TENANT[lang]) == "tenant"
    assert ownership.detect(SHARECROPPER[lang]) == "sharecropper"


@pytest.mark.parametrize("text", [
    "I don't own any land, but I have 2 acres.", "I do not own the land I farm.", "I am not the owner of 2 acres.",
    "मैं 2 एकड़ जमीन का मालिक नहीं हूँ।", "আমি ২ একর জমির মালিক নই না।",
    "I own 2 acres and rent 3 more.",                       # two different tenure kinds: ambiguous
])
def test_negated_or_mixed_statements_are_unknown(text):
    assert ownership.detect(text) is None


def test_bengali_naame_is_not_mistaken_for_negation_na():
    assert ownership.detect("আমার নামে ২ একর জমি আছে।") == "owner"


# ============================================================ 2. the guard vs a MISBEHAVING model
@pytest.mark.parametrize("lang,text", ALL_AMBIGUOUS)
@pytest.mark.parametrize("evidence", [None, "", "   ", "owner", "made up words", "I have", "मेरे पास", "আমার ২ একর জমি আছে"])
def test_model_claiming_owner_on_ambiguous_text_is_overruled(lang, text, evidence):
    """The reported bug: the model 'infers' owner. Whatever evidence it offers, the ambiguous text can't prove it."""
    p = extract(text, {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": evidence}, lang)
    assert p.land_area_acres == 2                       # the amount is kept ...
    assert p.land_ownership is None                     # ... the invented ownership is not


@pytest.mark.parametrize("lang,text", ALL_AMBIGUOUS)
def test_model_quoting_the_ambiguous_phrase_itself_as_evidence_is_overruled(lang, text):
    phrase = text.rstrip("।.")
    p = extract(text, {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": phrase}, lang)
    assert p.land_ownership is None


def test_no_evidence_field_at_all_is_not_accepted():
    assert extract("I own 2 acres of land.", {"land_ownership": "owner"}).land_ownership is None


def test_hallucinated_evidence_that_is_not_in_the_text_is_rejected():
    p = extract("I have 2 acres of land.", {"land_ownership": "owner", "land_ownership_evidence": "I own 2 acres"})
    assert p.land_ownership is None


def test_evidence_for_the_wrong_kind_is_rejected():
    p = extract("I rented 2 acres of land.", {"land_ownership": "owner", "land_ownership_evidence": "rented"})
    assert p.land_ownership is None                     # 'rented' proves tenant, not owner
    p = extract("I rented 2 acres of land.", {"land_ownership": "tenant", "land_ownership_evidence": "rented"})
    assert p.land_ownership == "tenant"


def test_negation_in_the_same_sentence_overrules_the_evidence():
    p = extract("I don't own any land, I have 2 acres.", {"land_ownership": "owner", "land_ownership_evidence": "own"})
    assert p.land_ownership is None


def test_without_the_user_text_ownership_is_never_accepted():
    assert parse_llm_profile({"land_ownership": "owner", "land_ownership_evidence": "I own"}).land_ownership is None


# ============================================================ 3. legitimate claims still work
@pytest.mark.parametrize("lang,text", [(l, t) for l, ts in EXPLICIT_OWNER.items() for t in ts])
def test_explicit_ownership_is_kept(lang, text):
    p = extract(text, {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": EVIDENCE[text]}, lang)
    assert (p.land_area_acres, p.land_ownership) == (2, "owner")


@pytest.mark.parametrize("lang", ["en", "hi", "bn"])
def test_tenancy_and_sharecropping_are_kept(lang):
    t = extract(TENANT[lang], {"land_area_acres": 2, "land_ownership": "tenant", "land_ownership_evidence": EVIDENCE[TENANT[lang]]}, lang)
    s = extract(SHARECROPPER[lang], {"land_area_acres": 2, "land_ownership": "sharecropper", "land_ownership_evidence": EVIDENCE[SHARECROPPER[lang]]}, lang)
    assert t.land_ownership == "tenant" and s.land_ownership == "sharecropper"


# ============================================================ 4. a compliant model + the full pipeline
@pytest.mark.parametrize("lang,text", [("en", "I have 2 acres of land."), ("bn", "আমার ২ একর জমি আছে।"),
                                       ("hi", "मेरे पास 2 एकड़ जमीन है।")])
def test_reported_cases_end_to_end_ownership_unknown_then_follow_up(client, use_claude, lang, text):
    use_claude(profiles={text: {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2,
                                "land_ownership": None, "land_ownership_evidence": None}})
    d = client.post("/api/analyze", json={"text": text, "language": lang}).json()
    assert d["profile"]["land_area_acres"] == 2 and d["profile"]["land_ownership"] is None
    by = {r["scheme_id"]: r for r in d["report"]["results"]}
    assert by["pm_kisan"]["status"] == "MISSING_INFORMATION" and "land_ownership" in by["pm_kisan"]["missing_fields"]
    assert any("own the land" in q for q in d["report"]["follow_up_questions"])       # engine asks; nothing assumed


@pytest.mark.parametrize("lang,text", [("en", "I have 2 acres of land."), ("bn", "আমার ২ একর জমি আছে।"),
                                       ("hi", "मेरे पास 2 एकड़ जमीन है।")])
def test_reported_cases_even_if_the_model_misbehaves_pipeline_stays_unknown(client, use_claude, lang, text):
    use_claude(profiles={text: {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2,
                                "land_ownership": "owner", "land_ownership_evidence": text.rstrip("।.")}})
    d = client.post("/api/analyze", json={"text": text, "language": lang}).json()
    assert d["profile"]["land_ownership"] is None
    assert {r["scheme_id"]: r["status"] for r in d["report"]["results"]}["pm_kisan"] == "MISSING_INFORMATION"


@pytest.mark.parametrize("lang,text", [("en", "I own 2 acres of land."), ("bn", "আমার নামে ২ একর জমি আছে।"),
                                       ("hi", "मेरे नाम पर 2 एकड़ जमीन है।")])
def test_explicit_ownership_end_to_end_moves_pm_kisan_forward(client, use_claude, lang, text):
    use_claude(profiles={text: {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2,
                                "land_ownership": "owner", "land_ownership_evidence": EVIDENCE[text]}})
    d = client.post("/api/analyze", json={"text": text, "language": lang}).json()
    assert d["profile"]["land_ownership"] == "owner"
    assert {r["scheme_id"]: r["status"] for r in d["report"]["results"]}["pm_kisan"] == "LIKELY_ELIGIBLE"


def test_a_tenant_is_not_eligible_for_pm_kisan_ownership_rule(client, use_claude):
    text = TENANT["hi"]
    use_claude(profiles={text: {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2,
                                "land_ownership": "tenant", "land_ownership_evidence": "किराए पर"}})
    d = client.post("/api/analyze", json={"text": text, "language": "hi"}).json()
    assert d["profile"]["land_ownership"] == "tenant"
    assert {r["scheme_id"]: r["status"] for r in d["report"]["results"]}["pm_kisan"] == "NOT_ELIGIBLE"  # engine rule, unchanged


def test_explicit_update_reply_can_supply_ownership(client, use_claude):
    use_claude(profiles={"I have 2 acres of land.": {"occupation": "farmer", "state": "West Bengal", "land_area_acres": 2},
                         "I own it.": {"land_ownership": "owner", "land_ownership_evidence": "I own"}})
    first = client.post("/api/analyze", json={"text": "I have 2 acres of land."}).json()
    assert first["profile"]["land_ownership"] is None
    second = client.post("/api/analyze", json={"text": "I own it.", "mode": "update", "profile": first["profile"]}).json()
    assert second["profile"]["land_ownership"] == "owner" and second["profile"]["land_area_acres"] == 2


# ============================================================ 5. offline English fallback obeys the same rules
@pytest.mark.parametrize("text", AMBIGUOUS["en"])
def test_offline_fallback_never_assumes_ownership(client, text):
    d = client.post("/api/analyze", json={"text": text}).json()
    assert d["extraction_source"] == "offline_english" and d["profile"]["land_ownership"] is None


def test_offline_fallback_explicit_and_tenancy():
    from backend.profile_extractor import _fallback_extract
    assert _fallback_extract("I own 2 acres of land.").land_ownership == "owner"
    assert _fallback_extract("I rented 2 acres of land.").land_ownership == "tenant"
    assert _fallback_extract("I am a bargadar with 2 acres.").land_ownership == "sharecropper"
    assert _fallback_extract("I don't own land. I have 2 acres.").land_ownership is None
    assert _fallback_extract("I have 2 acres of land.").land_area_acres == 2


# ============================================================ 6. the instruction the model actually receives
def test_tool_schema_demands_evidence_and_says_possession_is_not_ownership():
    props = EXTRACT_TOOL["input_schema"]["properties"]
    assert "land_ownership_evidence" in props
    d = props["land_ownership"]["description"]
    assert "EXPLICITLY" in d and "NOT" in d and "null" in d


def test_system_prompt_carries_the_reported_examples():
    for phrase in ("I have 2 acres of land", "मेरे पास 2 एकड़ जमीन है", "NOT establish ownership",
                   "मैं 2 एकड़ जमीन का मालिक हूँ", "मेरे नाम पर 2 एकड़ जमीन है", "किराए पर", "बटाई पर", "land_ownership_evidence"):
        assert phrase in SYSTEM, phrase


def test_groq_request_carries_the_evidence_schema_and_the_rule():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        args = {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": "मेरे पास"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "record_profile", "arguments": json.dumps(args, ensure_ascii=False)}}]}}]})

    provider = OpenAICompatProvider(name="groq", base_url="https://api.groq.com/openai/v1", api_key="k", model="m",
                                    transport=httpx.MockTransport(handler))
    text = "मेरे पास 2 एकड़ जमीन है।"
    p = extract_profile(text, "hi", client=provider).profile
    assert p.land_area_acres == 2 and p.land_ownership is None          # same guard on the Groq path
    assert "land_ownership_evidence" in seen["body"]["tools"][0]["function"]["parameters"]["properties"]
    assert "NOT establish ownership" in seen["body"]["messages"][0]["content"]


# ============================================================ 7. privacy: the drop is logged without the user's words
def test_drop_is_logged_without_user_text(caplog):
    text = "मेरे पास 2 एकड़ जमीन है।"
    with caplog.at_level(logging.INFO, logger="scheme_navigator.extract"):
        extract(text, {"land_ownership": "owner", "land_ownership_evidence": "मेरे पास"}, "hi")
    assert "dropped unsupported land_ownership" in caplog.text and "एकड़" not in caplog.text and "मेरे" not in caplog.text


def test_profile_schema_unchanged_by_the_evidence_field():
    assert "land_ownership_evidence" not in Profile.model_fields    # evidence is consumed by the guard, never stored
