# utils/premium.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import logging
import time

from utils.redis_kv import hget_json, hset_json
from utils.db import get_sessionmaker
from utils.models import UserPremiumEntitlement, PremiumGift

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore

log = logging.getLogger("premium")


# ---------------------------------------------------------------------------
# Redis helpers (user-level keys)
# ---------------------------------------------------------------------------

def _user_settings_key(user_id: int) -> str:
    return f"user:{int(user_id)}:premium"


async def _get_user_setting(user_id: int, field: str, default=None):
    return await hget_json(_user_settings_key(user_id), field, default=default)


async def _set_user_setting(user_id: int, field: str, value) -> None:
    await hset_json(_user_settings_key(user_id), field, value)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _now_epoch_s() -> int:
    return int(time.time())


def _parse_int(v) -> int | None:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if not s:
            return None
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        return None
    except Exception:
        return None


def _trial_expiry_from_source(source: str | None) -> int | None:
    """Extract trial expiry epoch from source field. Format: 'trial:<epoch_seconds>'."""
    try:
        s = str(source or "").strip().lower()
        if not s.startswith("trial:"):
            return None
        return _parse_int(s.split(":", 1)[1])
    except Exception:
        return None


def _trial_used_from_source(source: str | None) -> bool:
    try:
        s = str(source or "").strip().lower()
        return s.startswith("trial_used:")
    except Exception:
        return False


def _fmt_utc_from_epoch(epoch_s: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(epoch_s)))
    except Exception:
        return "(unknown time)"


# ---------------------------------------------------------------------------
# Premium trial (now per-user)
# ---------------------------------------------------------------------------

async def grant_premium_trial(*, user_id: int, days: int = 5) -> tuple[bool, str]:
    """Grant a premium trial to an individual user.

    Safety goals:
    - Must not crash if DB is down.
    - Trial is once per user: recorded in DB source field and Redis.
    """
    uid = int(user_id)
    days_i = max(1, int(days))
    now = _now_epoch_s()
    expires = now + days_i * 86400

    # Redis guard
    try:
        redis_exp = _parse_int(await _get_user_setting(uid, "premium_trial_expires_at", None))
        if redis_exp and redis_exp > now:
            return False, f"Your premium trial is already active until `{_fmt_utc_from_epoch(redis_exp)}`."
        used = await _get_user_setting(uid, "premium_trial_used", False)
        if bool(used):
            return False, "You have already used your premium trial."
    except Exception:
        pass

    # DB path (durable)
    try:
        if select is None:
            raise RuntimeError("sqlalchemy not available")
        Session = get_sessionmaker()
        async with Session() as session:
            ent = await session.get(UserPremiumEntitlement, uid)
            if ent is not None:
                tier = str(getattr(ent, "tier", "free") or "free").strip().lower()
                src = str(getattr(ent, "source", "") or "")
                if _trial_used_from_source(src) or _trial_expiry_from_source(src) is not None:
                    exp = _trial_expiry_from_source(src)
                    if tier == "pro" and exp and exp > now:
                        return False, f"Your premium trial is already active until `{_fmt_utc_from_epoch(exp)}`."
                    return False, "You have already used your premium trial."
                if tier == "pro":
                    return False, "You already have premium enabled."
                ent.tier = "pro"
                ent.source = f"trial:{expires}"[:32]
            else:
                ent = UserPremiumEntitlement(user_id=uid, tier="pro", source=f"trial:{expires}"[:32])
                session.add(ent)
            await session.commit()

        try:
            await _set_user_setting(uid, "premium_trial_expires_at", int(expires))
            await _set_user_setting(uid, "premium_trial_used", True)
        except Exception:
            pass

        try:
            from utils.analytics import track_funnel_event, METRIC_TRIAL_START, METRIC_CONVERSION
            await track_funnel_event(guild_id=0, event=METRIC_TRIAL_START)
            await track_funnel_event(guild_id=0, event=METRIC_CONVERSION)
        except Exception:
            pass

        return True, f"\u2705 Premium enabled for you until `{_fmt_utc_from_epoch(expires)}`."
    except Exception:
        log.exception("grant_premium_trial DB path failed (user_id=%s)", uid)
        try:
            await _set_user_setting(uid, "premium_trial_expires_at", int(expires))
            await _set_user_setting(uid, "premium_trial_used", True)
            try:
                from utils.analytics import track_funnel_event, METRIC_TRIAL_START, METRIC_CONVERSION
                await track_funnel_event(guild_id=0, event=METRIC_TRIAL_START)
                await track_funnel_event(guild_id=0, event=METRIC_CONVERSION)
            except Exception:
                pass
            return True, f"\u2705 Premium enabled for you until `{_fmt_utc_from_epoch(expires)}`."
        except Exception:
            return False, "\u26a0\ufe0f Premium trial is unavailable right now (storage is down). Try again later."


