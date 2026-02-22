# utils/scene_prompts.py
from __future__ import annotations

from typing import TYPE_CHECKING

from utils.talk_prompts import build_talk_system_prompt

if TYPE_CHECKING:
    from utils.character_registry import StyleDef


def build_scene_system_prompt(
    *,
    language: str,
    style_prompt: str,
    style_obj: StyleDef | None = None,
) -> str:
    """Scene system prompt.

    Uses the shared talk system prompt builder (mode="scene") for consistency.
    """
    return build_talk_system_prompt(
        language=(language or "english"),
        style_prompt=(style_prompt or ""),
        mode="scene",
        max_chars=1900,
        max_paragraphs=6,
        style_obj=style_obj,
    )


def build_scene_user_prompt(
    *,
    setting: str | None,
    transcript_lines: list[str],
    direction: str,
    user_message: str,
) -> str:
    # keep prompt compact and consistent
    parts: list[str] = []

    if setting:
        parts.append(f"SETTING:\n{setting.strip()}\n")

    if transcript_lines:
        parts.append("RECENT SCENE:\n" + "\n".join(transcript_lines) + "\n")

    d = (direction or "").strip()
    if d:
        parts.append(f"DIRECTION (how to reply):\n{d}\n")

    parts.append(f"YOUR TURN:\n{(user_message or '').strip()}\n")
    return "\n".join(parts).strip()

