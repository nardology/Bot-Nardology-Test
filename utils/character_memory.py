# utils/character_memory.py
from __future__ import annotations

import json
import logging
import re
from typing import Any

import config

logger = logging.getLogger("bot.character_memory")

MAX_MEMORIES_PER_PAIR = 10

# ---------------------------------------------------------------------------
# Keyword extraction patterns (runs synchronously, no AI call)
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("user_name", re.compile(
        r"(?:my name is|i'm|i am|call me)\s+([A-Z][a-z]+)", re.IGNORECASE
    )),
    ("likes", re.compile(
        r"(?:i (?:really )?(?:love|like|enjoy|adore))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE
    )),
    ("dislikes", re.compile(
        r"(?:i (?:really )?(?:hate|dislike|can't stand))\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE
    )),
    ("favorite", re.compile(
        r"(?:my favorite .+? is)\s+(.+?)(?:\.|,|!|$)", re.IGNORECASE
    )),
]


def extract_keyword_memories(user_text: str) -> list[dict[str, str]]:
    """Extract personal facts from *user_text* using regex patterns.

    Returns a list of ``{"key": ..., "value": ...}`` dicts.
    """
    if not user_text:
        return []
    results: list[dict[str, str]] = []
    for label, pat in _PATTERNS:
        m = pat.search(user_text)
        if m:
            value = m.group(1).strip()
            if value and len(value) < 200:
                results.append({"key": label, "value": value})
    return results


# ---------------------------------------------------------------------------
# Postgres persistence helpers
# ---------------------------------------------------------------------------

async def load_memories(user_id: int, style_id: str, *, limit: int = 10) -> list[dict]:
    """Fetch stored memory anchors from Postgres (newest first, capped at *limit*)."""
    try:
        from utils.db import get_sessionmaker
        from utils.models import CharacterMemory
        from sqlalchemy import select  # type: ignore

        Session = get_sessionmaker()
        async with Session() as session:
            rows = await session.execute(
                select(CharacterMemory)
                .where(CharacterMemory.user_id == int(user_id))
                .where(CharacterMemory.style_id == str(style_id))
                .order_by(CharacterMemory.created_at.desc())
                .limit(limit)
            )
            return [
                {"key": r.memory_key, "value": r.memory_value, "source": r.source}
                for r in rows.scalars().all()
            ]
    except Exception:
        logger.debug("load_memories failed", exc_info=True)
        return []


async def save_memory(
    user_id: int,
    style_id: str,
    key: str,
    value: str,
    source: str = "keyword",
) -> bool:
    """Upsert a memory anchor (dedup by key), enforce per-pair cap."""
    try:
        from utils.db import get_sessionmaker
        from utils.models import CharacterMemory
        from sqlalchemy import select, delete, func  # type: ignore

        Session = get_sessionmaker()
        async with Session() as session:
            existing = await session.execute(
                select(CharacterMemory)
                .where(CharacterMemory.user_id == int(user_id))
                .where(CharacterMemory.style_id == str(style_id))
                .where(CharacterMemory.memory_key == str(key))
            )
            row = existing.scalar_one_or_none()
            if row:
                row.memory_value = str(value)[:512]
                row.source = source
            else:
                count_result = await session.execute(
                    select(func.count())
                    .select_from(CharacterMemory)
                    .where(CharacterMemory.user_id == int(user_id))
                    .where(CharacterMemory.style_id == str(style_id))
                )
                count = count_result.scalar() or 0
                if count >= MAX_MEMORIES_PER_PAIR:
                    oldest = await session.execute(
                        select(CharacterMemory.id)
                        .where(CharacterMemory.user_id == int(user_id))
                        .where(CharacterMemory.style_id == str(style_id))
                        .order_by(CharacterMemory.created_at.asc())
                        .limit(1)
                    )
                    oldest_id = oldest.scalar_one_or_none()
                    if oldest_id:
                        await session.execute(
                            delete(CharacterMemory).where(CharacterMemory.id == oldest_id)
                        )
                session.add(CharacterMemory(
                    user_id=int(user_id),
                    style_id=str(style_id),
                    memory_key=str(key)[:128],
                    memory_value=str(value)[:512],
                    source=source,
                ))
            await session.commit()
            return True
    except Exception:
        logger.debug("save_memory failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Background AI extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You extract personal facts about a user from a conversation with a fictional character.
Return ONLY a JSON array of objects with "key" (short label, e.g. "user_name", \
"hobby", "pet", "job") and "value" (the fact). Return [] if nothing memorable.
Maximum 2 items. Do NOT include any text outside the JSON array.\
"""


async def extract_ai_memories_background(
    user_id: int,
    style_id: str,
    character_name: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """Fire-and-forget background task: extract memorable facts via AI call."""
    try:
        from utils.ai_client import generate_text

        model = getattr(config, "OPENAI_MODEL_FREE", None) or config.OPENAI_MODEL

        user_prompt = (
            f"User said to {character_name}: {user_text[:400]}\n"
            f"{character_name} replied: {assistant_text[:400]}\n\n"
            "Extract 0-2 personal facts about the user worth remembering. JSON array only."
        )

        raw = await generate_text(
            user_prompt,
            system=_EXTRACT_SYSTEM,
            temperature=0.2,
            max_output_tokens=120,
            model=model,
        )
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        items: list[dict[str, str]] = json.loads(text)
        if not isinstance(items, list):
            return

        existing = await load_memories(user_id, style_id, limit=MAX_MEMORIES_PER_PAIR)
        existing_keys = {m["key"] for m in existing}

        for item in items[:2]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()[:128]
            value = str(item.get("value", "")).strip()[:512]
            if key and value and key not in existing_keys:
                await save_memory(user_id, style_id, key, value, source="ai")
                existing_keys.add(key)

    except Exception:
        logger.debug("AI memory extraction failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Prompt block builder
# ---------------------------------------------------------------------------

def build_memory_prompt_block(memories: list[dict], bond_level: int) -> str:
    """Format memory anchors for injection into the system prompt.

    Bond gating:
        3-4  → up to 3 memories
        5-9  → up to 6 memories
        10+  → all (up to MAX_MEMORIES_PER_PAIR)
    """
    if not memories or bond_level < 3:
        return ""
    if bond_level < 5:
        memories = memories[:3]
    elif bond_level < 10:
        memories = memories[:6]
    else:
        memories = memories[:MAX_MEMORIES_PER_PAIR]

    lines = ["# Things You Remember About This User"]
    for m in memories:
        lines.append(f"- {m.get('value', m.get('key', ''))}")
    lines.append(
        "Use these memories naturally \u2014 reference them when relevant, but don't list them."
    )
    return "\n".join(lines)