# -------------------------
# Premium tier (user-level)
# -------------------------

async def get_premium_tier(user_id: int) -> str:
    """Return 'free' or 'pro' for a given user."""
    uid = int(user_id)

    # Trial override via Redis
    try:
        now = _now_epoch_s()
        exp = _parse_int(await _get_user_setting(uid, "premium_trial_expires_at", None))
        if exp and exp > now:
            return "pro"
        if exp and exp <= now:
            try:
                await _set_user_setting(uid, "premium_trial_expires_at", 0)
                await _set_user_setting(uid, "premium_trial_used", True)
            except Exception:
                pass
    except Exception:
        pass

    # Postgres source of truth
    try:
        if select is None:
            raise RuntimeError("sqlalchemy not available")
        Session = get_sessionmaker()
        async with Session() as session:
            res = await session.execute(
                select(
                    UserPremiumEntitlement.tier,
                    UserPremiumEntitlement.source,
                    UserPremiumEntitlement.subscription_period_end,
                ).where(UserPremiumEntitlement.user_id == uid)
            )
            row = res.one_or_none()
            if not row:
                return "free"

            tier = str(row[0] or "free").strip().lower()
            source = str(row[1] or "")
            period_end = row[2]

            # Enforce trial expiry
            if tier == "pro":
                exp = _trial_expiry_from_source(source)
                if exp is not None and exp <= _now_epoch_s():
                    try:
                        ent = await session.get(UserPremiumEntitlement, uid)
                        if ent is not None:
                            ent.tier = "free"
                            ent.source = f"trial_used:{exp}"[:32]
                            await session.commit()
                    except Exception:
                        pass
                    return "free"

            # Stripe grace period
            if tier == "free" and period_end is not None:
                try:
                    now_utc = datetime.now(timezone.utc)
                    if period_end.tzinfo is None:
                        period_end = period_end.replace(tzinfo=timezone.utc)
                    if period_end > now_utc:
                        return "pro"
                except Exception:
                    pass

            return tier if tier in {"free", "pro"} else "free"
    except Exception:
        # Redis fallback (legacy)
        try:
            tier = await _get_user_setting(uid, "premium_tier", "free")
            tier_s = str(tier or "free").strip().lower()
            return tier_s if tier_s in {"free", "pro"} else "free"
        except Exception:
            return "free"


# -------------------------
# Stripe premium helpers
# -------------------------

async def activate_stripe_premium(
    *,
    user_id: int,
    stripe_sub_id: str,
    stripe_cust_id: str,
    period_end: datetime,
) -> None:
    """Activate Pro for a user via a Stripe subscription."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    uid = int(user_id)
    now = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        ent = await session.get(UserPremiumEntitlement, uid)
        if ent is not None:
            ent.tier = "pro"
            ent.source = "stripe"
            ent.stripe_subscription_id = str(stripe_sub_id)
            ent.stripe_customer_id = str(stripe_cust_id)
            ent.subscription_period_end = period_end
            ent.updated_at = now
        else:
            ent = UserPremiumEntitlement(
                user_id=uid,
                tier="pro",
                source="stripe",
                stripe_subscription_id=str(stripe_sub_id),
                stripe_customer_id=str(stripe_cust_id),
                subscription_period_end=period_end,
                updated_at=now,
            )
            session.add(ent)
        await session.commit()

    try:
        from utils.analytics import track_funnel_event, METRIC_CONVERSION
        await track_funnel_event(guild_id=0, event=METRIC_CONVERSION)
    except Exception:
        pass

    log.info("Stripe Pro activated: user=%s sub=%s", uid, stripe_sub_id)


async def deactivate_stripe_premium(*, user_id: int) -> None:
    """Downgrade a user from Pro to free (subscription ended)."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    uid = int(user_id)
    now = datetime.now(timezone.utc)
    Session = get_sessionmaker()
    async with Session() as session:
        ent = await session.get(UserPremiumEntitlement, uid)
        if ent is not None:
            ent.tier = "free"
            ent.subscription_period_end = None
            ent.updated_at = now
            await session.commit()

    log.info("Stripe Pro deactivated: user=%s", uid)


