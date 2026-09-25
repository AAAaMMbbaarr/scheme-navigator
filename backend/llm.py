"""LLM access behind one small abstraction, with an explicit failure taxonomy.

    get_client()  ->  Provider           (chosen by LLM_PROVIDER)
    call_tool()   ->  dict               (forces one tool call, returns its arguments)

Providers:
    anthropic  the Anthropic SDK (default, unchanged behaviour)
    groq       Groq's OpenAI-compatible chat-completions API

The rest of the app only sees `get_client()` / `call_tool()` and AIError, so it does not know
which provider is active. Every failure becomes an AIError with a stable `code`. API keys are
never logged or returned: we log only the error code and the exception *type*.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Protocol

import anthropic
import httpx

logger = logging.getLogger("scheme_navigator.ai")

TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT", os.getenv("LLM_TIMEOUT", "25")))
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"

# code -> message safe to show users (English; the UI localizes the generic ones)
USER_MESSAGES = {
    "missing_api_key": "The AI service is not configured on this server (no API key).",
    "config_error": "The AI service is misconfigured on this server (check LLM_PROVIDER).",
    "api_error": "AI service is temporarily unavailable. Please try again or use English mode.",
    "rate_limited": "The AI service is busy right now (rate limit reached). Please wait a minute and try again.",
    "timeout": "The AI service took too long to answer. Please try again.",
    "invalid_response": "The AI returned an unexpected answer. Please try again.",
    "translation_failure": "Some explanations could not be translated; those are shown in English.",
}


class AIError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def user_message(self) -> str:
        return USER_MESSAGES.get(self.code, USER_MESSAGES["api_error"])


class Provider(Protocol):
    name: str
    model: str

    def complete_tool(self, *, system: str, tool: dict, user_content: str, max_tokens: int) -> dict:
        """Force a call of `tool` (Anthropic-style dict with input_schema) and return its arguments.
        Raises AIError for every failure."""


# --------------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any, model: str | None = None):
        self.client = client
        self.model = model or os.getenv("CLAUDE_MODEL", ANTHROPIC_DEFAULT_MODEL)

    def complete_tool(self, *, system: str, tool: dict, user_content: str, max_tokens: int) -> dict:
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system, tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.APITimeoutError as exc:
            raise AIError("timeout", type(exc).__name__) from None
        except anthropic.APIStatusError as exc:
            raise AIError("api_error", f"{type(exc).__name__} status={exc.status_code}") from None
        except anthropic.APIError as exc:
            raise AIError("api_error", type(exc).__name__) from None
        except Exception as exc:  # unexpected client/network failure
            raise AIError("api_error", type(exc).__name__) from None
        block = next((b for b in getattr(resp, "content", []) if getattr(b, "type", "") == "tool_use"), None)
        if block is None:
            raise AIError("invalid_response", f"no tool_use block (stop_reason={getattr(resp, 'stop_reason', '?')})")
        if not isinstance(block.input, dict):
            raise AIError("invalid_response", f"tool input is {type(block.input).__name__}, not an object")
        return block.input


# --------------------------------------------------------------------------- OpenAI-compatible (Groq)
MAX_RETRY_AFTER = 5.0   # seconds: retry a 429 once only if the provider asks for a short wait


class OpenAICompatProvider:
    """Chat-completions with forced function calling. Works with Groq and other OpenAI-compatible hosts.

    The tool's JSON schema is passed through unchanged, and the returned arguments are handed back
    to the same strict validators the Anthropic path uses: nothing is weakened for this provider.
    """

    def __init__(self, *, name: str, base_url: str, api_key: str, model: str,
                 timeout: float = TIMEOUT_SECONDS, reasoning_effort: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.name, self.model = name, model
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self._api_key = api_key            # only ever placed in the Authorization header
        self._timeout, self._transport = timeout, transport

    def _post(self, body: dict) -> httpx.Response:
        """POST with a short-lived client (closed every time: a public service must not leak sockets).
        A 429 is retried once if the provider asks for a short wait; otherwise it becomes `rate_limited`.
        Only the status code is ever recorded: never the response body or the request headers."""
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                    resp = http.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self._api_key}"})
                if resp.status_code == 429 and attempt == 1:
                    wait = _retry_after_seconds(resp)
                    if wait is not None and wait <= MAX_RETRY_AFTER:
                        time.sleep(wait)
                        continue
                resp.raise_for_status()
                return resp
            except httpx.TimeoutException as exc:
                raise AIError("timeout", type(exc).__name__) from None
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise AIError("rate_limited" if status == 429 else "api_error", f"HTTPStatusError status={status}") from None
            except Exception as exc:
                raise AIError("api_error", type(exc).__name__) from None
        raise AIError("rate_limited", "HTTPStatusError status=429")   # unreachable in practice

    def complete_tool(self, *, system: str, tool: dict, user_content: str, max_tokens: int) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            # reasoning models spend hidden tokens before answering; leave headroom so output isn't cut off
            "max_completion_tokens": max_tokens * 4,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            "tools": [{"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": tool["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        resp = self._post(body)

        try:
            data = resp.json()
            choice = data["choices"][0]
            calls = choice["message"].get("tool_calls") or []
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AIError("invalid_response", "response is not a chat-completions object") from None
        call = next((c for c in calls if isinstance(c, dict) and c.get("function", {}).get("name") == tool["name"]), None)
        if call is None:
            raise AIError("invalid_response", f"no '{tool['name']}' tool call (finish_reason={choice.get('finish_reason')})")
        try:
            args = json.loads(call["function"]["arguments"])
        except (ValueError, KeyError, TypeError):
            raise AIError("invalid_response", "tool arguments are not valid JSON") from None
        if not isinstance(args, dict):
            raise AIError("invalid_response", f"tool arguments are {type(args).__name__}, not an object")
        return args


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return None


# --------------------------------------------------------------------------- configuration
def provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower() or "anthropic"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def ai_configured() -> bool:
    p = provider_name()
    if p == "groq":
        return bool(_env("GROQ_API_KEY"))
    if p == "anthropic":
        return bool(_env("ANTHROPIC_API_KEY"))
    return False


def provider_model() -> str:
    """Model the active provider will use (no client is built, no key is read)."""
    p = provider_name()
    if p == "groq":
        return _env("LLM_MODEL") or GROQ_DEFAULT_MODEL
    if p == "anthropic":
        return os.getenv("CLAUDE_MODEL", ANTHROPIC_DEFAULT_MODEL)
    return ""


def get_client() -> Provider:
    """Build the configured provider. Raises AIError(missing_api_key | config_error)."""
    p = provider_name()
    try:
        if p == "groq":
            key = _env("GROQ_API_KEY")
            if not key:
                raise AIError("missing_api_key", "GROQ_API_KEY is not set (check .env / environment)")
            return OpenAICompatProvider(
                name="groq", api_key=key,
                base_url=_env("LLM_BASE_URL") or GROQ_BASE_URL,
                model=_env("LLM_MODEL") or GROQ_DEFAULT_MODEL,
                reasoning_effort=_env("LLM_REASONING_EFFORT") or None)
        if p == "anthropic":
            key = _env("ANTHROPIC_API_KEY")
            if not key:
                raise AIError("missing_api_key", "ANTHROPIC_API_KEY is not set (check .env / environment)")
            return AnthropicProvider(anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1))
        raise AIError("config_error", f"unknown LLM_PROVIDER '{p}' (use 'groq' or 'anthropic')")
    except AIError as err:
        logger.warning("LLM unavailable (provider=%s): %s", p, err)
        raise


def call_tool(client: Any, *, system: str, tool: dict, user_content: str, max_tokens: int, purpose: str) -> dict:
    """Force a tool call and return its arguments dict. Raises AIError on any failure.

    `client` is a Provider; a bare Anthropic-SDK-shaped client is wrapped for convenience.
    """
    provider = client if hasattr(client, "complete_tool") else AnthropicProvider(client)
    try:
        return provider.complete_tool(system=system, tool=tool, user_content=user_content, max_tokens=max_tokens)
    except AIError as err:
        logger.warning("LLM call failed (%s, provider=%s): %s", purpose, provider.name, err)
        raise
