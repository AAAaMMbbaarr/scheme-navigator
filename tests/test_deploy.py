"""Production-readiness checks for a single-service Render deployment."""
import ast
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

import backend.llm as llm
from backend import ratelimit
from backend.llm import AIError, OpenAICompatProvider

ROOT = Path(__file__).parent.parent
SECRET = "gsk_PROD_SECRET_abcdefghijklmnopqrstuvwxyz0123456789"
START = "uvicorn backend.main:app --host 0.0.0.0 --port $PORT"
BN = "আমি পশ্চিমবঙ্গের একজন কৃষক।"
SHIPPED = [p for p in ROOT.rglob("*") if p.is_file() and not any(
    part in {"node_modules", ".git", "__pycache__", ".pytest_cache", "tests", ".kilo"} for part in p.relative_to(ROOT).parts)
    and p.name not in {".env", "package-lock.json"} and p.suffix not in {".pyc", ".log"}]


# ------------------------------------------------------------------ entrypoint / start command / Render blueprint
def test_entrypoint_is_backend_main_app():
    app = importlib.import_module("backend.main").app
    assert isinstance(app, FastAPI)


def render_service():
    return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]


def test_render_blueprint_commands_and_health_path():
    svc = render_service()
    assert svc["type"] == "web" and svc["runtime"] == "python"
    assert svc["startCommand"] == START            # host 0.0.0.0, port from $PORT, real module path
    assert svc["buildCommand"] == "pip install -r requirements.txt"
    assert svc["healthCheckPath"] == "/health"
    assert "8000" not in svc["startCommand"]
    paths = {r.path for r in importlib.import_module("backend.main").app.routes}
    assert "/health" in paths


def test_render_blueprint_env_vars_and_secret_handling():
    env = {e["key"]: e for e in render_service()["envVars"]}
    assert env["LLM_PROVIDER"]["value"] == "groq"
    assert env["LLM_BASE_URL"]["value"] == "https://api.groq.com/openai/v1"
    assert env["LLM_MODEL"]["value"] == "openai/gpt-oss-20b"
    assert env["GROQ_API_KEY"] == {"key": "GROQ_API_KEY", "sync": False}     # no value in the repo
    assert (ROOT / ".python-version").read_text().strip() == env["PYTHON_VERSION"]["value"]


def test_every_blueprint_env_var_is_one_the_app_actually_reads():
    code = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "backend").glob("*.py"))
    read = set(re.findall(r'(?:getenv|_env)\("([A-Z_]+)"', code))
    for key in render_service()["envVars"]:
        assert key["key"] in read | {"PYTHON_VERSION"}, key["key"]


# ------------------------------------------------------------------ requirements
def _req_names(path):
    return {re.split(r"[=<>\[ ]", l.strip(), 1)[0].lower() for l in path.read_text().splitlines()
            if l.strip() and not l.startswith(("#", "-"))}


def test_runtime_requirements_cover_every_third_party_import():
    mods = set()
    for f in (ROOT / "backend").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    third_party = {m for m in mods if m not in sys.stdlib_module_names and m != "backend"}
    dist = {"yaml": "pyyaml", "dotenv": "python-dotenv"}
    names = _req_names(ROOT / "requirements.txt")
    missing = {dist.get(m, m).lower() for m in third_party} - names
    assert not missing, missing
    assert "uvicorn" in names                        # the server itself (not imported by the app)


def test_test_tools_are_not_shipped_to_production_and_versions_are_pinned():
    prod = (ROOT / "requirements.txt").read_text()
    assert "pytest" not in prod
    for line in prod.splitlines():
        if line.strip() and not line.startswith("#"):
            assert "==" in line, f"unpinned: {line}"
    dev = (ROOT / "requirements-dev.txt").read_text()
    assert "-r requirements.txt" in dev and "pytest" in dev


# ------------------------------------------------------------------ secrets
def test_env_is_gitignored_and_no_secret_shaped_strings_are_shipped():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    if shutil.which("git"):
        r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
        assert r.returncode == 0, ".env is not ignored by git"
    pattern = re.compile(r"gsk_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{30,}|Bearer [A-Za-z0-9_-]{30,}")
    assert SHIPPED, "scan found no files"
    for f in SHIPPED:
        assert not pattern.search(f.read_text(encoding="utf-8", errors="ignore")), f"secret-like string in {f}"
    assert "GROQ_API_KEY=\n" in (ROOT / ".env.example").read_text()                  # template holds no value


