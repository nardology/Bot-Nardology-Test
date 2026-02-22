"""core/safeguard.py

Anomaly detection + automatic global AI shutdown.

This is a "seatbelt". It's not your primary rate limiting (that's handled
by daily/weekly budgets, redis concurrency gating, and circuit breaker).
Instead, it protects against:
  - unexpected bypasses
  - accidental misconfiguration (e.g., huge concurrency caps)
  - abusive load spikes across many servers

Mechanics:
  - Sliding-window counters in Redis (10s buckets) for calls + tokens.
  - If thresholds are exceeded, we disable AI globally using utils.ai_kill.

Defaults are conservative and can be tuned via env vars.
"""

from __future__ import annotations

import os
import time

from utils.backpressure import get_redis_or_none
from utils.ai_kill import disable as disable_ai


WINDOW_S = 10


def _env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        v = default
    return max(min_value, v)


def _now() -> int:
    return int(time.time())


def _k(prefix: str, ident: str) -> str:
    return f"ai:sg:{WINDOW_S}s:{prefix}:{ident}"


async def check_and_record(*, guild_id: int, user_id: int, total_tokens: int) -> None:
    """Record this AI call and disable AI if thresholds are exceeded.

    This function must never raise.
    """
    r = await get_redis_or_none()
    if r is None:
        return

    # thresholds (tunable via env)
    global_calls_max = _env_int("AI_SAFEGUARD_GLOBAL_CALLS_PER_10S", 400, min_value=50)
    guild_calls_max = _env_int("AI_SAFEGUARD_GUILD_CALLS_PER_10S", 200, min_value=20)
    user_calls_max = _env_int("AI_SAFEGUARD_USER_CALLS_PER_10S", 50, min_value=10)

    global_tokens_max = _env_int("AI_SAFEGUARD_GLOBAL_TOKENS_PER_10S", 200_000, min_value=10_000)
    guild_tokens_max = _env_int("AI_SAFEGUARD_GUILD_TOKENS_PER_10S", 100_000, min_value=5_000)
    user_tokens_max = _env_int("AI_SAFEGUARD_USER_TOKENS_PER_10S", 25_000, min_value=2_000)

    # hard daily global tokens guardrail ("oops" protection)
    daily_global_tokens_max = _env_int("AI_SAFEGUARD_GLOBAL_TOKENS_PER_DAY", 10_000_000, min_value=100_000)

    # temporary shutdown duration
    shutdown_ttl_s = _env_int("AI_SAFEGUARD_SHUTDOWN_TTL_S", 3600, min_value=300)

    try:
        gid = str(int(guild_id))
        uid = str(int(user_id))
        tok = max(0, int(total_tokens or 0))

        # Increment counters (calls)
        g_calls = await r.incr(_k("calls:global", "all"))
        await r.expire(_k("calls:global", "all"), WINDOW_S + 2)

        guild_calls = await r.incr(_k("calls:guild", gid))
        await r.expire(_k("calls:guild", gid), WINDOW_S + 2)

        user_calls = await r.incr(_k("calls:user", uid))
        await r.expire(_k("calls:user", uid), WINDOW_S + 2)

        # Increment counters (tokens)
        g_tok = await r.incrby(_k("tokens:global", "all"), tok)
        await r.expire(_k("tokens:global", "all"), WINDOW_S + 2)

        guild_tok = await r.incrby(_k("tokens:guild", gid), tok)
        await r.expire(_k("tokens:guild", gid), WINDOW_S + 2)

        user_tok = await r.incrby(_k("tokens:user", uid), tok)
        await r.expire(_k("tokens:user", uid), WINDOW_S + 2)

        # Daily global tokens
        day = time.strftime("%Y%m%d", time.gmtime(_now()))
        day_k = f"ai:sg:day:{day}:tokens:global"
        day_tok = await r.incrby(day_k, tok)
        await r.expire(day_k, 86400 * 2)

        # Threshold checks
        triggered = None
        if int(g_calls or 0) > global_calls_max:
            triggered = f"GLOBAL_CALLS>{global_calls_max}/10s"
        elif int(g_tok or 0) > global_tokens_max:
            triggered = f"GLOBAL_TOKENS>{global_tokens_max}/10s"
        elif int(day_tok or 0) > daily_global_tokens_max:
            triggered = f"GLOBAL_TOKENS_DAY>{daily_global_tokens_max}/day"
        elif int(guild_calls or 0) > guild_calls_max:
            triggered = f"GUILD_CALLS({gid})>{guild_calls_max}/10s"
        elif int(guild_tok or 0) > guild_tokens_max:
            triggered = f"GUILD_TOKENS({gid})>{guild_tokens_max}/10s"
        elif int(user_calls or 0) > user_calls_max:
            triggered = f"USER_CALLS({uid})>{user_calls_max}/10s"
        elif int(user_tok or 0) > user_tokens_max:
            triggered = f"USER_TOKENS({uid})>{user_tokens_max}/10s"

        if triggered:
            await disable_ai(
                reason=(
                    f"Safeguard triggered: {triggered}. "
                    f"(window={WINDOW_S}s, guild={gid}, user={uid}, tokens={tok})"
                ),
                ttl_s=shutdown_ttl_s,
            )

    except Exception:
        return
