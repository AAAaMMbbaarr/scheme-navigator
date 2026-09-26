import copy
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

from backend.eligibility_engine import SCHEMES_DIR, evaluate, evaluate_scheme, load_schemes
from backend.explainer import localize, validate_localized
from backend.models import Profile, Scheme, Status
from backend.profile_extractor import extract_profile, merge_profiles, parse_llm_profile

FARMER = Profile(state="West Bengal", occupation="farmer", land_area_acres=2,
                 land_ownership="owner", has_aadhaar=True, has_bank_account=True)


def make_scheme(**over) -> Scheme:
    base = {
        "id": "t", "name": "Test", "state": "India", "description": "d",
        "eligibility_rules": [
            {"field": "occupation", "operator": "equals", "value": "farmer", "description": "farmer"},
            {"field": "age", "operator": "gte", "value": 18, "description": "adult"},
        ],
        "required_documents": ["Aadhaar"], "official_url": "https://example.gov.in/",
        "last_verified": "2026-01-01",
    }
    base.update(over)
    return Scheme.model_validate(base)


# 1. Profile extraction schema
def test_profile_rejects_unknown_and_bad_fields():
    with pytest.raises(Exception):
        Profile(favourite_colour="red")
    with pytest.raises(Exception):
        Profile(age=-4)
    assert Profile().model_dump() == {k: None for k in Profile.model_fields}


def test_merge_never_erases_known_values():
    merged = merge_profiles(FARMER, Profile(crop="rice"))
    assert merged.state == "West Bengal" and merged.crop == "rice"


# 2/4. Eligibility rules + eligible case
def test_eligible_case():
    r = evaluate_scheme(make_scheme(), Profile(occupation="farmer", age=30))
    assert r.status == Status.ELIGIBLE and len(r.matched_rules) == 2


def test_manual_verification_gives_likely_not_eligible():
    s = make_scheme(manual_verification=[{"description": "check land record"}])
    r = evaluate_scheme(s, Profile(occupation="farmer", age=30))
    assert r.status == Status.LIKELY_ELIGIBLE and r.manual_checks == ["check land record"]


# 5. Ineligible
def test_ineligible_case():
    r = evaluate_scheme(make_scheme(), Profile(occupation="student", age=30))
    assert r.status == Status.NOT_ELIGIBLE and r.failed_rules == ["farmer"]


def test_failed_rule_beats_missing_info():
    r = evaluate_scheme(make_scheme(), Profile(occupation="student"))
    assert r.status == Status.NOT_ELIGIBLE


# 3. Missing information
def test_missing_required_field_asks_question_not_verdict():
    report = evaluate(Profile(occupation="farmer"), [make_scheme()])
    assert report.results[0].status == Status.MISSING_INFORMATION
    assert report.follow_up_fields == ["age"]
    assert "old" in report.follow_up_questions[0]


def test_missing_verify_field_is_likely_and_not_asked():
    s = make_scheme(eligibility_rules=[
        {"field": "occupation", "operator": "equals", "value": "farmer", "description": "farmer"},
        {"field": "is_income_taxpayer", "operator": "is_false", "importance": "verify", "description": "no tax"},
    ])
    report = evaluate(Profile(occupation="farmer"), [s])
    assert report.results[0].status == Status.LIKELY_ELIGIBLE
    assert report.follow_up_fields == []


def test_unknown_never_treated_as_false_or_true():
    s = make_scheme(eligibility_rules=[
        {"field": "has_aadhaar", "operator": "is_false", "description": "x"}])
    assert evaluate_scheme(s, Profile()).status == Status.MISSING_INFORMATION


# 6. Document deduplication
def test_documents_deduplicated_and_have_flag():
    a = make_scheme(id="a", name="A", required_documents=["Aadhaar", "Land record"])
    b = make_scheme(id="b", name="B", required_documents=["aadhaar ", "Photo"])
    report = evaluate(Profile(occupation="farmer", age=30, has_aadhaar=True), [a, b])
    docs = [c.document.casefold() for c in report.checklist]
    assert docs.count("aadhaar") == 1
    aad = next(c for c in report.checklist if c.document.casefold() == "aadhaar")
    assert aad.have is True and sorted(aad.needed_for) == ["A", "B"]


def test_checklist_skips_ineligible_schemes():
    a = make_scheme(required_documents=["Special doc"])
    report = evaluate(Profile(occupation="student", age=30), [a])
    assert report.checklist == []


# 7. Invalid / unknown LLM output
@pytest.mark.parametrize("raw", [None, "junk", 5, [], {"age": "abc"}, {"occupation": "wizard"},
                                 {"unknown": 1}, {"has_aadhaar": {"x": 1}}])
def test_bad_llm_output_never_crashes(raw):
    p = parse_llm_profile(raw)
    assert isinstance(p, Profile)


def test_bad_field_dropped_good_field_kept():
    p = parse_llm_profile({"occupation": "wizard", "state": "West Bengal", "age": -3})
    assert p.occupation is None and p.age is None and p.state == "West Bengal"


