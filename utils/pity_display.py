"""Human-readable pity / luck progress for gacha rolls (legendary & mythic tracks)."""
from __future__ import annotations

# Hard pity floors (must match choose_rarity in character_registry).
LEGENDARY_GUARANTEE_AT = 99
MYTHIC_GUARANTEE_AT = 999


def pct_toward_guarantee(current: int, cap: int) -> float:
    """Percent progress 0..100 toward hard pity."""
    c = max(0, int(current or 0))
    cap = max(1, int(cap or 1))
    return min(100.0, 100.0 * c / float(cap))


def legendary_phase(pity_legendary: int) -> str:
    p = max(0, int(pity_legendary or 0))
    if p >= LEGENDARY_GUARANTEE_AT:
        return "guaranteed next legendary roll"
    if p >= 70:
        return "very high"
    if p >= 40:
        return "building"
    if p >= 15:
        return "warming up"
    return "early"


def mythic_phase(pity_mythic: int) -> str:
    p = max(0, int(pity_mythic or 0))
    if p >= MYTHIC_GUARANTEE_AT:
        return "guaranteed next mythic roll"
    if p >= 600:
        return "very high"
    if p >= 300:
        return "building"
    if p >= 80:
        return "warming up"
    return "early"


def format_luck_progress_embed_value(pity_legendary: int, pity_mythic: int) -> str:
    """Two-line summary for embed fields (no raw 'pity' jargon as a luck score)."""
    pl = max(0, int(pity_legendary or 0))
    pm = max(0, int(pity_mythic or 0))
    leg_pct = pct_toward_guarantee(pl, LEGENDARY_GUARANTEE_AT)
    myt_pct = pct_toward_guarantee(pm, MYTHIC_GUARANTEE_AT)
    return (
        f"**Legendary track:** {legendary_phase(pl)} — ~{leg_pct:.0f}% toward safety net "
        f"({pl}/{LEGENDARY_GUARANTEE_AT})\n"
        f"**Mythic track:** {mythic_phase(pm)} — ~{myt_pct:.0f}% toward safety net "
        f"({pm}/{MYTHIC_GUARANTEE_AT})\n"
        "_Each miss on rare-or-below raises both tracks. Hitting legendary resets the legendary track._"
    )


def kai_pity_message_line(pity_legendary: int, pity_mythic: int) -> str:
    """Short line for KAI mascot (no confusing '35 pity' alone)."""
    pl = max(0, int(pity_legendary or 0))
    pm = max(0, int(pity_mythic or 0))
    leg_pct = pct_toward_guarantee(pl, LEGENDARY_GUARANTEE_AT)
    myt_pct = pct_toward_guarantee(pm, MYTHIC_GUARANTEE_AT)
    return (
        f"Your **legendary** safety net is about **{leg_pct:.0f}%** full "
        f"and your **mythic** safety net about **{myt_pct:.0f}%** full — "
        "keep rolling; the odds quietly improve over time."
    )