def test_app_never_needs_a_local_env_file(monkeypatch, tmp_path):
    """Config comes from the process environment; the dotenv step is optional and silent."""
    monkeypatch.setenv("LLM_PROVIDER", "groq"); monkeypatch.setenv("GROQ_API_KEY", SECRET)
    assert not (tmp_path / ".env").exists()
    c = llm.get_client()
    assert c.name == "groq" and c.model == "openai/gpt-oss-20b" and c.base_url == "https://api.groq.com/openai/v1"


# ------------------------------------------------------------------ frontend is production-safe
def test_no_dev_only_urls_or_ports_in_shipped_code():
    for f in SHIPPED:
        if f.suffix in {".py", ".js", ".html", ".css", ".yaml"}:
            text = f.read_text(encoding="utf-8")
            assert not re.search(r"localhost|127\.0\.0\.1|0\.0\.0\.0:\d|:8000\b", text.replace("--host 0.0.0.0", "")), f


def test_frontend_calls_only_same_origin_root_relative_paths():
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    calls = re.findall(r'(?:fetch|post)\(\s*["\']([^"\']+)', js)
    assert calls and all(c.startswith("/api/") for c in calls), calls
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert all(u.startswith("/static/") for u in re.findall(r'(?:src|href)="(/[^"]+)"', html)), "non-static asset path"


def test_no_cors_configuration_single_origin(client):
    assert "CORSMiddleware" not in (ROOT / "backend" / "main.py").read_text()
    r = client.get("/api/languages", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_homepage_and_all_static_assets_are_served(client):
    home = client.get("/")
    assert home.status_code == 200 and "text/html" in home.headers["content-type"]
    for path in sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', home.text))):
        if "fonts.googleapis" in path:
            continue
        r = client.get(path)
        assert r.status_code == 200 and len(r.content) > 0, path
    assert "javascript" in client.get("/static/app.js").headers["content-type"]


# ------------------------------------------------------------------ health
def test_health_is_constant_and_never_touches_the_llm(client, use_claude, monkeypatch):
    fake = use_claude()
    monkeypatch.setenv("LLM_PROVIDER", "definitely-not-configured")     # even a broken config must not matter
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert fake.calls == []


# ------------------------------------------------------------------ the browser never sees secrets
def test_no_endpoint_or_static_file_ever_returns_the_key_or_config(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq"); monkeypatch.setenv("GROQ_API_KEY", SECRET)
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": f"bad key {r.headers['authorization']}"}}))
    real = httpx.Client
    monkeypatch.setattr(llm.httpx, "Client", lambda **k: real(**{**k, "transport": transport}))
    responses = [client.get(p) for p in ("/", "/health", "/api/health", "/api/languages", "/api/schemes", "/static/app.js", "/static/strings.js")]
    responses += [client.post("/api/analyze", json={"text": BN, "language": "bn"}),
                  client.post("/api/analyze", json={"text": "farmer", "language": "en"}),
                  client.post("/api/explain", json={"profile": {}, "language": "hi"})]
    for r in responses:
        blob = r.text + json.dumps(dict(r.headers))
        assert SECRET not in blob and "Authorization" not in blob and "gsk_" not in blob
    health = client.get("/api/health").json()
    assert set(health) <= {"ok", "schemes", "ai_configured", "ai_provider", "ai_model"}


# ------------------------------------------------------------------ Groq failures are safe and friendly
def _provider_with(monkeypatch, handler):
    monkeypatch.setenv("LLM_PROVIDER", "groq"); monkeypatch.setenv("GROQ_API_KEY", SECRET)
    real = httpx.Client
    calls = []
    def wrapped(request):
        calls.append(request)
        return handler(request, len(calls))
    monkeypatch.setattr(llm.httpx, "Client", lambda **k: real(**{**k, "transport": httpx.MockTransport(wrapped)}))
    return calls


def test_groq_429_becomes_rate_limited_with_friendly_message(client, monkeypatch):
    calls = _provider_with(monkeypatch, lambda r, n: httpx.Response(429, json={"error": {"message": "Rate limit reached for org_secret123"}}))
    r = client.post("/api/analyze", json={"text": BN, "language": "bn"})
    assert r.status_code == 503 and r.headers["retry-after"]
    e = r.json()["error"]
    assert e["code"] == "rate_limited" and "busy" in e["message"] and "wait" in e["message"].lower()
    assert "org_secret123" not in r.text and SECRET not in r.text
    assert len(calls) == 1                                   # no Retry-After from Groq -> no retry


def test_groq_429_with_short_retry_after_is_retried_once_then_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    def handler(r, n):
        if n == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": "t", "arguments": "{\"a\": 1}"}}]}}]})
    calls = _provider_with(monkeypatch, handler)
    out = llm.get_client().complete_tool(system="s", tool={"name": "t", "input_schema": {}}, user_content="x", max_tokens=5)
    assert out == {"a": 1} and len(calls) == 2 and slept == [2.0]