async def activate_gift_premium(
    *,
    recipient_user_id: int,
    gifter_user_id: int,
    months: int,
    stripe_session_id: str | None = None,
) -> None:
    """Activate or extend Pro for a user via a gift."""
    if select is None:
        raise RuntimeError("sqlalchemy not available")

    uid = int(recipient_user_id)
    now = datetime.now(timezone.utc)
    gift_duration = timedelta(days=30 * int(months))

    Session = get_sessionmaker()
    async with Session() as session:
        ent = await session.get(UserPremiumEntitlement, uid)
        if ent is not None:
            # If already pro with a period end, extend from that date
            if ent.tier == "pro" and ent.subscription_period_end and ent.subscription_period_end > now:
                new_end = ent.subscription_period_end + gift_duration
            else:
                new_end = now + gift_duration
            ent.tier = "pro"
            ent.source = "gift"
            ent.subscription_period_end = new_end
            ent.gifted_by_user_id = int(gifter_user_id)
            ent.updated_at = now
        else:
            new_end = now + gift_duration
            ent = UserPremiumEntitlement(
                user_id=uid,
                tier="pro",
                source="gift",
                subscription_period_end=new_end,
                gifted_by_user_id=int(gifter_user_id),
                updated_at=now,
            )
            session.add(ent)
        await session.commit()

        # Record in audit trail
        gift = PremiumGift(
            gifter_user_id=int(gifter_user_id),
            recipient_user_id=uid,
            months=int(months),
            stripe_session_id=stripe_session_id,
        )
        session.add(gift)
        await session.commit()

    log.info(
        "Gift Pro activated: recipient=%s gifter=%s months=%s",
        uid, gifter_user_id, months,
    )


async def get_premium_details(user_id: int) -> dict:
    """Return detailed premium info for /premium status display."""
    info: dict = {
        "tier": "free",
        "source": "",
        "stripe_subscription_id": None,
        "stripe_customer_id": None,
        "subscription_period_end": None,
        "gifted_by_user_id": None,
    }
    try:
        if select is None:
            return info
        Session = get_sessionmaker()
        async with Session() as session:
            ent = await session.get(UserPremiumEntitlement, int(user_id))
            if ent is None:
                return info
            info["tier"] = str(getattr(ent, "tier", "free") or "free").strip().lower()
            info["source"] = str(getattr(ent, "source", "") or "")
            info["stripe_subscription_id"] = getattr(ent, "stripe_subscription_id", None)
            info["stripe_customer_id"] = getattr(ent, "stripe_customer_id", None)
            info["subscription_period_end"] = getattr(ent, "subscription_period_end", None)
            info["gifted_by_user_id"] = getattr(ent, "gifted_by_user_id", None)
    except Exception:
        pass

    # Grace-period logic: cancelled Stripe sub still active until period end
    if info["tier"] == "free" and info["subscription_period_end"] is not None:
        try:
            pe = info["subscription_period_end"]
            now_utc = datetime.now(timezone.utc)
            if pe.tzinfo is None:
                pe = pe.replace(tzinfo=timezone.utc)
            if pe > now_utc:
                info["tier"] = "pro"
        except Exception:
            pass

    # Gift expiry: source == "gift" and period_end has passed -> free
    if info["tier"] == "pro" and info["source"] == "gift" and info["subscription_period_end"] is not None:
        try:
            pe = info["subscription_period_end"]
            now_utc = datetime.now(timezone.utc)
            if pe.tzinfo is None:
                pe = pe.replace(tzinfo=timezone.utc)
            if pe <= now_utc:
                info["tier"] = "free"
        except Exception:
            pass

    return info


