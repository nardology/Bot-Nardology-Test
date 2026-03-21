"""Lightweight user-text emotion label for connection trait `emotion_adapt`."""
from __future__ import annotations

import hashlib
import logging
import re

import config

logger = logging.getLogger("bot.emotion_classifier")

_LABELS = (
    "neutral",
    "happy",
    "anxious",
    "angry",
    "sad",
    "depressed",
    "excited",
    "lonely",
    "stressed",
    "hopeful",
)


def _keyword_fallback(text: str) -> str:
    t = (text or "").lower()
    if any(x in t for x in ("depress", "hopeless", "empty inside", "can't go on")):
        return "depressed"
    if any(x in t for x in ("anxious", "panic", "worried", "nervous")):
        return "anxious"
    if any(x in t for x in ("angry", "furious", "pissed")):
        return "angry"
    if any(x in t for x in ("sad", "crying", "miss you", "heartbroken")):
        return "sad"
    if any(x in t for x in ("happy", "great day", "excited", "yay")):
        return "happy"
    if any(x in t for x in ("lonely", "alone", "no one")):
        return "lonely"
    if any(x in t for x in ("stress", "overwhelmed", "burnout")):
        return "stressed"
    return "neutral"


async def classify_emotion_line(text: str) -> str:
    """Return a single label from _LABELS (best-effort). ~0 extra tokens if keyword matches."""
    line = (text or "").strip()
    if not line:
        return "neutral"

    fb = _keyword_fallback(line)
    if fb != "neutral":
        return fb

    if getattr(config, "AI_DISABLED", False) or not (config.OPENAI_API_KEY or "").strip():
        return "neutral"

    # Optional tiny model call (bounded); failures fall back to neutral.
    try:
        from utils.ai_client import generate_text

        sys_prompt = (
            "You classify the writer's emotional tone in one English word from this list only: "
            + ", ".join(_LABELS)
            + ". Reply with that single word, nothing else."
        )
        out = await generate_text(
            line[:2000],
            system=sys_prompt,
            max_output_tokens=8,
            temperature=0.2,
            model=getattr(config, "OPENAI_MODEL_FREE", config.OPENAI_MODEL),
        )
        w = re.sub(r"[^a-zA-Z]", "", (out or "").strip().lower())
        for lab in _LABELS:
            if lab == w or w.startswith(lab):
                return lab
    except Exception:
        logger.debug("emotion AI fallback", exc_info=True)
    return "neutral"


def cache_key_for_message(message_id: int) -> str:
    return hashlib.sha256(f"emo:{message_id}".encode()).hexdigest()[:24]


async def classify_emotion(text: str) -> str:
    """Public alias for connection traits /talk."""
    return await classify_emotion_line(text)
