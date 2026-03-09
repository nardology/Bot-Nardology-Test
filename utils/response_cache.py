"""utils/response_cache.py

Response caching for AI calls (Phase 5).

Caches AI responses for identical short prompts to eliminate redundant API
calls at scale. Only applies to "talk" mode with short prompts and no
conversation memory.

Safety rules:
  - Only caches "talk" mode (not scene/roleplay where variety matters).
  - Only caches short prompts (< 50 chars) where repetition is common.
  - Never caches when conversation memory is present (context changes output).
  - TTL of 12 hours so responses feel fresh.
  - 70% serve rate: only returns cached response ~70% of the time so users
    don't always get identical replies.

Storage:
  - Redis key: "ai:cache:{hash}" (value = response text, TTL = 12 hours)
"""

from __future__ import annotations

import hashlib
import os
import random

from utils.backpressure import get_redis_or_none


# ---------------------------------------------------------------------------
# Configuration (tunable via env)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


# Max prompt length to cache (longer prompts are too unique to benefit)
MAX_PROMPT_LENGTH = _env_int("AI_CACHE_MAX_PROMPT_LENGTH", 50)

# TTL for cached responses (seconds). Default: 12 hours.
CACHE_TTL_S = _env_int("AI_CACHE_TTL_S", 12 * 3600)

# Probability of serving a cache hit (0.0-1.0). Default: 0.7 (70%).
# Set to 1.0 to always serve cache, 0.0 to effectively disable.
CACHE_SERVE_RATE = _env_float("AI_CACHE_SERVE_RATE", 0.7)


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Normalize a prompt for cache key generation.

    Lowercase, strip whitespace, collapse internal whitespace.
    """
    return " ".join(text.lower().split())


def cache_key(character_id: str, user_prompt: str) -> str:
    """Generate a Redis cache key from character + prompt.

    Uses a truncated SHA-256 hash so keys are compact and collision-resistant.
    """
    normalized = _normalize(user_prompt)
    raw = f"{character_id}:{normalized}".encode("utf-8", errors="replace")
    h = hashlib.sha256(raw).hexdigest()[:16]
    return f"ai:cache:{h}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_cacheable(*, mode: str, user_prompt: str, has_memory: bool) -> bool:
    """Determine whether this request is eligible for caching.

    Returns False for scene mode, long prompts, or memory-backed conversations.
    """
    if (mode or "").strip().lower() != "talk":
        return False
    if has_memory:
        return False
    if len(user_prompt.strip()) > MAX_PROMPT_LENGTH:
        return False
    if not user_prompt.strip():
        return False
    return True


async def get_cached(character_id: str, user_prompt: str) -> str | None:
    """Try to retrieve a cached response.

    Returns the cached text or None. Applies the serve-rate probability:
    even on a cache hit, returns None ~30% of the time for variety.
    """
    r = await get_redis_or_none()
    if r is None:
        return None

    key = cache_key(character_id, user_prompt)
    try:
        val = await r.get(key)
        if val is None:
            return None

        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="ignore")

        text = str(val).strip()
        if not text:
            return None

        # Serve-rate randomization: skip cache hit some % of the time
        if random.random() > CACHE_SERVE_RATE:
            return None

        return text
    except Exception:
        return None


async def store_cached(character_id: str, user_prompt: str, response_text: str) -> None:
    """Store a response in cache.

    Only stores non-empty responses. TTL ensures automatic cleanup.
    """
    if not response_text or not response_text.strip():
        return

    r = await get_redis_or_none()
    if r is None:
        return

    key = cache_key(character_id, user_prompt)
    try:
        await r.set(key, response_text.strip(), ex=CACHE_TTL_S)
    except Exception:
        pass
