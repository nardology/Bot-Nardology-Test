"""core/ai_gateway.py

Single entry-point for *any* AI call.

Phase 1 objective (Architecture & Code Health):
  - Centralize backpressure, concurrency gating, and exception-to-user-message mapping.
  - Keep the rest of the codebase from importing utils.ai_client.generate_text directly.

Phase 1 additions:
  - Emergency kill switch via env AI_DISABLED
  - Centralized daily + weekly usage budgets (talk/scene) via core.ai_usage
  - Gentle token clamping by mode+tier to avoid runaway responses
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config

from core.ai_usage import check_budget, record_success
from core.safeguard import check_and_record as safeguard_check

from utils.ai_client import (
    AIAuthError,
    AIConfigError,
    AIConnectionError,
    AIError,
    AIRateLimitError,
    AIStatusError,
    AITimeoutError,
    generate_text,
)
from utils.analytics import track_ai_call
from utils.backpressure import ai_slot, is_open, trip
from utils.ai_kill import is_disabled as ai_killed


@dataclass(frozen=True)
class AIGatewayResponse:
    ok: bool
    text: str = ""
    # A user-facing error message to show in Discord when ok=False
    user_message: str = ""
    # Suggested retry-after (seconds) if relevant
    retry_after_s: int = 0
    # Stable-ish label for metrics/logging
    error_type: str = ""


async def request_text(
    *,
    guild_id: int,
    user_id: int,
    tier: str,
    mode: str,
    system: str,
    user_prompt: str,
    max_output_tokens: int,
    timeout_s: Optional[float] = None,
    character_id: str = "",
    has_memory: bool = False,
) -> AIGatewayResponse:
    """Call the AI safely.

    This function:
      1) checks response cache (talk mode, short prompts, no memory)
      2) checks circuit breaker
      3) acquires Redis-backed concurrency slot (ai_slot)
      4) calls generate_text
      5) maps exceptions to a consistent, user-friendly message
      6) stores response in cache if eligible
    """

    # 0) Kill switch
    if getattr(config, "AI_DISABLED", False) or await ai_killed():
        # Include runtime reason when available (best-effort).
        reason = ""
        try:
            from utils.ai_kill import get_disable_meta
            _, r, _ = await get_disable_meta()
            reason = str(r or "").strip()
        except Exception:
            reason = ""
        user_message = "⛔ AI is temporarily disabled by the administrator."
        if reason:
            user_message += f"\nReason: `{reason[:300]}`"
        return AIGatewayResponse(ok=False, user_message=user_message, error_type="AI_DISABLED")

    # 0.5) Budgets (before we spend money)
    try:
        decision = await check_budget(mode=mode, guild_id=int(guild_id), user_id=int(user_id))
        if not decision.allowed:
            return AIGatewayResponse(ok=False, user_message=decision.message, error_type="BudgetExceeded")
    except Exception:
        # Budget checks must never crash a command.
        pass

    # 0.6) Revenue-linked cost cap (hard profitability guarantee)
    try:
        from utils.cost_tracker import is_within_budget
        allowed, current_cents, cap_cents = await is_within_budget(int(guild_id), str(tier or ""))
        if not allowed:
            return AIGatewayResponse(
                ok=False,
                user_message=(
                    f"⛔ This server has reached its daily AI budget. "
                    "Try again tomorrow (resets at midnight UTC)."
                ),
                error_type="CostCapExceeded",
            )
    except Exception:
        # Cost cap must never crash a command.
        pass

    # 0.7) Response cache (short talk prompts without memory)
    _cache_eligible = False
    try:
        from utils.response_cache import is_cacheable, get_cached
        _cache_eligible = is_cacheable(
            mode=mode, user_prompt=user_prompt, has_memory=has_memory,
        ) and bool(character_id)
        if _cache_eligible:
            cached_text = await get_cached(str(character_id), user_prompt)
            if cached_text:
                return AIGatewayResponse(ok=True, text=cached_text)
    except Exception:
        _cache_eligible = False

    # 1) Circuit breaker
    rem = await is_open()
    if rem > 0:
        return AIGatewayResponse(
            ok=False,
            user_message=f"⏳ The AI is busy right now. Try again in **{rem}s**.",
            retry_after_s=int(rem),
            error_type="BackpressureOpen",
        )

    # 2) Concurrency gating
    async with ai_slot(guild_id=int(guild_id), tier=str(tier or "")) as gate:
        if not gate.ok:
            return AIGatewayResponse(
                ok=False,
                user_message=f"⏳ Too many AI requests right now. Try again in **{gate.retry_after_s}s**.",
                retry_after_s=int(gate.retry_after_s or 0),
                error_type=f"AIConcurrency:{gate.mode}",
            )

        # 3) Token clamping (gentle)
        try:
            req = int(max_output_tokens)
        except Exception:
            req = 256

        mode_l = (mode or "").strip().lower()
        tier_l = (tier or "").strip().lower()

        # Safety backstop: hard ceiling even if caller requests more.
        # Actual requested values come from entitlements (200/350 talk, 500/1200 scene).
        # These backstops are slightly above entitlements to allow headroom.
        if mode_l == "scene":
            hard_max = 1200 if tier_l == "pro" else 550
        else:  # talk/default
            hard_max = 400 if tier_l == "pro" else 250

        max_tokens = max(64, min(req, int(hard_max)))

        # Model tiering: route free-tier to cheaper model to control costs.
        ai_model = config.OPENAI_MODEL if tier_l == "pro" else getattr(config, "OPENAI_MODEL_FREE", config.OPENAI_MODEL)

        # 3.5) Safeguard quick-check (spike detection). This is best-effort.
        try:
            # Use requested max token clamp as a conservative estimate for this call.
            await safeguard_check(guild_id=int(guild_id), user_id=int(user_id), total_tokens=int(max_tokens))
            if await ai_killed():
                return AIGatewayResponse(
                    ok=False,
                    user_message="⛔ AI was temporarily disabled for safety due to anomalous usage.",
                    error_type="AI_DISABLED_SAFEGUARD",
                )
        except Exception:
            pass

        # 4) AI call
        try:
            res = await generate_text(
                system=system,
                user=user_prompt,
                timeout_s=float(timeout_s if timeout_s is not None else getattr(config, "OPENAI_TIMEOUT_S", 20.0) or 20.0),
                max_output_tokens=int(max_tokens),
                return_raw=True,
                model=ai_model,
            )

            # robust: res may be str or object with .text
            if isinstance(res, str):
                text = res
                tokens = 0
                in_tokens = 0
                out_tokens = 0
            else:
                text = getattr(res, "text", "") or ""
                tokens = int(getattr(res, "total_tokens", 0) or 0)
                in_tokens = int(getattr(res, "input_tokens", 0) or 0)
                out_tokens = int(getattr(res, "output_tokens", 0) or 0)

            # Record usage AFTER success (call counts + actual token counts)
            try:
                await record_success(
                    mode=mode,
                    guild_id=int(guild_id),
                    user_id=int(user_id),
                    tokens=int(tokens),
                )
            except Exception:
                pass

            # Record estimated cost for revenue-linked cap
            try:
                from utils.cost_tracker import record_cost
                await record_cost(
                    guild_id=int(guild_id),
                    tier=str(tier or ""),
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                )
            except Exception:
                pass

            # Global/product analytics (real token usage when available)
            try:
                await track_ai_call(
                    guild_id=int(guild_id),
                    user_id=int(user_id),
                    mode=str(mode or "talk"),
                    tokens_used=int(tokens),
                )
            except Exception:
                pass

            # Store in response cache if eligible
            if _cache_eligible and text:
                try:
                    from utils.response_cache import store_cached
                    await store_cached(str(character_id), user_prompt, text)
                except Exception:
                    pass

            return AIGatewayResponse(ok=True, text=text)

        # 5) Exception mapping
        except AIConfigError as e:
            return AIGatewayResponse(
                ok=False,
                user_message=f"⚠️ Config error: {e}",
                error_type="AIConfigError",
            )
        except AIAuthError:
            return AIGatewayResponse(
                ok=False,
                user_message="⚠️ AI is misconfigured right now. Please tell an admin to check the API key.",
                error_type="AIAuthError",
            )
        except AITimeoutError:
            await trip(10)
            return AIGatewayResponse(
                ok=False,
                user_message="⏳ The AI is slow right now. Please try again in a minute.",
                retry_after_s=60,
                error_type="AITimeoutError",
            )
        except AIRateLimitError:
            await trip(20)
            return AIGatewayResponse(
                ok=False,
                user_message="⏳ The AI service is rate-limited right now. Please try again in a minute.",
                retry_after_s=60,
                error_type="AIRateLimitError",
            )
        except AIConnectionError:
            await trip(15)
            return AIGatewayResponse(
                ok=False,
                user_message="🌐 I’m having trouble reaching the AI service. Try again in a minute.",
                retry_after_s=60,
                error_type="AIConnectionError",
            )
        except AIStatusError as e:
            status = int(getattr(e, "status_code", 0) or 0)
            if 500 <= status < 600:
                await trip(15)
            return AIGatewayResponse(
                ok=False,
                user_message=f"⚠️ AI request failed (status {status}).",
                error_type=f"AIStatusError:{status}",
            )
        except AIError:
            return AIGatewayResponse(
                ok=False,
                user_message="⚠️ Something went wrong. Please try again in a minute.",
                retry_after_s=60,
                error_type="AIError",
            )
        except Exception as e:
            return AIGatewayResponse(
                ok=False,
                user_message="⚠️ Something went wrong. Please try again in a minute.",
                retry_after_s=60,
                error_type=type(e).__name__,
            )
