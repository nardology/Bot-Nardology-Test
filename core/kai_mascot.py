# core/kai_mascot.py
"""KAI mascot: happy, energetic robot cat that helps and motivates users.

Images live in BotNardology-Assets repo under assets/ui.
"""
from __future__ import annotations

import random

import discord

from utils.character_emotion_manifest import ROLL_ANIMATION_UI_BASE

# Pity threshold: only show kaihappy encouragement when user has built up pity (feels "close").
PITY_LEGENDARY_THRESHOLD = 5
PITY_MYTHIC_THRESHOLD = 10

# Image filenames (case-sensitive on GitHub)
KAI_START_IMAGE = "start.png"
KAI_LOVE_IMAGE = "Kailove.jpg"
KAI_HAPPY_IMAGE = "kaihappy.jpg"


def _url(filename: str) -> str:
    """Use jsDelivr base so KAI images always load (e.g. during roll pity/celebration)."""
    base = (ROLL_ANIMATION_UI_BASE or "").rstrip("/")
    return f"{base}/{filename}" if base else ""


def embed_kai_start(description: str, *, title: str = "KAI says hi!") -> discord.Embed:
    """Embed with start.png for greetings (e.g. /start, opening quests)."""
    e = discord.Embed(title=title, description=description, color=0x3498DB)
    e.set_image(url=_url(KAI_START_IMAGE))
    e.set_footer(text="KAI · your friendly robot cat")
    return e


def embed_kailove(description: str, *, title: str = "KAI loves it!") -> discord.Embed:
    """Embed with Kailove.jpg for bond milestones and daily claim."""
    e = discord.Embed(title=title, description=description, color=0xE91E63)
    e.set_image(url=_url(KAI_LOVE_IMAGE))
    e.set_footer(text="KAI · your friendly robot cat")
    return e


def embed_kaihappy(description: str, *, title: str = "KAI is here for you!") -> discord.Embed:
    """Embed with kaihappy.jpg for pity rolls and quest claims."""
    e = discord.Embed(title=title, description=description, color=0xF1C40F)
    e.set_image(url=_url(KAI_HAPPY_IMAGE))
    e.set_footer(text="KAI · your friendly robot cat")
    return e


def embed_kaisad(description: str, *, title: str = "KAI is sad...") -> discord.Embed:
    """Embed for sad/loss events like broken streaks. Uses kaihappy image (no sad asset yet) with muted color."""
    e = discord.Embed(title=title, description=description, color=0xED4245)
    e.set_image(url=_url(KAI_HAPPY_IMAGE))
    e.set_footer(text="KAI · your friendly robot cat")
    return e


# ---------- Motivational messages (streak / context-aware) ----------


def get_kai_start_greeting() -> str:
    """Formal greeting for /start — energetic, welcoming."""
    return (
        "Hi! I'm **KAI**, your friendly robot cat! 🤖🐱\n\n"
        "I'm here to help you get the most out of Bot‑Nardology. "
        "Roll characters, chat with them, build bonds, and complete quests — "
        "I'll be cheering you on every step of the way. Let's go!"
    )


def get_kai_quests_open_greeting() -> str:
    """Greeting when user opens /points quests."""
    return (
        "Here are your quests! Complete them to earn points. "
        "I believe in you — every little step counts! 💪"
    )


def get_kai_first_talk_message(character_name: str) -> str:
    """When user talks with a character for the first time."""
    return (
        f"You just talked with **{character_name}** for the first time! "
        "This may be the start of a new friendship!"
    )


def get_kai_bond_level_message(bond_title: str, character_name: str) -> str:
    """When user levels up bond with a character."""
    return (
        f"Wow! You just became **{bond_title}** with **{character_name}**! "
        "You must really like each other — keep it up!"
    )


def get_kai_daily_claim_message(awarded: int, streak: int, first_bonus: int) -> str:
    """Message when user claims daily reward. Streak-aware."""
    parts = [f"You claimed **{awarded}** points!"]
    if streak > 0:
        parts.append(f" Your streak: **{streak}** day(s).")
    if first_bonus > 0:
        parts.append(f" Plus **{first_bonus}** bonus for your first time — amazing!")
    parts.append(" KAI is so proud of you. Keep coming back!")
    return "".join(parts)


def get_kai_daily_claim_message_varied(streak: int) -> str:
    """Extra motivational line based on streak (for embed title or second line)."""
    if streak >= 30:
        return "You're a legend! 30+ days in a row!"
    if streak >= 14:
        return "Two weeks strong — you're unstoppable!"
    if streak >= 7:
        return "A full week! KAI is doing a happy dance!"
    if streak >= 3:
        return "Great consistency! Keep it up!"
    if streak >= 1:
        return "Day one (or another day) — every journey starts here!"
    return "KAI loves that you're here!"


def get_kai_pity_roll_message(pity_legendary: int = 0, pity_mythic: int = 0) -> str:
    """When user gets a pity point (rolled but not legendary/mythic). Variants; can mention pity when high."""
    variants = [
        "It's been a while since you rolled something rare — "
        "maybe the next one will be an amazing roll! I'm rooting for you!",
        "Your next roll could be the big one — KAI believes in you! Keep going!",
        "Every roll gets you closer. The next one might just be legendary!",
    ]
    if pity_legendary >= PITY_LEGENDARY_THRESHOLD or pity_mythic >= PITY_MYTHIC_THRESHOLD:
        high_pity = [
            f"You're at **{pity_legendary}** pity for legendary — the next one could be it!",
            "You're so close! KAI can feel it. One more roll!",
        ]
        variants = high_pity + variants
    return random.choice(variants)


def get_kai_quest_claim_message(awarded: int, single: bool = True) -> str:
    """When user claims a quest (single or one of many)."""
    if single:
        return (
            f"Quest complete! **+{awarded}** points. "
            "You're on a roll — KAI is so happy for you!"
        )
    return (
        f"All those points are yours! "
        "You're crushing it — KAI is cheering!"
    )


def get_kai_claim_all_quests_message(total: int, new_balance: int) -> str:
    """When user claims all quests at once."""
    return (
        f"Claimed **{total}** points! New balance: **{new_balance}**. "
        "That's what I call productivity! 🎉"
    )


def get_kai_legendary_roll_message(character_name: str, rarity: str) -> str:
    """When user rolls legendary or mythic — KAI celebrates (Kailove: heart eyes)."""
    r = (rarity or "").strip().lower()
    if r == "mythic":
        return (
            f"**MYTHIC!** You got **{character_name}**! "
            "KAI is so happy for you — that's incredible! 🎉"
        )
    # legendary (or fallback)
    return (
        f"**Legendary!** You rolled **{character_name}**! "
        "KAI loves it — what a pull! 🌟"
    )
