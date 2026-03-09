# utils/bonds_prompt.py
from __future__ import annotations

from dataclasses import dataclass

from utils.bonds import level_from_xp, title_for_level


@dataclass
class BondPromptContext:
    level: int
    title: str
    nickname: str | None


def build_bond_prompt_context(*, xp: int, nickname: str | None) -> BondPromptContext:
    lvl = level_from_xp(int(xp or 0))
    title = title_for_level(lvl)
    nick = (nickname or "").strip() or None
    return BondPromptContext(level=lvl, title=title, nickname=nick)


def secrets_for_bond_level(secrets: list[str] | None, level: int) -> list[str]:
    """Return the subset of secrets unlocked at the given bond level.

    Progression:
        Level < 5  → no secrets
        Level 5-6  → first secret only
        Level 7-9  → first two secrets (or all if fewer than 3)
        Level 10+  → all secrets
    """
    if not secrets or level < 5:
        return []
    if level < 7:
        return secrets[:1]
    if level < 10:
        return secrets[:2]
    return list(secrets)


def bond_system_lines(
    ctx: BondPromptContext,
    *,
    secrets: list[str] | None = None,
) -> str:
    """Returns a compact system-instruction block for bond context."""
    lines: list[str] = []
    lines.append(f"BOND_LEVEL: {ctx.level} ({ctx.title})")

    # warmth tuning
    if ctx.level >= 10:
        lines.append("Tone: warm, familiar, trusting. Show subtle affection and shared-history vibes.")
    elif ctx.level >= 5:
        lines.append("Tone: friendly, comfortable. Use gentle teasing or supportive energy when fitting.")
    elif ctx.level >= 3:
        lines.append("Tone: friendly and open. Slightly more personal than default.")
    else:
        lines.append("Tone: polite and curious. Relationship is still new.")

    # nickname rule
    if ctx.nickname:
        lines.append(
            f"UserNicknameForYou: {ctx.nickname}. Use it sparingly (about 1 in 8 messages), "
            "especially greetings, emotional moments, or emphasis. Do not overuse."
            "Do not use the nickname more than once per message."
        )

    # micro-unlocks
    if ctx.level >= 3:
        lines.append("Unlock: occasional short greeting or callback line before responding (not always).")
    if ctx.level >= 5:
        lines.append("Unlock: you may reference 1 shared preference from the current conversation when fitting.")
        lines.append("Unlock: you can playfully disagree with the user, tease them, or express mild frustration when it fits your character.")
    if ctx.level >= 10:
        lines.append("Unlock: you may show light protectiveness/loyalty in character (still respect user autonomy).")
        lines.append("Unlock: reference inside jokes or shared tone from the conversation naturally. Show that this relationship has history.")

    # bond-gated secrets
    revealed = secrets_for_bond_level(secrets, ctx.level)
    if revealed:
        lines.append("")
        lines.append("Hidden truths you can now reveal (weave these in naturally when relevant, don't dump them all at once):")
        for s in revealed:
            lines.append(f"- {s}")

    return "\n".join(lines)
