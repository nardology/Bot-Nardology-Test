"""Background tasks: weekly status reminders + random check-in DMs (connection traits)."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import discord

from utils.backpressure import get_redis_or_none
from utils.connection_traits_store import list_profiles_with_trait, load_profile

log = logging.getLogger("connection_traits_loop")


def _iso_week_id() -> str:
    dt = datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}{int(w):02d}"


async def _weekly_reminders(bot) -> None:
    now = datetime.now(timezone.utc)
    # Sunday 12:00–12:59 UTC
    if now.weekday() != 6 or now.hour != 12:
        return
    wk = _iso_week_id()
    a = await list_profiles_with_trait("weekly_life")
    b = await list_profiles_with_trait("daily_status")
    pairs: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for uid, sid in a + b:
        t = (int(uid), str(sid).lower())
        if t in seen:
            continue
        seen.add(t)
        pairs.append(t)

    base = ""
    try:
        import config

        base = (config.BASE_URL or "").strip().rstrip("/")
    except Exception:
        pass

    for uid, sid in pairs:
        try:
            data = await load_profile(user_id=uid, style_id=sid)
            pl = data.get("payload") or {}
            if str(pl.get("week_id") or "") == wk and (pl.get("weekly_status") or "").strip():
                continue
            u = bot.get_user(uid) or await bot.fetch_user(uid)
            link = f"{base}/connection" if base else "your connection traits dashboard"
            await u.send(
                f"It's **Sunday** — time for your weekly life update for character `{sid}`.\n"
                f"Open: {link}"
            )
        except Exception:
            log.debug("weekly reminder failed uid=%s", uid, exc_info=True)


async def _random_checkins(bot) -> None:
    pairs = await list_profiles_with_trait("random_dm")
    if not pairs:
        return
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y%m%d")
    r = await get_redis_or_none()

    for uid, sid in pairs:
        if random.random() > 0.12:
            continue
        if r:
            try:
                k = f"conn:rand:{int(uid)}:{day}"
                n = int(await r.get(k) or 0)
                if n >= 3:
                    continue
                await r.incr(k)
                await r.expire(k, 86400 * 3)
            except Exception:
                pass
        try:
            import config

            base = (config.BASE_URL or "https://discord.com").strip().rstrip("/")
            u = bot.get_user(uid) or await bot.fetch_user(uid)
            v = discord.ui.View(timeout=3600)
            v.add_item(
                discord.ui.Button(
                    label="Open dashboard",
                    style=discord.ButtonStyle.link,
                    url=f"{base}/connection",
                )
            )
            await u.send(
                f"Quick check-in about `{sid}`: how are you doing today? "
                f"(Reply here or continue in your server — connection traits.)",
                view=v,
            )
        except Exception:
            log.debug("random check-in failed uid=%s", uid, exc_info=True)


async def connection_traits_loop_tick(bot) -> None:
    await _weekly_reminders(bot)
    await _random_checkins(bot)


def start_connection_traits_loop(bot) -> asyncio.Task:
    async def runner():
        await bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(900)
                await connection_traits_loop_tick(bot)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("connection_traits_loop tick")

    return asyncio.create_task(runner())
