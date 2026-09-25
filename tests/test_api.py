FARMER_TEXT = ("I am a farmer from West Bengal. I own 2 acres of agricultural land. "
               "I have Aadhaar and a bank account.")


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Scheme Navigator" in r.text


def test_farmer_end_to_end(client):
    r = client.post("/api/analyze", json={"text": FARMER_TEXT})
    assert r.status_code == 200
    d = r.json()
    assert d["profile"]["state"] == "West Bengal" and d["profile"]["land_area_acres"] == 2
    assert d["extraction_source"] == "offline_english" and d["ai"]["code"] == "missing_api_key"
    by = {x["scheme_id"]: x for x in d["report"]["results"]}
    assert by["krishak_bandhu"]["status"] == "LIKELY_ELIGIBLE"
    for x in by.values():
        assert x["official_url"].startswith("https://") and x["last_verified"]
    docs = [c["document"] for c in d["report"]["checklist"]]
    assert len(docs) == len({x.lower() for x in docs})
    assert "informational" in d["report"]["disclaimer"]


def test_missing_info_generates_followup(client):
    d = client.post("/api/analyze", json={"text": "I am a farmer from West Bengal."}).json()
    by = {x["scheme_id"]: x for x in d["report"]["results"]}
    assert by["pm_kisan"]["status"] == "MISSING_INFORMATION"
    assert d["report"]["follow_up_questions"]


def test_followup_answer_merges_profile_only_in_update_mode(client):
    first = client.post("/api/analyze", json={"text": "I am a farmer from West Bengal."}).json()
    second = client.post("/api/analyze", json={"text": "I own 3 acres", "mode": "update",
                                               "profile": first["profile"]}).json()
    assert second["profile"]["state"] == "West Bengal" and second["profile"]["land_area_acres"] == 3


def test_rejects_oversized_input(client):
    assert client.post("/api/analyze", json={"text": "x" * 5000}).status_code == 422
