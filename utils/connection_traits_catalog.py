"""Configurable connection traits shop (enable/disable and base costs).

Edit this file to add/remove shop items. Purchased data lives in Postgres
(`character_connection_profiles`); disabling a trait here only blocks NEW purchases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["connection_trait", "cosmetic", "character_unlock"]


@dataclass(frozen=True)
class ConnectionTraitDef:
    trait_id: str
    base_shard_cost: int
    title: str
    description: str
    category: Category = "connection_trait"
    enabled: bool = True


# Ordered shop listing (IDs stable for stored purchases).
CONNECTION_TRAITS: list[ConnectionTraitDef] = [
    ConnectionTraitDef(
        trait_id="remember_name",
        base_shard_cost=15,
        title="Remember your name",
        description="Bot recalls your name for this character (10-word limit). Name edits cost 15 shards each.",
    ),
    ConnectionTraitDef(
        trait_id="hobbies",
        base_shard_cost=25,
        title="Hobbies & interests",
        description="Up to 3 hobbies (50 words each). Extra slots: +25 shards base, +10% compounding per slot.",
    ),
    ConnectionTraitDef(
        trait_id="speech_style",
        base_shard_cost=50,
        title="Personal speech style",
        description="How you want this character to talk to you (150 words). +100 words per extra purchase; +10% price stack.",
    ),
    ConnectionTraitDef(
        trait_id="weekly_life",
        base_shard_cost=100,
        title="Weekly life update",
        description="250-word weekly status; Sunday reset + DM reminder.",
    ),
    ConnectionTraitDef(
        trait_id="emotion_adapt",
        base_shard_cost=200,
        title="Emotion awareness",
        description="Lightweight mood detection so the character adapts tone.",
    ),
    ConnectionTraitDef(
        trait_id="daily_status",
        base_shard_cost=250,
        title="Daily status",
        description="Edit weekly + daily (100 words/day); week-scoped memory of dailies.",
    ),
    ConnectionTraitDef(
        trait_id="random_dm",
        base_shard_cost=500,
        title="Random check-in DMs",
        description="1–3 DM prompts/day with buttons; replies continue in-server.",
    ),
    ConnectionTraitDef(
        trait_id="memory_semi",
        base_shard_cost=500,
        title="Semi-permanent memory",
        description="Keep weekly/daily statuses ~30 days for prompts.",
    ),
    ConnectionTraitDef(
        trait_id="memory_permanent",
        base_shard_cost=1000,
        title="Long-term memory",
        description="Retain important notes from statuses/messages up to ~1 year (bounded injection).",
    ),
]


def get_trait(trait_id: str) -> ConnectionTraitDef | None:
    tid = (trait_id or "").strip().lower()
    for t in CONNECTION_TRAITS:
        if t.trait_id == tid:
            return t
    return None


def list_enabled_traits() -> list[ConnectionTraitDef]:
    return [t for t in CONNECTION_TRAITS if t.enabled]


def hobby_slot_price_after_expansions(prior_expansions: int) -> int:
    """Price for the next hobby slot expansion (after initial 3 from first purchase)."""
    base = 25
    n = max(0, int(prior_expansions))
    return int(round(base * (1.1 ** (n + 1))))


def speech_style_price_after_expansions(prior_expansions: int) -> int:
    """Price for the next speech-style word-limit expansion."""
    base = 50
    n = max(0, int(prior_expansions))
    return int(round(base * (1.1 ** (n + 1))))
