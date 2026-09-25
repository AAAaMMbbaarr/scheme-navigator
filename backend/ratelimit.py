"""Tiny in-memory per-client rate limiter for the public demo (no accounts, no CAPTCHA, no storage).

State lives in process memory only: it resets on restart and is per instance, which is fine for a single
free-tier web service. Nothing about users is persisted.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

WINDOW_SECONDS = 60.0
MAX_TRACKED_CLIENTS = 5000          # bound memory even under a flood of distinct addresses
DEFAULT_LIMIT_PER_MINUTE = 20       # each analysis makes ~2 LLM calls, so this is ~40 calls/min/client at most

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}


def limit_per_minute() -> int:
    """Read on every request so it can be tuned (or disabled with 0) through the environment."""
    try:
        return int(os.getenv("RATE_LIMIT_PER_MINUTE", str(DEFAULT_LIMIT_PER_MINUTE)))
    except ValueError:
        return DEFAULT_LIMIT_PER_MINUTE


def client_key(forwarded_for: str | None, peer: str | None) -> str:
    """Render (like most proxies) APPENDS the real client address to X-Forwarded-For, so the LAST entry is the
    one our own proxy vouches for; earlier entries can be forged by the caller."""
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last[:64]
    return (peer or "unknown")[:64]


def check(key: str, now: float | None = None) -> float | None:
    """Record a hit. Returns None if allowed, else the number of seconds until the client may retry."""
    limit = limit_per_minute()
    if limit <= 0:
        return None
    now = time.monotonic() if now is None else now
    with _lock:
        if key not in _hits and len(_hits) >= MAX_TRACKED_CLIENTS:
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > WINDOW_SECONDS]:
                del _hits[k]
            if len(_hits) >= MAX_TRACKED_CLIENTS:      # still full of active clients: drop the oldest bucket
                del _hits[next(iter(_hits))]
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, WINDOW_SECONDS - (now - q[0]))
        q.append(now)
        return None


def reset() -> None:
    with _lock:
        _hits.clear()
