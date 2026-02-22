# utils/world_lore.py
from __future__ import annotations

"""World lore system — provides global world context and reverse relationships.

Loads world definitions from ``data/worlds.json`` and injects:
  1. A world context block (what world the character lives in, key facts)
  2. A world knowledge block (how much this character understands)
  3. An origin block (where the character came from before the Convergence)
  4. A reverse-relationship block (what OTHER characters think of this one)
"""

import json
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.character_registry import StyleDef

logger = logging.getLogger("bot.world_lore")

WORLDS_FILE = Path("data") / "worlds.json"

_worlds: dict[str, dict[str, Any]] = {}


def _load_worlds() -> dict[str, dict[str, Any]]:
    try:
        if not WORLDS_FILE.exists():
            return {}
        with open(WORLDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("worlds.json root must be an object")
            return {}
        return {str(k).strip().lower(): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        logger.warning("Failed to load worlds.json", exc_info=True)
        return {}


def reload_worlds() -> int:
    """Reload worlds from disk. Returns count loaded."""
    global _worlds
    _worlds = _load_worlds()
    return len(_worlds)


def get_world(world_id: str) -> dict[str, Any] | None:
    wid = (world_id or "").strip().lower()
    return _worlds.get(wid)


def get_all_worlds() -> dict[str, dict[str, Any]]:
    return dict(_worlds)


# ---------------------------------------------------------------------------
# Prompt block builders
# ---------------------------------------------------------------------------

def build_world_context_block(style_obj: "StyleDef") -> str:
    """Build a world context + knowledge + origin block for a character."""
    world_id = getattr(style_obj, "world", None)
    original_world_id = getattr(style_obj, "original_world", None)
    world_knowledge = getattr(style_obj, "world_knowledge", None)

    if not world_id:
        return ""

    wid = str(world_id).strip().lower()
    world = _worlds.get(wid)
    if not world:
        return ""

    lines: list[str] = []

    lines.append("# The World You Live In")
    world_name = world.get("name", wid)
    world_desc = world.get("description", "")
    lines.append(f"You exist in {world_name}.")
    if world_desc:
        lines.append(world_desc)

    if original_world_id:
        oid = str(original_world_id).strip().lower()
        origin = _worlds.get(oid)
        if origin and oid != wid:
            origin_name = origin.get("name", oid)
            origin_desc = origin.get("description", "")
            lines.append("")
            lines.append(f"Your original home was {origin_name}.")
            if origin_desc:
                lines.append(origin_desc)

    if world_knowledge:
        lines.append("")
        lines.append(f"Your understanding of the world: {world_knowledge}")

    return "\n".join(lines)


def build_reverse_relationships_block(
    style_id: str,
    all_styles: dict[str, "StyleDef"],
) -> str:
    """Build a block showing what OTHER characters think of this one.

    Scans all characters' ``relationships`` dicts. If character X has a
    relationship entry keyed to ``style_id``, that opinion is included so
    this character knows about it.
    """
    sid = (style_id or "").strip().lower()
    if not sid:
        return ""

    own_rels = set()
    own_style = all_styles.get(sid)
    if own_style and getattr(own_style, "relationships", None):
        own_rels = {k.strip().lower() for k in own_style.relationships}

    entries: list[str] = []
    for other_id, other_style in all_styles.items():
        if other_id == sid:
            continue
        rels = getattr(other_style, "relationships", None)
        if not rels or not isinstance(rels, dict):
            continue
        for target_id, opinion in rels.items():
            if target_id.strip().lower() == sid:
                other_name = getattr(other_style, "display_name", other_id)
                if other_id in own_rels:
                    entries.append(
                        f"- {other_name} (whom you know): "
                        f'Their opinion of you: "{opinion}"'
                    )
                else:
                    entries.append(
                        f"- {other_name} (you may not know them well): "
                        f'They think of you: "{opinion}"'
                    )

    if not entries:
        return ""

    lines = ["# What Others Think of You"]
    lines.append(
        "These are other characters' opinions of you. You may sense these "
        "dynamics even if you don't know the details. Use this to inform how "
        "you react if someone brings them up."
    )
    lines.extend(entries)
    return "\n".join(lines)


# Load at import time.
reload_worlds()