class FakeClient:
    def __init__(self, payload=None, boom=False):
        self.messages = SimpleNamespace(create=self._create)
        self.payload, self.boom = payload, boom

    def _create(self, **kw):
        if self.boom:
            raise RuntimeError("api down")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.payload)])


def test_extract_uses_claude_output_validated():
    ext = extract_profile("x", client=FakeClient({"state": "West Bengal", "occupation": "farmer",
                                                 "age": "not a number", "bogus": 1}))
    assert ext.source == "llm" and ext.profile.occupation == "farmer" and ext.profile.age is None


def test_extract_falls_back_when_api_fails():
    ext = extract_profile("I am a farmer from West Bengal. I own 2 acres. I have Aadhaar and a bank account.",
                          client=FakeClient(boom=True))
    p = ext.profile
    assert ext.source == "offline_english" and ext.error.code == "api_error"
    assert p.state == "West Bengal" and p.land_area_acres == 2 and p.land_ownership == "owner"
    assert p.has_aadhaar is True and p.has_bank_account is True


def test_llm_cannot_influence_status_in_localization():
    report = evaluate(FARMER)
    statuses = [r.status for r in report.results]
    payload = {"summary": "s", "follow_up_questions": report.follow_up_questions,
               "scheme_explanations": {"pm_kisan": "ELIGIBLE for sure!", "evil": "x"}}
    out = localize(report, "hi", client=FakeClient(payload))
    assert out["localized"] and out["error_code"] == "translation_failure"  # partial: some ids missing
    assert "evil" not in out["scheme_explanations"]
    assert [r.status for r in report.results] == statuses  # untouched


def test_localize_rejects_wrong_shape_and_falls_back():
    report = evaluate(Profile())
    assert validate_localized({"summary": "s", "follow_up_questions": ["only one"] * 99,
                               "scheme_explanations": {}}, report) is None
    out = localize(report, "bn", client=FakeClient({"nonsense": True}))
    assert out["localized"] is False and out["error_code"] == "invalid_response"


# 8. Scheme data validation (real files)
def test_all_scheme_files_valid():
    schemes = load_schemes()
    assert len(schemes) >= 3
    for s in schemes:
        assert s.official_url.startswith("https://")
        assert isinstance(s.last_verified, date)
        assert s.required_documents and s.eligibility_rules and s.verification_notes


def test_invalid_scheme_data_rejected():
    good = yaml.safe_load((SCHEMES_DIR / "pm_kisan.yaml").read_text(encoding="utf-8"))
    for mutate in (
        lambda d: d.pop("last_verified"),
        lambda d: d.update(official_url="http://insecure"),
        lambda d: d["eligibility_rules"][0].update(field="not_a_field"),
        lambda d: d["eligibility_rules"][0].update(operator="roughly"),
        lambda d: d.update(required_documents=[]),
        lambda d: d.update(eligibility_rules=[]),
    ):
        d = copy.deepcopy(good)
        mutate(d)
        with pytest.raises(Exception):
            Scheme.model_validate(d)


# Farmer example against the real scheme files
def test_farmer_example_real_schemes():
    report = evaluate(FARMER)
    by_id = {r.scheme_id: r for r in report.results}
    assert by_id["pm_kisan"].status == Status.LIKELY_ELIGIBLE
    assert by_id["krishak_bandhu"].status == Status.LIKELY_ELIGIBLE
    assert by_id["pmfby"].status == Status.LIKELY_ELIGIBLE
    assert sum(c.document == "Aadhaar" for c in report.checklist) == 1


def test_student_not_eligible_for_farm_schemes():
    report = evaluate(Profile(state="Bihar", occupation="student"))
    by_id = {r.scheme_id: r.status for r in report.results}
    assert by_id["krishak_bandhu"] == Status.NOT_ELIGIBLE  # wrong state and occupation
    assert by_id["pmfby"] == Status.NOT_ELIGIBLE
    assert by_id["pm_kisan"] == Status.MISSING_INFORMATION  # PM-KISAN depends on land, not occupation


def test_lakshmir_bhandar_age_and_gender_rules():
    woman = Profile(state="West Bengal", gender="female", age=30)
    by = {r.scheme_id: r for r in evaluate(woman).results}
    assert by["lakshmir_bhandar"].status == Status.LIKELY_ELIGIBLE  # manual checks remain
    young = evaluate(Profile(state="West Bengal", gender="female", age=20))
    assert {r.scheme_id: r for r in young.results}["lakshmir_bhandar"].status == Status.NOT_ELIGIBLE
    man = evaluate(Profile(state="West Bengal", gender="male", age=30))
    assert {r.scheme_id: r for r in man.results}["lakshmir_bhandar"].status == Status.NOT_ELIGIBLE


def test_govt_employee_excluded_from_pm_kisan():
    p = FARMER.model_copy(update={"is_govt_employee": True})
    assert {r.scheme_id: r for r in evaluate(p).results}["pm_kisan"].status == Status.NOT_ELIGIBLE


def test_every_scheme_has_source_and_date_and_note():
    for s in load_schemes():
        assert s.official_url and s.last_verified and s.verification_notes.strip()
