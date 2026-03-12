# utils/safety.py
from __future__ import annotations

import re
from typing import Optional

from utils.storage import get_guild_settings

# Always-on filter: blocked globally regardless of server safety mode.
GLOBAL_BLOCKED_PATTERNS = [
    r"\b(sex|sexy|sexual|nude|nudes|nudity|porn|pornography|hentai)\b",
    r"\b(blowjob|handjob|anal|orgasm|erotic|fetish|bondage)\b",
    r"\b(xxx|nsfw|onlyfans|r34|rule\s*34)\b",
]

# Extra patterns only enforced when a server opts into strict mode.
STRICT_PATTERNS = [
    r"\b(how to|best way to)\s+(kill myself|self harm|cut myself|suicide)\b",
    r"\b(dox|doxx|address|phone number|ip address)\b",
]

def _normalize(s: str) -> str:
    return (s or "").strip().lower()

def _topic_hit(text: str, topic: str) -> bool:
    t = _normalize(text)
    q = _normalize(topic)
    if not q:
        return False
    return q in t

async def get_safety_mode(guild_id: int) -> str:
    s = await get_guild_settings(guild_id)
    mode = (s.get("ai_safety_mode") or "standard").strip().lower()
    return mode if mode in ("standard", "strict") else "standard"

async def check_blocked_topics(guild_id: int, text: str) -> Optional[str]:
    s = await get_guild_settings(guild_id)
    topics = s.get("ai_blocked_topics", [])
    if not isinstance(topics, list) or not topics:
        return None
    for topic in topics:
        if isinstance(topic, str) and _topic_hit(text, topic):
            return f"This server blocks the topic: `{topic}`."
    return None

def check_global_blocked(text: str) -> Optional[str]:
    """Always-on content filter. Returns a refusal reason or None."""
    t = _normalize(text)
    for pat in GLOBAL_BLOCKED_PATTERNS:
        try:
            if re.search(pat, t, flags=re.IGNORECASE):
                return "This request contains content that isn't allowed."
        except Exception:
            continue
    return None

async def check_strict_filters(guild_id: int, text: str) -> Optional[str]:
    mode = await get_safety_mode(guild_id)
    if mode != "strict":
        return None
    t = _normalize(text)
    for pat in STRICT_PATTERNS:
        try:
            if re.search(pat, t, flags=re.IGNORECASE):
                return "This request isn't allowed in **Strict Safety Mode**."
        except Exception:
            continue
    return None

# Abuse: prompts that ask for very long content or look like spam (cost drain).
PROMPT_ABUSE_PATTERNS = [
    r"\b(2500|2000|3000|5000)\s*(\w+\s+)?words?\b",
    r"\b(write|generate|produce|create)\s+(a\s+)?(\d+\s*)?(word\s+)?essay\b",
    r"\bextract\s+(all|every|entire|lengthy)\b",
    r"\b(as\s+long\s+as\s+possible|maximum\s+length|full\s+length)\b",
    r"\b(complete\s+)?(document|article|report)\s+(of|with)\s+(\d+\s*)?words\b",
]


def check_prompt_abuse(prompt: str) -> Optional[str]:
    """
    Returns a refusal reason if the prompt looks like abuse (long-content requests, spam).
    Used to prevent /talk from being used to drain AI budget (essays, data dumps, etc.).
    """
    if not prompt or len(prompt) < 100:
        return None
    t = _normalize(prompt)
    for pat in PROMPT_ABUSE_PATTERNS:
        try:
            if re.search(pat, t, flags=re.IGNORECASE):
                return (
                    "This looks like a request for very long or bulk content. "
                    "Keep messages short for character chat (e.g. a few sentences)."
                )
        except Exception:
            continue
    # High repetition: same character or very repeated substring (spam / paste dump)
    if len(prompt) > 600:
        from collections import Counter
        counts = Counter(t.replace(" ", ""))
        if counts:
            most_common = counts.most_common(1)[0][1]
            if most_common / max(1, len(t.replace(" ", ""))) > 0.45:
                return "Message looks like spam or repeated characters. Please send a normal short message."
    return None


async def safety_gate(guild_id: int, user_text: str) -> Optional[str]:
    """
    Returns a human-readable refusal reason, or None if allowed.
    """
    global_hit = check_global_blocked(user_text)
    if global_hit:
        return global_hit
    hit = await check_blocked_topics(guild_id, user_text)
    if hit:
        return hit
    hit2 = await check_strict_filters(guild_id, user_text)
    if hit2:
        return hit2
    abuse = check_prompt_abuse(user_text)
    if abuse:
        return abuse
    return None
