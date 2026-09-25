"""Regression tests for the stale-profile bug: a new statement must never inherit an old profile."""
import json

A = "I am a student from Bihar."
B = "I am a farmer from West Bengal. I have 2 acres of land."
B_ACTUALLY = "Actually, I am a farmer from West Bengal and I have 3 acres."

PROFILES = {
    A: {"state": "Bihar", "occupation": "student"},
    B: {"state": "West Bengal", "occupation": "farmer", "land_area_acres": 2},
    B_ACTUALLY: {"state": "West Bengal", "occupation": "farmer", "land_area_acres": 3},
    "I own 5 acres now": {"land_area_acres": 5},
}


def post(client, text, **extra):
    r = client.post("/api/analyze", json={"text": text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_second_statement_does_not_inherit_bihar_student(client, use_claude):
    use_claude(profiles=PROFILES)
    a = post(client, A)
    assert (a["profile"]["state"], a["profile"]["occupation"]) == ("Bihar", "student")
    b = post(client, B)
    p = b["profile"]
    assert p["occupation"] == "farmer" and p["state"] == "West Bengal" and p["land_area_acres"] == 2
    assert p["occupation"] != "student" and p["state"] != "Bihar"


def test_actually_phrasing_is_still_a_fresh_profile(client, use_claude):
    use_claude(profiles=PROFILES)
    post(client, A)
    p = post(client, B_ACTUALLY)["profile"]
    assert (p["occupation"], p["state"], p["land_area_acres"]) == ("farmer", "West Bengal", 3)


def test_stale_profile_sent_by_client_is_ignored_in_new_mode(client, use_claude):
    """Even a buggy client that still ships the old profile cannot leak it into a fresh request."""
    use_claude(profiles=PROFILES)
    a = post(client, A)
    b = post(client, B, profile=a["profile"], asked=["old question?"])  # mode defaults to "new"
    assert b["mode"] == "new"
    assert b["profile"]["state"] == "West Bengal" and b["profile"]["occupation"] == "farmer"


def test_fresh_request_to_claude_contains_only_current_input(client, use_claude):
    fake = use_claude(profiles=PROFILES)
    a = post(client, A)
    post(client, B, profile=a["profile"], asked=["old question?"])
    req = fake.requests_for("record_profile")[-1]
    sent = json.dumps([req["messages"], req["system"]], default=str)  # (the tool schema legitimately lists "student")
    for stale in ("Bihar", "student", "old question"):
        assert stale not in sent
    assert len(fake.requests_for("record_profile")[-1]["messages"]) == 1  # no conversation history


def test_server_holds_no_state_between_requests(client, use_claude):
    use_claude(profiles=PROFILES)
    post(client, A)
    b1 = post(client, B)
    b2 = post(client, B)
    assert b1["profile"] == b2["profile"]


def test_explicit_update_merges_and_still_sends_no_profile_to_claude(client, use_claude):
    fake = use_claude(profiles=PROFILES)
    b = post(client, B)
    u = post(client, "I own 5 acres now", mode="update", profile=b["profile"])
    p = u["profile"]
    assert u["mode"] == "update" and p["land_area_acres"] == 5
    assert p["state"] == "West Bengal" and p["occupation"] == "farmer"  # kept because user asked to update
    req = fake.requests_for("record_profile")[-1]
    assert "West Bengal" not in json.dumps([req["messages"], req["system"]], default=str)


def test_fresh_profile_also_works_on_offline_english_path(client):
    a = post(client, A)["profile"]
    assert (a["state"], a["occupation"]) == ("Bihar", "student")
    b = post(client, "I am a farmer from West Bengal with 2 acres of land.", profile=a)["profile"]
    assert (b["state"], b["occupation"], b["land_area_acres"]) == ("West Bengal", "farmer", 2)


def test_eligibility_recomputed_from_scratch(client, use_claude):
    use_claude(profiles=PROFILES)
    post(client, A)
    by = {r["scheme_id"]: r["status"] for r in post(client, B)["report"]["results"]}
    assert by["krishak_bandhu"] != "NOT_ELIGIBLE"  # would be NOT_ELIGIBLE if Bihar/student leaked
