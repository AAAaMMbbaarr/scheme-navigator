import json

import pytest

from backend.languages import LANGUAGES, get_language

EXPECTED_LOCALES = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "mr": "mr-IN", "te": "te-IN", "ta": "ta-IN",
    "gu": "gu-IN", "ur": "ur-IN", "kn": "kn-IN", "or": "or-IN", "ml": "ml-IN",
}
EXPECTED_NATIVE = {"hi": "हिन्दी", "bn": "বাংলা", "mr": "मराठी", "te": "తెలుగు", "ta": "தமிழ்", "gu": "ગુજરાતી",
                   "ur": "اردو", "kn": "ಕನ್ನಡ", "or": "ଓଡ଼ିଆ", "ml": "മലയാളം", "en": "English"}

BN_TEXT = "আমি পশ্চিমবঙ্গের একজন কৃষক। আমার ২ একর জমি আছে।"
HI_TEXT = "मैं बिहार का किसान हूँ और मेरे पास 2 एकड़ जमीन है।"
PROFILES = {
    BN_TEXT: {"state": "West Bengal", "occupation": "farmer", "land_area_acres": 2},
    HI_TEXT: {"state": "Bihar", "occupation": "farmer", "land_area_acres": 2},
}


# B. all 11 languages map to the right locale
def test_eleven_languages_and_locales():
    assert len(LANGUAGES) == 11
    assert {l.code: l.locale for l in LANGUAGES} == EXPECTED_LOCALES
    assert {l.code: l.native for l in LANGUAGES} == EXPECTED_NATIVE


def test_only_urdu_is_rtl():
    assert [l.code for l in LANGUAGES if l.rtl] == ["ur"]


def test_languages_endpoint_is_the_single_source_for_the_dropdown(client):
    data = client.get("/api/languages").json()
    assert {l["code"]: l["locale"] for l in data} == EXPECTED_LOCALES


def test_unknown_language_rejected_with_422_not_silently_english(client):
    r = client.post("/api/analyze", json={"text": "hello", "language": "xx"})
    assert r.status_code == 422 and "unsupported language" in r.text
    with pytest.raises(KeyError):
        get_language("xx")


# C. Bengali extraction
def test_bengali_extraction(client, use_claude):
    use_claude(profiles=PROFILES)
    d = client.post("/api/analyze", json={"text": BN_TEXT, "language": "bn"}).json()
    p = d["profile"]
    assert (p["state"], p["occupation"], p["land_area_acres"]) == ("West Bengal", "farmer", 2)
    assert d["extraction_source"] == "llm" and d["ai"]["ok"] is True


# D. Hindi extraction
def test_hindi_extraction(client, use_claude):
    use_claude(profiles=PROFILES)
    p = client.post("/api/analyze", json={"text": HI_TEXT, "language": "hi"}).json()["profile"]
    assert (p["state"], p["occupation"], p["land_area_acres"]) == ("Bihar", "farmer", 2)


def test_bengali_digits_are_accepted_if_model_passes_them_through():
    from backend.profile_extractor import parse_llm_profile
    assert parse_llm_profile({"land_area_acres": "২"}).land_area_acres == 2
    assert parse_llm_profile({"age": "३५"}).age == 35            # Devanagari
    assert parse_llm_profile({"age": "۳۵"}).age == 35            # Urdu


# E. selected language reaches both the extraction and the explanation layer
@pytest.mark.parametrize("code", list(EXPECTED_LOCALES))
def test_language_reaches_extraction_and_explanation(client, use_claude, code):
    fake = use_claude(profiles={"my text": {"occupation": "farmer", "state": "West Bengal"}})
    d = client.post("/api/analyze", json={"text": "my text", "language": code}).json()
    lang = get_language(code)
    ext_req = fake.requests_for("record_profile")[0]["messages"][0]["content"]
    exp_req = fake.requests_for("localized_text")[0]["messages"][0]["content"]
    for content in (ext_req, exp_req):
        assert f"Language: {lang.english} ({lang.locale})" in content
    assert d["language"] == code
    assert d["localized"]["summary"] == f"[{lang.english}] summary"
    assert all(v.startswith(f"[{lang.english}]") for v in d["localized"]["scheme_explanations"].values())


def test_language_switch_reexplains_without_reextracting(client, use_claude):
    fake = use_claude(profiles={"my text": {"occupation": "farmer", "state": "West Bengal"}})
    first = client.post("/api/analyze", json={"text": "my text", "language": "en"}).json()
    n_extract = len(fake.requests_for("record_profile"))
    d = client.post("/api/explain", json={"profile": first["profile"], "language": "bn"}).json()
    assert len(fake.requests_for("record_profile")) == n_extract  # no new extraction
    assert d["language"] == "bn" and d["localized"]["summary"] == "[Bengali] summary"
    assert d["profile"] == first["profile"]


def test_system_prompts_protect_scheme_names_and_forbid_deciding_eligibility(use_claude):
    from backend.explainer import SYSTEM as EXPLAIN
    from backend.profile_extractor import SYSTEM as EXTRACT
    assert "Do NOT translate" in EXPLAIN and "PM-KISAN" in EXPLAIN and "never change a status" in EXPLAIN
    assert "never guess or infer eligibility" in EXTRACT


# H. eligibility integrity: explanation language cannot change anything the engine decides
def test_changing_language_cannot_change_eligibility(client, use_claude):
    use_claude(profiles={"my text": {"occupation": "farmer", "state": "West Bengal",
                                     "land_area_acres": 2, "land_ownership": "owner"}})
    reports = {}
    for code in EXPECTED_LOCALES:
        d = client.post("/api/analyze", json={"text": "my text", "language": code}).json()
        reports[code] = d["report"]
    base = json.dumps(reports["en"], sort_keys=True)
    for code, rep in reports.items():
        assert json.dumps(rep, sort_keys=True) == base, f"report differs for {code}"
    ids = [r["scheme_id"] for r in reports["bn"]["results"]]
    assert "pm_kisan" in ids and reports["bn"]["checklist"]


def test_llm_claiming_eligibility_in_text_cannot_change_status(client, use_claude):
    use_claude(profiles={"my text": {"occupation": "student", "state": "Bihar"}},
               raw={"localized_text": {"summary": "You are ELIGIBLE for everything!", "follow_up_questions": [],
                                       "scheme_explanations": {"krishak_bandhu": "ELIGIBLE, apply now"}}})
    d = client.post("/api/analyze", json={"text": "my text", "language": "hi"}).json()
    by = {r["scheme_id"]: r["status"] for r in d["report"]["results"]}
    assert by["krishak_bandhu"] == "NOT_ELIGIBLE"  # the engine, not the LLM text, decides
