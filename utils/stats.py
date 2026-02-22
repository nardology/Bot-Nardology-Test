"""Stats aggregation for /inspect: points, streak, characters, bonds."""
from __future__ import annotations

from dataclasses import dataclass, field

from utils.points_store import GLOBAL_GUILD_ID, get_balance, get_claim_status
from utils.character_store import load_state
from utils.character_registry import BASE_STYLE_IDS, get_style
from utils.bonds_store import list_bonds_for_user
from utils.bonds import level_from_xp, title_for_level


@dataclass
class BondSummary:
    style_id: str
    character_name: str
    xp: int
    level: int
    title: str


@dataclass
class UserStats:
    user_id: int
    points: int = 0
    daily_streak: int = 0
    characters_owned_count: int = 0
    character_ids: list[str] = field(default_factory=list)
    selected_character_id: str = ""
    selected_character_name: str = ""
    bonds: list[BondSummary] = field(default_factory=list)
    highest_bond: BondSummary | None = None
    total_bond_xp: int = 0


def _format_character_name(style_id: str) -> str:
    s = get_style((style_id or "").strip().lower())
    if s and getattr(s, "display_name", None):
        return str(s.display_name)
    return (style_id or "unknown").replace("_", " ").title()


async def get_user_stats(user_id: int, guild_id: int = 0) -> UserStats:
    """Aggregate stats for a user: points, streak, characters, bonds.
    guild_id is only used for API compatibility; points/streak/bonds are global.
    """
    uid = int(user_id)
    stats = UserStats(user_id=uid)

    # Points and daily streak (global)
    try:
        stats.points = await get_balance(guild_id=GLOBAL_GUILD_ID, user_id=uid)
        _claimed, _next_s, stats.daily_streak = await get_claim_status(
            guild_id=GLOBAL_GUILD_ID, user_id=uid
        )
    except Exception:
        pass

    # Characters owned and selected (from character_store)
    try:
        state = await load_state(user_id=uid)
        owned_list = list(getattr(state, "owned_custom", []) or [])
        base_set = {s.lower() for s in (BASE_STYLE_IDS or [])}
        stats.character_ids = [s for s in owned_list if s and str(s).lower() not in base_set]
        stats.character_ids = list(dict.fromkeys(s.lower() for s in stats.character_ids if s))
        stats.characters_owned_count = len(stats.character_ids)
        active = getattr(state, "active_style_id", None) or ""
        stats.selected_character_id = (active or "").strip().lower()
        if stats.selected_character_id:
            stats.selected_character_name = _format_character_name(stats.selected_character_id)
    except Exception:
        pass

    # Bonds: list all, compute levels, find highest
    try:
        bond_list = await list_bonds_for_user(uid)
        for b in bond_list:
            xp = int(b.xp or 0)
            level = level_from_xp(xp)
            title = title_for_level(level)
            name = _format_character_name(b.style_id or "")
            summary = BondSummary(
                style_id=b.style_id or "",
                character_name=name,
                xp=xp,
                level=level,
                title=title,
            )
            stats.bonds.append(summary)
            stats.total_bond_xp += xp

        if stats.bonds:
            stats.bonds.sort(key=lambda x: x.xp, reverse=True)
            stats.highest_bond = stats.bonds[0]
    except Exception:
        pass

    return stats
