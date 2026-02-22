# utils/mood_tracker.py
from __future__ import annotations

import json
import logging
from typing import Any

import config
from utils.redis_kv import kv_get_json, kv_set_json, kv_del

logger = logging.getLogger("bot.mood_tracker")

MOOD_TTL_SECONDS = 1800  # 30 minutes
MOOD_DECAY_TURNS = 3

_MOOD_VOCAB = [
    "neutral", "happy", "amused", "excited", "contemplative",
    "melancholy", "irritated", "anxious", "playful", "suspicious",
    "impressed", "disappointed",
]


def _key(user_id: int, style_id: str) -> str:
    return f"mood:{int(user_id)}:{style_id}"


async def get_current_mood(user_id: int, style_id: str) -> dict | None:
    return await kv_get_json(_key(user_id, style_id))


async def save_mood(
    user_id: int,
    style_id: str,
    mood: str,
    reason: str,
    intensity: str,
) -> None:
    payload = {"mood": mood, "reason": reason, "intensity": intensity, "turn": 0}
    await kv_set_json(_key(user_id, style_id), payload, ex=MOOD_TTL_SECONDS)


async def advance_mood_turn(user_id: int, style_id: str) -> dict | None:
    """Load the current mood and increment its turn counter.

    Returns the mood dict if still active, or ``None`` if it has decayed
    past ``MOOD_DECAY_TURNS`` (in which case the key is deleted).
    """
    data = await get_current_mood(user_id, style_id)
    if data is None:
        return None
    turn = int(data.get("turn", 0)) + 1
    if turn >= MOOD_DECAY_TURNS:
        await kv_del(_key(user_id, style_id))
        return None
    data["turn"] = turn
    await kv_set_json(_key(user_id, style_id), data, ex=MOOD_TTL_SECONDS)
    return data


def build_mood_prompt_block(mood_data: dict) -> str:
    mood = mood_data.get("mood", "neutral")
    intensity = mood_data.get("intensity", "mild")
    reason = mood_data.get("reason", "")
    lines = [
        "# Current Emotional State",
        f"You are currently feeling {mood} ({intensity})"
        + (f" because: {reason}." if reason else "."),
        "This should subtly color your responses \u2014 don't announce your mood, "
        "just let it influence your tone.",
        "If the user's message improves your mood, you can shift naturally.",
    ]
    return "\n".join(lines)


_ANALYSIS_SYSTEM = """\
You analyze fictional character emotions. Given a short exchange between a \
character and a user, determine the character's emotional state NOW.
Return ONLY a JSON object with three keys:
  "mood" — one of: {vocab}
  "reason" — 5-15 word explanation
  "intensity" — "mild", "moderate", or "strong"
Do NOT include any text outside the JSON object.\
"""


async def analyze_mood_background(
    user_id: int,
    style_id: str,
    character_name: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """Fire-and-forget background task that analyzes the exchange and stores mood."""
    try:
        from utils.ai_client import generate_text

        model = getattr(config, "OPENAI_MODEL_FREE", None) or config.OPENAI_MODEL
        vocab = ", ".join(_MOOD_VOCAB)
        system = _ANALYSIS_SYSTEM.format(vocab=vocab)

        user_prompt = (
            f"Character: {character_name}\n"
            f"User said: {user_text[:300]}\n"
            f"{character_name} replied: {assistant_text[:300]}\n\n"
            "What is the character feeling now? Return JSON only."
        )

        raw = await generate_text(
            user_prompt,
            system=system,
            temperature=0.2,
            max_output_tokens=80,
            model=model,
        )
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        text = text.strip()

        # Tolerate markdown code fences
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed: dict[str, Any] = json.loads(text)
        mood = str(parsed.get("mood", "neutral")).lower()
        if mood not in _MOOD_VOCAB:
            mood = "neutral"
        reason = str(parsed.get("reason", ""))[:120]
        intensity = str(parsed.get("intensity", "mild")).lower()
        if intensity not in ("mild", "moderate", "strong"):
            intensity = "mild"

        if mood == "neutral":
            await kv_del(_key(user_id, style_id))
        else:
            await save_mood(user_id, style_id, mood, reason, intensity)

    except Exception:
        logger.debug("Mood analysis failed (non-fatal)", exc_info=True)
