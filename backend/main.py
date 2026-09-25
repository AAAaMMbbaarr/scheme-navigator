"""FastAPI app. Run:  uvicorn backend.main:app --reload

The API is STATELESS. Nothing about a previous request is stored on the server; the
only way an earlier profile can influence a request is if the client explicitly sends it
with mode="update".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import AfterValidator, BaseModel, Field, field_validator  # noqa: E402

from . import ratelimit  # noqa: E402
from .eligibility_engine import evaluate, load_schemes  # noqa: E402
from .explainer import localize  # noqa: E402
from .languages import BY_CODE, LANGUAGES  # noqa: E402
from .llm import USER_MESSAGES, AIError, ai_configured, provider_model, provider_name  # noqa: E402
from .models import EligibilityReport, Profile  # noqa: E402
from .profile_extractor import extract_profile, merge_profiles  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scheme_navigator")

FRONTEND = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Scheme Navigator")
SCHEMES = load_schemes()  # fail fast on invalid scheme data
# .env is read once, at import. Log what THIS process sees so a stale server is obvious (never the key).
logger.info("LLM provider=%s model=%s key_configured=%s env_file=%s (found=%s)", provider_name(), provider_model(),
            ai_configured(), Path(__file__).parent.parent / ".env", (Path(__file__).parent.parent / ".env").exists())


def _check_language(code: str) -> str:
    if code not in BY_CODE:
        raise ValueError(f"unsupported language '{code}'; supported: {', '.join(BY_CODE)}")
    return code


Lang = Annotated[str, AfterValidator(_check_language)]

MAX_BODY_BYTES = 64 * 1024   # a real request is < 3 KB; refuse absurd bodies before parsing them


class TooManyRequests(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after


@app.exception_handler(TooManyRequests)
async def _too_many(_: Request, exc: TooManyRequests) -> JSONResponse:
    secs = max(1, int(exc.retry_after + 0.999))
    return JSONResponse(status_code=429, headers={"Retry-After": str(secs)}, content={"error": {
        "code": "too_many_requests", "retry_after_seconds": secs,
        "message": f"Too many requests. Please wait {secs} seconds and try again."}})


def public_guard(request: Request) -> None:
    """Lightweight abuse protection for the endpoints that cost LLM calls. Health/static/read-only routes are exempt."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise BodyTooLarge()
    wait = ratelimit.check(ratelimit.client_key(request.headers.get("x-forwarded-for"),
                                                request.client.host if request.client else None))
    if wait is not None:
        raise TooManyRequests(wait)


class BodyTooLarge(Exception):
    pass


@app.exception_handler(BodyTooLarge)
async def _too_large(_: Request, __: BodyTooLarge) -> JSONResponse:
    return JSONResponse(status_code=413, content={"error": {"code": "payload_too_large", "message": "Request is too large."}})


class AnalyzeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: Lang = "en"
    # "new" (default): a fresh profile from this text only. `profile` and `asked` are ignored.
    # "update": the user explicitly adds to / corrects `profile`.
    mode: Literal["new", "update"] = "new"
    profile: Profile | None = None
    asked: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=10)


    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text is empty")
        return v


class ExplainRequest(BaseModel):
    """Re-explain an existing profile in another language (no new user text involved)."""
    profile: Profile
    language: Lang = "en"


class AIStatus(BaseModel):
    ok: bool = True
    code: str | None = None       # see backend/llm.py USER_MESSAGES
    message: str | None = None    # English, safe to show


class AnalyzeResponse(BaseModel):
    profile: Profile
    mode: str
    language: str
    extraction_source: str        # "claude" | "offline_english" | "provided"
    report: EligibilityReport
    localized: dict
    ai: AIStatus


def _ai_status(*codes: str | None) -> AIStatus:
    code = next((c for c in codes if c), None)
    if code is None:
        return AIStatus()
    return AIStatus(ok=False, code=code, message=USER_MESSAGES.get(code))


def _error(err: AIError, language: str) -> JSONResponse:
    """User-safe error: a stable code and a friendly message. Provider details stay in the server log."""
    # 503 = the AI provider is unavailable. (HTTP 429 is reserved for OUR per-client limiter, see public_guard.)
    headers = {"Retry-After": "30"} if err.code == "rate_limited" else None
    return JSONResponse(status_code=503, headers=headers, content={
        "error": {"code": err.code, "message": err.user_message, "language": language}})


@app.post("/api/analyze", response_model=AnalyzeResponse, dependencies=[Depends(public_guard)])
def analyze(req: AnalyzeRequest):
    logger.info("analyze mode=%s language=%s chars=%d", req.mode, req.language, len(req.text))
    try:
        if req.mode == "update":
            ext = extract_profile(req.text, req.language, asked=req.asked or None)
            profile = merge_profiles(req.profile or Profile(), ext.profile)
        else:  # fresh: nothing from any earlier request is used
            ext = extract_profile(req.text, req.language)
            profile = ext.profile
    except AIError as err:  # non-English input and Claude unavailable: cannot proceed honestly
        return _error(err, req.language)

    report = evaluate(profile, SCHEMES)  # deterministic; the LLM has no say here
    loc = localize(report, req.language)
    return AnalyzeResponse(
        profile=profile, mode=req.mode, language=req.language, extraction_source=ext.source,
        report=report, localized=loc,
        ai=_ai_status(ext.error.code if ext.error else None, loc["error_code"]))


@app.post("/api/explain", response_model=AnalyzeResponse, dependencies=[Depends(public_guard)])
def explain(req: ExplainRequest) -> AnalyzeResponse:
    report = evaluate(req.profile, SCHEMES)
    loc = localize(report, req.language)
    return AnalyzeResponse(profile=req.profile, mode="explain", language=req.language,
                           extraction_source="provided", report=report, localized=loc,
                           ai=_ai_status(loc["error_code"]))


@app.get("/api/languages")
def languages() -> list[dict]:
    return [{"code": l.code, "english": l.english, "native": l.native, "locale": l.locale, "rtl": l.rtl}
            for l in LANGUAGES]


@app.get("/api/schemes")
def schemes() -> list[dict]:
    return [{"id": s.id, "name": s.name, "official_url": s.official_url,
             "last_verified": s.last_verified.isoformat()} for s in SCHEMES]


@app.get("/health")
def health_check() -> dict:
    """Render health check: constant-time, touches no LLM, no config, no disk."""
    return {"status": "ok"}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "schemes": len(SCHEMES), "ai_configured": ai_configured(), "ai_provider": provider_name(), "ai_model": provider_model()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