@pytest.mark.parametrize("retry_after", ["60", "abc", None])
def test_groq_429_with_long_or_missing_retry_after_is_not_retried(monkeypatch, retry_after):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    headers = {"retry-after": retry_after} if retry_after else {}
    calls = _provider_with(monkeypatch, lambda r, n: httpx.Response(429, headers=headers, json={}))
    with pytest.raises(AIError) as e:
        llm.get_client().complete_tool(system="s", tool={"name": "t", "input_schema": {}}, user_content="x", max_tokens=5)
    assert e.value.code == "rate_limited" and len(calls) == 1 and slept == []


def test_two_consecutive_429s_give_up_after_one_retry(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = _provider_with(monkeypatch, lambda r, n: httpx.Response(429, headers={"retry-after": "1"}, json={}))
    with pytest.raises(AIError) as e:
        llm.get_client().complete_tool(system="s", tool={"name": "t", "input_schema": {}}, user_content="x", max_tokens=5)
    assert e.value.code == "rate_limited" and len(calls) == 2


@pytest.mark.parametrize("handler, code", [
    (lambda r, n: httpx.Response(500, text="upstream exploded secret-detail"), "api_error"),
    (lambda r, n: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "timeout"),
    (lambda r, n: httpx.Response(200, json={"choices": [{"message": {"content": "no tools"}}]}), "invalid_response"),
    (lambda r, n: httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": "record_profile", "arguments": "{oops"}}]}}]}), "invalid_response"),
])
def test_other_groq_failures_map_to_safe_codes_without_provider_details(client, monkeypatch, handler, code):
    _provider_with(monkeypatch, handler)
    r = client.post("/api/analyze", json={"text": BN, "language": "bn"})
    assert r.status_code == 503 and r.json()["error"]["code"] == code
    assert "secret-detail" not in r.text and "exploded" not in r.text and SECRET not in r.text


def test_provider_uses_a_fresh_short_lived_client_each_call(monkeypatch):
    """A long-running public service must not leak sockets: every call opens and closes its own client."""
    opened, closed = [], []
    real = httpx.Client
    class Tracking(real):
        def __init__(self, **k): super().__init__(**{**k, "transport": httpx.MockTransport(lambda r: httpx.Response(500))}); opened.append(1)
        def __exit__(self, *exc): closed.append(1); return super().__exit__(*exc)
    monkeypatch.setattr(llm.httpx, "Client", Tracking)
    p = OpenAICompatProvider(name="groq", base_url="https://x.test/v1", api_key="k", model="m")
    for _ in range(3):
        with pytest.raises(AIError):
            p.complete_tool(system="s", tool={"name": "t", "input_schema": {}}, user_content="x", max_tokens=5)
    assert len(opened) == 3 and len(closed) == 3


# ------------------------------------------------------------------ public-demo abuse protection
def test_rate_limit_returns_429_with_retry_after_and_is_per_client(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3"); ratelimit.reset()
    body = {"text": "I am a farmer from West Bengal.", "language": "en"}
    for _ in range(3):
        assert client.post("/api/analyze", json=body).status_code == 200
    r = client.post("/api/analyze", json=body)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "too_many_requests"
    other = client.post("/api/analyze", json=body, headers={"X-Forwarded-For": "203.0.113.7"})
    assert other.status_code == 200                                          # a different client has its own budget
    for path in ("/health", "/api/languages", "/"):                          # cheap routes are never limited
        assert client.get(path).status_code == 200


def test_forged_leading_forwarded_for_entries_cannot_dodge_the_limit(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2"); ratelimit.reset()
    body = {"text": "I am a farmer.", "language": "en"}
    codes = [client.post("/api/analyze", json=body, headers={"X-Forwarded-For": f"{i}.{i}.{i}.{i}, 198.51.100.9"}).status_code for i in range(1, 5)]
    assert codes == [200, 200, 429, 429]                                     # the proxy-appended LAST hop is what counts


def test_rate_limit_applies_to_explain_and_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1"); ratelimit.reset()
    assert client.post("/api/explain", json={"profile": {}, "language": "en"}).status_code == 200
    assert client.post("/api/explain", json={"profile": {}, "language": "en"}).status_code == 429
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    assert all(client.post("/api/explain", json={"profile": {}, "language": "en"}).status_code == 200 for _ in range(5))


def test_limiter_window_expires_and_memory_is_bounded(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2"); ratelimit.reset()
    assert ratelimit.check("a", now=0) is None and ratelimit.check("a", now=1) is None
    assert ratelimit.check("a", now=2) > 0
    assert ratelimit.check("a", now=61.5) is None                            # window slid
    monkeypatch.setattr(ratelimit, "MAX_TRACKED_CLIENTS", 10)
    for i in range(100):
        ratelimit.check(f"client{i}", now=100 + i * 0.01)
    assert len(ratelimit._hits) <= 10


def test_bad_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "lots")
    assert ratelimit.limit_per_minute() == ratelimit.DEFAULT_LIMIT_PER_MINUTE


def test_input_size_limits(client):
    assert client.post("/api/analyze", json={"text": "x" * 2001}).status_code == 422
    assert client.post("/api/analyze", json={"text": "hi", "pad": "a" * 70000}).status_code == 413
    assert client.post("/api/analyze", json={"text": "hi", "mode": "update", "profile": {}, "asked": ["q" * 301]}).status_code == 422
    assert client.post("/api/explain", json={"profile": {"state": "s" * 101}, "language": "en"}).status_code == 422
    assert client.post("/api/analyze", json={"text": "   "}).status_code == 422


# ------------------------------------------------------------------ public disclaimer (site-wide, all languages fall back to English)
def test_site_wide_disclaimer_is_present_and_translated():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="site-disclaimer"' in html
    for phrase in ("not a government website", "not an official determination", "verify"):
        assert phrase in html
    strings = (ROOT / "frontend" / "strings.js").read_text(encoding="utf-8")
    assert strings.count("siteDisclaimer:") >= 3                              # en, bn, hi
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert app.count("site-disclaimer") == 1                                  # set on language change, never cleared by a reset


# ------------------------------------------------------------------ nothing is persisted
def test_app_is_stateless_no_disk_writes_no_database():
    for f in (ROOT / "backend").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"sqlite|sqlalchemy|shelve|pickle|open\([^)]*[\"']w|write_text|write_bytes|\.mkdir", text), f


# ------------------------------------------------------------------ the critical safeguards survive deployment changes
def test_ownership_safeguard_still_behaves(client, use_claude):
    ambiguous, explicit = "मेरे पास 2 एकड़ जमीन है।", "मैं 2 एकड़ जमीन का मालिक हूँ।"
    use_claude(profiles={
        ambiguous: {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": "मेरे पास"},   # model misbehaves
        explicit: {"land_area_acres": 2, "land_ownership": "owner", "land_ownership_evidence": "मालिक"}})
    a = client.post("/api/analyze", json={"text": ambiguous, "language": "hi"}).json()["profile"]
    b = client.post("/api/analyze", json={"text": explicit, "language": "hi"}).json()["profile"]
    assert (a["land_area_acres"], a["land_ownership"]) == (2, None)
    assert (b["land_area_acres"], b["land_ownership"]) == (2, "owner")
