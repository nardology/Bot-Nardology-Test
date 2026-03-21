"""Phase 3: Dashboard query helpers for retention, economy, AI cost, churn."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from utils.analytics import utc_day_str
from utils.db import get_sessionmaker
from utils.metrics import estimate_ai_cost_usd_from_tokens

logger = logging.getLogger("bot.dashboard")

try:
    from sqlalchemy import select, func, and_  # type: ignore
except Exception:
    select = func = and_ = None  # type: ignore


@dataclass
class RetentionStats:
    cohort_day: str
    cohort_size: int
    d1_retained: int
    d7_retained: int
    d30_retained: int
    d1_pct: float
    d7_pct: float
    d30_pct: float


@dataclass
class EconomyStats:
    total_points_spent: int
    by_reason: dict[str, int]
    by_item: dict[str, int]
    spenders_count: int


@dataclass
class AICostStats:
    days: list[str]
    tokens_by_day: dict[str, int]
    total_tokens: int
    estimated_usd: float


@dataclass
class ChurnStats:
    guilds_declining: list[tuple[int, int, int]]  # (guild_id, last7, prev7)
    trials_ended_recently: int


def _add_days(day_utc: str, delta: int) -> str:
    """Add delta days to YYYYMMDD. Naive but sufficient for retention."""
    from datetime import datetime, timezone, timedelta

    try:
        dt = datetime.strptime(day_utc, "%Y%m%d").replace(tzinfo=timezone.utc)
        dt2 = dt + timedelta(days=delta)
        return dt2.strftime("%Y%m%d")
    except Exception:
        return ""


async def get_retention_stats(*, guild_id: int | None = None, cohort_days_back: int = 14) -> list[RetentionStats]:
    """D1/D7/D30 retention for cohorts. Requires UserActivityDay table."""
    if select is None or func is None:
        return []

    try:
        from utils.models import UserFirstSeen, UserActivityDay

        Session = get_sessionmaker()
        results: list[RetentionStats] = []
        now_ts = int(__import__("time").time())
        today = utc_day_str(now_ts)

        for i in range(cohort_days_back):
            cohort_day = utc_day_str(now_ts - 86400 * (i + 7))
            d1_day = _add_days(cohort_day, 1)
            d7_day = _add_days(cohort_day, 7)
            d30_day = _add_days(cohort_day, 30)
            if not d1_day or not d7_day or not d30_day:
                continue
            if d30_day > today:
                continue

            async with Session() as session:
                q = select(UserFirstSeen.user_id, UserFirstSeen.guild_id).where(
                    UserFirstSeen.first_day_utc == cohort_day
                )
                if guild_id is not None:
                    q = q.where(UserFirstSeen.guild_id == guild_id)
                rows = (await session.execute(q)).all()
                cohort = [(r[0], r[1]) for r in rows]
                cohort_size = len(cohort)

                if cohort_size == 0:
                    continue

                cohort_set = set((uid, gid) for uid, gid in cohort)
                d1_active: set[tuple[int, int]] = set()
                d7_active: set[tuple[int, int]] = set()
                d30_active: set[tuple[int, int]] = set()

                for day, s in [(d1_day, d1_active), (d7_day, d7_active), (d30_day, d30_active)]:
                    act_rows = (
                        await session.execute(
                            select(UserActivityDay.user_id, UserActivityDay.guild_id).where(
                                UserActivityDay.day_utc == day
                            )
                        )
                    )
                    for uid, gid in act_rows.all():
                        if (uid, gid) in cohort_set:
                            s.add((uid, gid))

                d1_retained = len(d1_active)
                d7_retained = len(d7_active)
                d30_retained = len(d30_active)

                d1_pct = (d1_retained / cohort_size * 100) if cohort_size else 0
                d7_pct = (d7_retained / cohort_size * 100) if cohort_size else 0
                d30_pct = (d30_retained / cohort_size * 100) if cohort_size else 0

                results.append(
                    RetentionStats(
                        cohort_day=cohort_day,
                        cohort_size=cohort_size,
                        d1_retained=d1_retained,
                        d7_retained=d7_retained,
                        d30_retained=d30_retained,
                        d1_pct=d1_pct,
                        d7_pct=d7_pct,
                        d30_pct=d30_pct,
                    )
                )

        return results[:7]
    except Exception as e:
        logger.exception("get_retention_stats failed: %s", e)
        return []


async def get_economy_stats(*, days: int = 7) -> EconomyStats:
    """Points spent by reason and shop item. Uses PointsLedger."""
    if select is None:
        return EconomyStats(0, {}, {}, 0)

    try:
        from utils.models import PointsLedger

        Session = get_sessionmaker()
        cutoff = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff - __import__("datetime").timedelta(days=days)

        async with Session() as session:
            rows = (
                await session.execute(
                    select(PointsLedger.delta, PointsLedger.reason, PointsLedger.meta_json).where(
                        and_(PointsLedger.delta < 0, PointsLedger.created_at >= cutoff)
                    )
                )
            ).all()

        total = 0
        by_reason: dict[str, int] = {}
        by_item: dict[str, int] = {}
        spenders: set[int] = set()

        for delta, reason, meta_json in rows:
            d = abs(int(delta or 0))
            total += d
            r = (reason or "unknown").strip() or "unknown"
            by_reason[r] = by_reason.get(r, 0) + d

            try:
                meta = json.loads(meta_json or "{}") if meta_json else {}
                item = meta.get("item") or meta.get("name")
                if item:
                    by_item[str(item)] = by_item.get(str(item), 0) + d
            except Exception:
                pass

        return EconomyStats(
            total_points_spent=total,
            by_reason=by_reason,
            by_item=by_item,
            spenders_count=0,
        )
    except Exception as e:
        logger.exception("get_economy_stats failed: %s", e)
        return EconomyStats(0, {}, {}, 0)


async def get_ai_cost_stats(*, days: int = 7, guild_id: int | None = None) -> AICostStats:
    """AI token usage and estimated cost from AnalyticsDailyMetric."""
    if select is None or func is None:
        return AICostStats([], {}, 0, 0.0)

    try:
        from utils.models import AnalyticsDailyMetric

        Session = get_sessionmaker()
        now_ts = int(__import__("time").time())
        day_list = [utc_day_str(now_ts - 86400 * i) for i in range(days)]

        async with Session() as session:
            q = (
                select(AnalyticsDailyMetric.day_utc, func.sum(AnalyticsDailyMetric.value))
                .where(AnalyticsDailyMetric.metric == "daily_ai_token_budget")
                .where(AnalyticsDailyMetric.day_utc.in_(day_list))
            )
            if guild_id is not None:
                q = q.where(AnalyticsDailyMetric.guild_id == guild_id)
            q = q.group_by(AnalyticsDailyMetric.day_utc)
            rows = (await session.execute(q)).all()

        tokens_by_day = {str(d): int(v or 0) for d, v in rows}
        total_tokens = sum(tokens_by_day.values())
        estimated_usd = estimate_ai_cost_usd_from_tokens(total_tokens)

        return AICostStats(
            days=day_list,
            tokens_by_day=tokens_by_day,
            total_tokens=total_tokens,
            estimated_usd=estimated_usd,
        )
    except Exception as e:
        logger.exception("get_ai_cost_stats failed: %s", e)
        return AICostStats([], {}, 0, 0.0)


@dataclass
class StickinessStats:
    dau: int
    wau: int
    mau: int
    stickiness_pct: float  # DAU / MAU * 100


@dataclass
class StreakBucket:
    label: str
    count: int


@dataclass
class InactiveUserStats:
    total_at_risk: int
    sample_user_ids: list[int]


async def get_stickiness_stats(*, guild_id: int | None = None) -> StickinessStats:
    """DAU / WAU / MAU from UserActivityDay. Stickiness = DAU/MAU."""
    if select is None or func is None:
        return StickinessStats(0, 0, 0, 0.0)
    try:
        from utils.models import UserActivityDay

        Session = get_sessionmaker()
        now_ts = int(__import__("time").time())
        today = utc_day_str(now_ts)
        last_7 = [utc_day_str(now_ts - 86400 * i) for i in range(7)]
        last_30 = [utc_day_str(now_ts - 86400 * i) for i in range(30)]

        async with Session() as session:
            def _q(day_list: list[str]):
                q = select(func.count(func.distinct(UserActivityDay.user_id))).where(
                    UserActivityDay.day_utc.in_(day_list)
                )
                if guild_id is not None:
                    q = q.where(UserActivityDay.guild_id == guild_id)
                return q

            dau = (await session.execute(_q([today]))).scalar() or 0
            wau = (await session.execute(_q(last_7))).scalar() or 0
            mau = (await session.execute(_q(last_30))).scalar() or 0

        stickiness = (dau / mau * 100) if mau > 0 else 0.0
        return StickinessStats(dau=int(dau), wau=int(wau), mau=int(mau), stickiness_pct=round(stickiness, 1))
    except Exception as e:
        logger.exception("get_stickiness_stats failed: %s", e)
        return StickinessStats(0, 0, 0, 0.0)


async def get_streak_distribution() -> list[StreakBucket]:
    """Group PointsWallet.streak into buckets."""
    if select is None or func is None:
        return []
    try:
        from utils.models import PointsWallet
        from sqlalchemy import case, literal_column  # type: ignore

        Session = get_sessionmaker()
        async with Session() as session:
            bucket_expr = case(
                (PointsWallet.streak == 0, literal_column("'0'")),
                (PointsWallet.streak <= 2, literal_column("'1-2'")),
                (PointsWallet.streak <= 6, literal_column("'3-6'")),
                (PointsWallet.streak <= 13, literal_column("'7-13'")),
                (PointsWallet.streak <= 29, literal_column("'14-29'")),
                (PointsWallet.streak <= 59, literal_column("'30-59'")),
                (PointsWallet.streak <= 89, literal_column("'60-89'")),
                else_=literal_column("'90+'"),
            ).label("bucket")

            rows = (
                await session.execute(
                    select(bucket_expr, func.count())
                    .group_by(bucket_expr)
                )
            ).all()

        order = ["0", "1-2", "3-6", "7-13", "14-29", "30-59", "60-89", "90+"]
        by_label = {str(label): int(cnt) for label, cnt in rows}
        return [StreakBucket(label=lab, count=by_label.get(lab, 0)) for lab in order]
    except Exception as e:
        logger.exception("get_streak_distribution failed: %s", e)
        return []


async def get_inactive_users(*, active_min_days: int = 3, inactive_days: int = 7, limit: int = 20) -> InactiveUserStats:
    """Users active 3+ days in last 30d but not seen in last 7d."""
    if select is None or func is None:
        return InactiveUserStats(0, [])
    try:
        from utils.models import UserActivityDay

        Session = get_sessionmaker()
        now_ts = int(__import__("time").time())
        last_30 = [utc_day_str(now_ts - 86400 * i) for i in range(30)]
        recent = [utc_day_str(now_ts - 86400 * i) for i in range(inactive_days)]

        async with Session() as session:
            active_sub = (
                select(UserActivityDay.user_id)
                .where(UserActivityDay.day_utc.in_(last_30))
                .group_by(UserActivityDay.user_id)
                .having(func.count(func.distinct(UserActivityDay.day_utc)) >= active_min_days)
            ).subquery()

            recent_sub = (
                select(func.distinct(UserActivityDay.user_id))
                .where(UserActivityDay.day_utc.in_(recent))
            ).subquery()

            q = (
                select(active_sub.c.user_id)
                .where(active_sub.c.user_id.notin_(select(recent_sub)))
            )
            rows = (await session.execute(q)).all()

        user_ids = [int(r[0]) for r in rows]
        return InactiveUserStats(total_at_risk=len(user_ids), sample_user_ids=user_ids[:limit])
    except Exception as e:
        logger.exception("get_inactive_users failed: %s", e)
        return InactiveUserStats(0, [])


async def _sum_metric_global_days(*, metric: str, day_list: list[str]) -> int:
    """Sum metric values across all guilds for the given UTC days."""
    if select is None or func is None:
        return 0
    if not day_list:
        return 0
    try:
        from utils.models import AnalyticsDailyMetric

        Session = get_sessionmaker()
        async with Session() as session:
            row = (
                await session.execute(
                    select(func.sum(AnalyticsDailyMetric.value)).where(
                        AnalyticsDailyMetric.metric == metric,
                        AnalyticsDailyMetric.day_utc.in_(day_list),
                    )
                )
            ).scalar()
            return int(row or 0)
    except Exception as e:
        logger.exception("_sum_metric_global_days failed: %s", e)
        return 0


def _day_list_last_n_days(*, now_ts: int, n: int) -> list[str]:
    """UTC YYYYMMDD for today and the previous n-1 days (n days total)."""
    return [utc_day_str(now_ts - 86400 * i) for i in range(max(1, n))]


async def get_spending_dashboard(*, series_days: int = 90) -> dict[str, Any]:
    """AI spend (tokens → USD) by period + simple projections. Uses analytics_daily_metrics.

    Covers token-budget totals (same basis as existing AI cost estimates). Does not include
    Stripe fees or other vendors unless you extend metrics.
    """
    import os

    from utils.analytics import METRIC_DAILY_AI_CALLS, METRIC_DAILY_AI_TOKEN_BUDGET
    from datetime import datetime, timezone
    import calendar
    import time as time_mod

    now_ts = int(time_mod.time())
    today = utc_day_str(now_ts)

    tok_today = await _sum_metric_global_days(metric=METRIC_DAILY_AI_TOKEN_BUDGET, day_list=[today])
    calls_today = await _sum_metric_global_days(metric=METRIC_DAILY_AI_CALLS, day_list=[today])

    days_7 = _day_list_last_n_days(now_ts=now_ts, n=7)
    days_30 = _day_list_last_n_days(now_ts=now_ts, n=30)
    days_365 = _day_list_last_n_days(now_ts=now_ts, n=365)

    tok_7 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_TOKEN_BUDGET, day_list=days_7)
    tok_30 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_TOKEN_BUDGET, day_list=days_30)
    tok_365 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_TOKEN_BUDGET, day_list=days_365)

    calls_7 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_CALLS, day_list=days_7)
    calls_30 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_CALLS, day_list=days_30)
    calls_365 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_CALLS, day_list=days_365)

    usd_today = estimate_ai_cost_usd_from_tokens(tok_today)
    usd_7 = estimate_ai_cost_usd_from_tokens(tok_7)
    usd_30 = estimate_ai_cost_usd_from_tokens(tok_30)
    usd_365 = estimate_ai_cost_usd_from_tokens(tok_365)

    avg_daily_7 = usd_7 / 7.0
    avg_daily_30 = usd_30 / 30.0

    # Previous week (days 8–14) for simple trend
    days_prev_7 = [utc_day_str(now_ts - 86400 * i) for i in range(7, 14)]
    tok_prev_7 = await _sum_metric_global_days(metric=METRIC_DAILY_AI_TOKEN_BUDGET, day_list=days_prev_7)
    usd_prev_7 = estimate_ai_cost_usd_from_tokens(tok_prev_7)
    avg_daily_prev_7 = usd_prev_7 / 7.0
    week_over_week_pct: float | None = None
    if avg_daily_prev_7 > 0:
        week_over_week_pct = round((avg_daily_7 - avg_daily_prev_7) / avg_daily_prev_7 * 100.0, 1)

    # Projections (linear from recent averages)
    proj = {
        "from_7d_avg_usd_per_day": round(avg_daily_7, 6),
        "next_7d_usd": round(avg_daily_7 * 7, 4),
        "next_30d_usd": round(avg_daily_7 * 30, 4),
        "next_365d_usd": round(avg_daily_7 * 365, 4),
        "from_30d_avg_usd_per_day": round(avg_daily_30, 6),
        "next_365d_usd_from_30d_avg": round(avg_daily_30 * 365, 4),
    }

    # Rest of calendar month (UTC), starting tomorrow
    now_dt = datetime.now(timezone.utc)
    last_d = calendar.monthrange(now_dt.year, now_dt.month)[1]
    days_after_today = max(0, last_d - now_dt.day)
    proj["rest_of_month_usd_from_7d_avg"] = round(avg_daily_7 * days_after_today, 4)
    proj["days_remaining_in_month_utc"] = days_after_today

    # Daily series for chart (token + usd per day)
    n_series = max(7, min(int(series_days), 365))
    series_days_list = _day_list_last_n_days(now_ts=now_ts, n=n_series)
    series: list[dict[str, Any]] = []
    try:
        from utils.models import AnalyticsDailyMetric

        if select is not None:
            Session = get_sessionmaker()
            async with Session() as session:
                rows = (
                    await session.execute(
                        select(AnalyticsDailyMetric.day_utc, func.sum(AnalyticsDailyMetric.value))
                        .where(AnalyticsDailyMetric.metric == METRIC_DAILY_AI_TOKEN_BUDGET)
                        .where(AnalyticsDailyMetric.day_utc.in_(series_days_list))
                        .group_by(AnalyticsDailyMetric.day_utc)
                    )
                ).all()
                by_day = {str(d): int(v or 0) for d, v in rows}
                for i in range(n_series - 1, -1, -1):
                    d = utc_day_str(now_ts - 86400 * i)
                    t = int(by_day.get(d, 0))
                    series.append(
                        {
                            "day_utc": d,
                            "tokens_budget": t,
                            "estimated_usd": round(estimate_ai_cost_usd_from_tokens(t), 6),
                        }
                    )
    except Exception as e:
        logger.exception("spending series failed: %s", e)

    try:
        price_1k = float(os.getenv("AI_COST_PER_1K_TOKENS", "0.002"))
    except Exception:
        price_1k = 0.002

    return {
        "currency": "USD",
        "basis": "OpenAI-style token budget totals (METRIC_DAILY_AI_TOKEN_BUDGET); est. using AI_COST_PER_1K_TOKENS.",
        "price_per_1k_tokens": price_1k,
        "periods": {
            "daily": {
                "day_utc": today,
                "tokens_budget": tok_today,
                "ai_calls": calls_today,
                "estimated_usd": round(usd_today, 6),
            },
            "weekly": {
                "days": 7,
                "tokens_budget": tok_7,
                "ai_calls": calls_7,
                "estimated_usd": round(usd_7, 4),
                "avg_daily_usd": round(avg_daily_7, 6),
            },
            "monthly": {
                "days": 30,
                "tokens_budget": tok_30,
                "ai_calls": calls_30,
                "estimated_usd": round(usd_30, 4),
                "avg_daily_usd": round(avg_daily_30, 6),
            },
            "yearly": {
                "days": 365,
                "tokens_budget": tok_365,
                "ai_calls": calls_365,
                "estimated_usd": round(usd_365, 4),
            },
        },
        "trend": {
            "avg_daily_usd_prior_7d": round(avg_daily_prev_7, 6),
            "week_over_week_avg_daily_pct": week_over_week_pct,
        },
        "projections": proj,
        "disclaimer": (
            "Forecasts assume usage stays near recent averages. Stripe/hosting/other costs are not included "
            "unless you add metrics for them."
        ),
        "series_last_days": series,
    }


async def get_churn_stats(*, guild_ids: list[int] | None = None) -> ChurnStats:
    """Guilds with declining activity, trials ended."""
    if select is None or func is None:
        return ChurnStats([], 0)

    try:
        from utils.models import AnalyticsDailyMetric, PremiumEntitlement

        Session = get_sessionmaker()
        now_ts = int(__import__("time").time())
        last7 = [utc_day_str(now_ts - 86400 * i) for i in range(7)]
        prev7 = [utc_day_str(now_ts - 86400 * (i + 7)) for i in range(7)]

        guilds_declining: list[tuple[int, int, int]] = []

        async with Session() as session:
            all_guilds = guild_ids
            if not all_guilds:
                r = await session.execute(
                    select(AnalyticsDailyMetric.guild_id)
                    .where(AnalyticsDailyMetric.metric == "daily_ai_calls")
                    .distinct()
                )
                all_guilds = list({row[0] for row in r.all()})

            for gid in (all_guilds or [])[:100]:
                last7_sum = (
                    await session.execute(
                        select(func.sum(AnalyticsDailyMetric.value))
                        .where(AnalyticsDailyMetric.guild_id == gid)
                        .where(AnalyticsDailyMetric.metric == "daily_ai_calls")
                        .where(AnalyticsDailyMetric.day_utc.in_(last7))
                    )
                ).scalar() or 0
                prev7_sum = (
                    await session.execute(
                        select(func.sum(AnalyticsDailyMetric.value))
                        .where(AnalyticsDailyMetric.guild_id == gid)
                        .where(AnalyticsDailyMetric.metric == "daily_ai_calls")
                        .where(AnalyticsDailyMetric.day_utc.in_(prev7))
                    )
                ).scalar() or 0
                if prev7_sum > 0 and int(last7_sum) < int(prev7_sum) * 0.5:
                    guilds_declining.append((gid, int(last7_sum), int(prev7_sum)))

            guilds_declining.sort(key=lambda x: x[2] - x[1], reverse=True)

            trials_ended = 0
            ents = (await session.execute(select(PremiumEntitlement).where(PremiumEntitlement.tier == "free"))).scalars().all()
            for ent in ents:
                src = str(getattr(ent, "source", "") or "")
                if "trial_used" in src.lower() or "trial:" in src.lower():
                    trials_ended += 1

        return ChurnStats(guilds_declining=guilds_declining[:10], trials_ended_recently=trials_ended)
    except Exception as e:
        logger.exception("get_churn_stats failed: %s", e)
        return ChurnStats([], 0)
