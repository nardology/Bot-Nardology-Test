"""Pack creator daily bonus: points when your pack is enabled in servers (capped, no server-pack bonus).

Phase 5: Optional creator revenue share stub for when Stripe/Pro revenue is ready.
"""
from __future__ import annotations

import logging
from typing import Any

from utils.packs_store import get_custom_pack, get_enabled_pack_ids, list_custom_packs

log = logging.getLogger("pack_creator_rewards")


async def record_creator_revenue_share(
    pack_id: str,
    creator_user_id: int,
    amount_cents: int,
    *,
    source: str = "stripe",
) -> None:
    """Phase 5: Optional stub for creator revenue share (Pro/subscription payouts).

    When Stripe integration is ready, call this when distributing a share of
    Pro revenue or points spent in a pack to the pack creator.
    No-op for now; can later write to DB or emit analytics.
    """
    # Optional: track for analytics / future payout
    log.debug(
        "creator_revenue_share stub: pack=%s creator=%s amount_cents=%s source=%s",
        pack_id, creator_user_id, amount_cents, source,
    )

# Economy: shop has extra_roll 60, lucky_booster 120, inv_upgrade 500+.
# Cap daily pack-creator bonus so it doesn't dwarf shop (e.g. ~8 extra rolls max).
POINTS_PER_MEMBER = 0.5
CAP_PER_SERVER_MEMBERS = 5000
CAP_DAILY_BONUS = 500

# Pack IDs that never grant creator bonus (server packs, built-in).
SERVER_PACK_PREFIX = "server_"
BUILTIN_PACK_ID = "nardologybot"


def _pack_id_grants_creator_bonus(pack_id: str) -> bool:
    """Server packs and built-in pack do not grant any creator bonus."""
    pid = (pack_id or "").strip().lower()
    if pid.startswith(SERVER_PACK_PREFIX):
        return False
    if pid == BUILTIN_PACK_ID:
        return False
    return True


async def _pack_ids_created_by_user(user_id: int) -> set[str]:
    """Return set of custom pack IDs where created_by_user == user_id (excludes server/builtin)."""
    uid = int(user_id)
    out: set[str] = set()
    try:
        packs = await list_custom_packs(include_internal=False)
        for p in packs:
            if not isinstance(p, dict):
                continue
            creator = int(p.get("created_by_user") or 0)
            if creator != uid:
                continue
            pid = str(p.get("pack_id") or "").strip().lower()
            if not pid or not _pack_id_grants_creator_bonus(pid):
                continue
            out.add(pid)
    except Exception:
        log.exception("list packs for creator failed")
    return out


async def get_pack_creator_daily_bonus(
    bot: Any,
    user_id: int,
) -> tuple[int, list[tuple[str, str, int, int]]]:
    """Compute daily pack-creator bonus for a user.

    For each guild the bot is in, for each enabled pack that this user created
    (excluding server_* and nardologybot), add floor(member_count * POINTS_PER_MEMBER)
    with member_count capped at CAP_PER_SERVER_MEMBERS. Total bonus is capped at CAP_DAILY_BONUS.

    Returns (total_bonus, breakdown) where breakdown is list of (guild_name, pack_id, members, points).
    """
    uid = int(user_id)
    my_packs = await _pack_ids_created_by_user(uid)
    if not my_packs:
        return 0, []

    total = 0
    breakdown: list[tuple[str, str, int, int]] = []

    try:
        for guild in bot.guilds:
            member_count = getattr(guild, "member_count", None) or 0
            member_count = min(max(0, int(member_count)), CAP_PER_SERVER_MEMBERS)
            if member_count <= 0:
                continue
            enabled = await get_enabled_pack_ids(guild.id)
            guild_name = getattr(guild, "name", "") or str(guild.id)
            for pack_id in enabled:
                if pack_id not in my_packs:
                    continue
                points = int(member_count * POINTS_PER_MEMBER)
                if points <= 0:
                    continue
                total += points
                breakdown.append((guild_name, pack_id, member_count, points))
    except Exception:
        log.exception("pack creator bonus iteration failed")

    total = min(total, CAP_DAILY_BONUS)
    return total, breakdown