# -------------------------
# Rate-limit caps (AI + SAY)
# -------------------------
@dataclass(frozen=True)
class LimitCaps:
    # AI caps
    ai_user_max_min: int
    ai_user_max_max: int
    ai_user_window_min: int
    ai_user_window_max: int

    ai_guild_max_min: int
    ai_guild_max_max: int
    ai_guild_window_min: int
    ai_guild_window_max: int

    # SAY caps
    say_user_max_min: int
    say_user_max_max: int
    say_user_window_min: int
    say_user_window_max: int

    say_guild_max_min: int
    say_guild_max_max: int
    say_guild_window_min: int
    say_guild_window_max: int


FREE_CAPS = LimitCaps(
    ai_user_max_min=1, ai_user_max_max=3,
    ai_user_window_min=10, ai_user_window_max=600,

    ai_guild_max_min=1, ai_guild_max_max=10,
    ai_guild_window_min=30, ai_guild_window_max=3600,

    say_user_max_min=1, say_user_max_max=1,
    say_user_window_min=5, say_user_window_max=60,

    say_guild_max_min=1, say_guild_max_max=3,
    say_guild_window_min=10, say_guild_window_max=300,
)

PRO_CAPS = LimitCaps(
    ai_user_max_min=1, ai_user_max_max=50,
    ai_user_window_min=5, ai_user_window_max=86400,

    ai_guild_max_min=1, ai_guild_max_max=1000,
    ai_guild_window_min=5, ai_guild_window_max=86400,

    say_user_max_min=1, say_user_max_max=10,
    say_user_window_min=1, say_user_window_max=300,

    say_guild_max_min=1, say_guild_max_max=50,
    say_guild_window_min=1, say_guild_window_max=600,
)


async def get_caps(user_id: int) -> LimitCaps:
    return PRO_CAPS if (await get_premium_tier(user_id)) == "pro" else FREE_CAPS


def clamp_int(value: int, *, min_value: int, max_value: int) -> int:
    v = int(value)
    if v < min_value:
        return min_value
    if v > max_value:
        return max_value
    return v


# -------------------------
# Feedback caps
# -------------------------
@dataclass(frozen=True)
class FeedbackCaps:
    daily_max: int
    max_chars: int
    max_file_bytes: int


FREE_FEEDBACK_CAPS = FeedbackCaps(
    daily_max=3,
    max_chars=1000,
    max_file_bytes=1_000_000,  # ~1MB
)

PRO_FEEDBACK_CAPS = FeedbackCaps(
    daily_max=15,
    max_chars=3000,
    max_file_bytes=4_000_000,  # ~4MB
)


async def get_feedback_caps(user_id: int) -> FeedbackCaps:
    return PRO_FEEDBACK_CAPS if (await get_premium_tier(user_id)) == "pro" else FREE_FEEDBACK_CAPS


# -------------------------
# /talk daily caps (per-user + per-guild)
# -------------------------
@dataclass(frozen=True)
class AskCaps:
    daily_max: int          # per-user per day (UTC)
    guild_daily_max: int    # per-guild per day (UTC)
    weekly_max: int         # per-user per 7 days (rolling, UTC)
    guild_weekly_max: int   # per-guild per 7 days (rolling, UTC)


FREE_ASK_CAPS = AskCaps(
    daily_max=10,
    guild_daily_max=100,
    weekly_max=50,
    guild_weekly_max=500,
)

PRO_ASK_CAPS = AskCaps(
    daily_max=50,
    guild_daily_max=300,
    weekly_max=250,
    guild_weekly_max=2000,
)


async def get_talk_caps(user_id: int) -> AskCaps:
    return PRO_ASK_CAPS if (await get_premium_tier(user_id)) == "pro" else FREE_ASK_CAPS


# -------------------------
# Product boundaries (rolls, memory, queue priority)
# -------------------------


@dataclass(frozen=True)
class ProductCaps:
    roll_per_day: int
    memory_max_lines: int


FREE_PRODUCT_CAPS = ProductCaps(
    roll_per_day=1,
    memory_max_lines=4,
)

PRO_PRODUCT_CAPS = ProductCaps(
    roll_per_day=3,
    memory_max_lines=12,
)


async def get_product_caps(user_id: int) -> ProductCaps:
    """Single place to define tier boundaries used across the bot."""
    return PRO_PRODUCT_CAPS if (await get_premium_tier(user_id)) == "pro" else FREE_PRODUCT_CAPS
