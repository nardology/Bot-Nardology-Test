"""utils/cost_tracker.py

Revenue-linked cost caps (Phase 4).

Tracks estimated daily AI spend per guild in Redis and enforces a hard daily
cap per guild. This is the "never lose money" backstop: even if all other
budget layers are misconfigured, no single guild can cost more than the cap.

Storage:
  - Redis key: "cost:guild:{guild_id}:{YYYYMMDD}" (value in milli-cents for precision)
  - TTL: 2 days (auto-cleanup)

Cost rates are approximate and configurable via env vars. Update them when
OpenAI changes pricing.
"""

from __future__ import annotations

import os
import time

from utils.backpressure import get_redis_or_none
from utils.redis_kv import incr


# ---------------------------------------------------------------------------
# Cost-per-token rates (configurable via env, defaults match Feb 2026 pricing)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


# Pro model (gpt-4.1-mini): $0.40 / 1M input, $1.60 / 1M output
COST_PER_INPUT_TOKEN_PRO = _env_float("AI_COST_PER_INPUT_TOKEN_PRO", 0.0000004)
COST_PER_OUTPUT_TOKEN_PRO = _env_float("AI_COST_PER_OUTPUT_TOKEN_PRO", 0.0000016)

# Free model (gpt-4.1-nano): $0.10 / 1M input, $0.40 / 1M output
COST_PER_INPUT_TOKEN_FREE = _env_float("AI_COST_PER_INPUT_TOKEN_FREE", 0.0000001)
COST_PER_OUTPUT_TOKEN_FREE = _env_float("AI_COST_PER_OUTPUT_TOKEN_FREE", 0.0000004)

_TTL = 2 * 24 * 3600  # 2 days


def _today_utc() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


def _cost_key(guild_id: int, day: str) -> str:
    return f"cost:guild:{int(guild_id)}:{day}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_cost_cents(*, tier: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the cost of an AI call in cents (USD).

    Returns a float (e.g. 0.032 = 0.032 cents).
    """
    tier_l = (tier or "").strip().lower()
    if tier_l == "pro":
        rate_in = COST_PER_INPUT_TOKEN_PRO
        rate_out = COST_PER_OUTPUT_TOKEN_PRO
    else:
        rate_in = COST_PER_INPUT_TOKEN_FREE
        rate_out = COST_PER_OUTPUT_TOKEN_FREE

    cost_dollars = (max(0, input_tokens) * rate_in) + (max(0, output_tokens) * rate_out)
    return cost_dollars * 100  # convert to cents


async def record_cost(
    *,
    guild_id: int,
    tier: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record the estimated cost of an AI call for this guild (today).

    Cost is stored in milli-cents (1/1000 of a cent) for precision.
    """
    cost_cents = estimate_cost_cents(
        tier=tier, input_tokens=input_tokens, output_tokens=output_tokens,
    )
    if cost_cents <= 0:
        return

    milli_cents = int(cost_cents * 1000)
    if milli_cents <= 0:
        return

    day = _today_utc()
    key = _cost_key(guild_id, day)
    await incr(key, milli_cents, ex=_TTL)


async def get_today_cost_cents(guild_id: int) -> float:
    """Return the estimated AI cost for this guild today, in cents.

    Returns 0.0 if Redis is unavailable (degrade safely).
    """
    r = await get_redis_or_none()
    if r is None:
        return 0.0

    day = _today_utc()
    key = _cost_key(guild_id, day)
    try:
        val = await r.get(key)
        if val is None:
            return 0.0
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="ignore")
        milli_cents = int(val)
        return milli_cents / 1000.0
    except Exception:
        return 0.0


async def is_within_budget(guild_id: int, tier: str) -> tuple[bool, float, float]:
    """Check if the guild is within its daily cost cap.

    Returns (allowed, current_cents, cap_cents).
    If Redis is unavailable, returns (True, 0, cap) to degrade safely.
    """
    import config

    tier_l = (tier or "").strip().lower()
    if tier_l == "pro":
        cap = float(getattr(config, "AI_COST_CAP_PRO_DAILY_CENTS", 50))
    else:
        cap = float(getattr(config, "AI_COST_CAP_FREE_DAILY_CENTS", 5))

    # Cap of 0 means disabled (no limit)
    if cap <= 0:
        return True, 0.0, 0.0

    current = await get_today_cost_cents(guild_id)
    return current < cap, current, cap
