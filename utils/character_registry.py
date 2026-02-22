from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import random
import os
import json
import logging
from pathlib import Path

logger = logging.getLogger("bot.character_registry")

# --- Global disable list ("nuked" characters) ---
# Disabled characters are treated as removed from the game:
# - not rollable
# - not selectable
# - hidden from listings
_DISABLED_FILE = Path("data") / "disabled_characters.json"
_DISABLED: set[str] = set()


def _load_disabled() -> None:
    global _DISABLED
    try:
        if _DISABLED_FILE.exists():
            data = json.load(open(_DISABLED_FILE, "r", encoding="utf-8"))
            if isinstance(data, list):
                _DISABLED = {str(x).strip().lower() for x in data if str(x).strip()}
    except Exception:
        # Fail-open: don't crash the bot if file is corrupt.
        _DISABLED = set()


def _save_disabled() -> None:
    try:
        _DISABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
        json.dump(sorted(_DISABLED), open(_DISABLED_FILE, "w", encoding="utf-8"), indent=2)
    except Exception:
        # Best-effort
        pass


def is_style_disabled(style_id: str | None) -> bool:
    sid = (style_id or "").strip().lower()
    return bool(sid) and sid in _DISABLED


def disable_style_globally(style_id: str) -> None:
    sid = (style_id or "").strip().lower()
    if not sid:
        return
    _DISABLED.add(sid)
    _save_disabled()


def enable_style_globally(style_id: str) -> None:
    sid = (style_id or "").strip().lower()
    if not sid:
        return
    _DISABLED.discard(sid)
    _save_disabled()


# ---------------------------------------------------------------------------
# Backwards-compatibility aliases
#
# Older parts of the codebase (e.g. utils/character_store.py) import
# `disable_style` / `enable_style` from this module. Those names were
# renamed to "*_globally" during a refactor. Keep aliases so older
# imports don't crash.


def disable_style(style_id: str) -> None:
    """Alias for disable_style_globally."""
    disable_style_globally(style_id)


def enable_style(style_id: str) -> None:
    """Alias for enable_style_globally."""
    enable_style_globally(style_id)


Rarity = Literal["common", "uncommon", "rare", "legendary", "mythic"]
RARITY_ORDER: list[Rarity] = ["common", "uncommon", "rare", "legendary", "mythic"]

@dataclass(frozen=True)
class StyleDef:
    style_id: str
    display_name: str
    rarity: Rarity
    color: int  # Discord embed color (int)
    prompt: str

    # NEW UX fields:
    description: str
    tips: list[str]

    # Optional future features:
    image_url: str | None = None
    rollable: bool = True  # event-only styles can set this False

    # Pack support (for premium "packs" later). Defaults to "core".
    pack_id: str = "core"

    # Optional tags for filtering/searching.
    tags: list[str] | None = None

    # Optional max bond cap (by level). If set, bond XP will stop once this level is reached.
    max_bond_level: int | None = None

    # Per-character image overrides (custom packs/server-only chars).
    emotion_images: dict[str, str] | None = None
    bond_images: list[str] | None = None

    # --- Structured persona fields ---
    # These enable rich, human-like character definitions. When present,
    # build_talk_system_prompt() assembles them into a detailed persona block.
    # Characters that only set ``prompt`` still work identically.
    backstory: str | None = None
    personality_traits: list[str] | None = None
    quirks: list[str] | None = None
    speech_style: str | None = None
    fears: list[str] | None = None
    desires: list[str] | None = None
    likes: list[str] | None = None
    dislikes: list[str] | None = None
    catchphrases: list[str] | None = None
    secrets: list[str] | None = None
    lore: str | None = None
    age: str | None = None
    occupation: str | None = None
    relationships: dict[str, str] | None = None
    topic_reactions: dict[str, str] | None = None

    # --- World context fields ---
    # Reference a world id from data/worlds.json.
    world: str | None = None
    original_world: str | None = None
    world_knowledge: str | None = None

BASE_STYLE_IDS = {"fun", "serious"}

# --- Shop item definitions discovered during JSON loading ---
_SHOP_ITEM_DEFS: dict[str, dict] = {}


def get_shop_item_defs() -> dict[str, dict]:
    """Return a copy of shop item definitions captured from character JSON files."""
    return dict(_SHOP_ITEM_DEFS)

STYLE_DEFS: dict[str, StyleDef] = {
    "fun": StyleDef(
        style_id="fun",
        display_name="Fun",
        rarity="common",
        color=0x2ECC71,
        prompt="You're a naturally upbeat and cheerful person. You crack jokes, use casual slang, and find something fun in every topic. You get genuinely excited about interesting questions and a little impatient with boring ones. You laugh easily and sometimes go on mini tangents when something sparks your interest.",
        description="Upbeat, friendly replies with light humor.",
        tips=[
            "Great for casual questions",
            "Keeps answers short and lively",
        ],
        rollable=False,
    ),
    "serious": StyleDef(
        style_id="serious",
        display_name="Serious",
        rarity="common",
        color=0x95A5A6,
        prompt="You're a composed, no-nonsense professional. You value precision and dislike wasting time. You get quietly frustrated when people ask vague questions, and you feel genuine satisfaction when you nail an explanation. Dry humor slips out occasionally. You respect effort and have zero patience for laziness.",
        description="Direct, calm, and professional responses.",
        tips=[
            "Best for school/work questions",
            "Focuses on clarity and accuracy",
        ],
        rollable=False,
    ),
    # All rollable characters are loaded from data/characters/ JSON files.
    # See data/characters/core/ for the built-in roster.
}

# Snapshot of style ids defined in-code (before external JSON merges).
_BUILTIN_STYLE_IDS = set(STYLE_DEFS.keys())


def _load_disabled_style_ids() -> set[str]:
    try:
        if _DISABLED_FILE.exists():
            raw = _DISABLED_FILE.read_text(encoding="utf-8") or "[]"
            data = json.loads(raw)
            if isinstance(data, list):
                return {str(x).strip().lower() for x in data if str(x).strip()}
    except Exception:
        logger.exception("Failed to load disabled characters list")
    return set()


def _save_disabled_style_ids(ids: set[str]) -> None:
    try:
        _DISABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DISABLED_FILE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save disabled characters list")


DISABLED_STYLE_IDS: set[str] = _load_disabled_style_ids()


def is_style_disabled(style_id: str | None) -> bool:
    sid = (style_id or "").strip().lower()
    return bool(sid and sid in DISABLED_STYLE_IDS)


def disable_style_globally(style_id: str) -> None:
    sid = (style_id or "").strip().lower()
    if not sid:
        return
    DISABLED_STYLE_IDS.add(sid)
    _save_disabled_style_ids(DISABLED_STYLE_IDS)


def _parse_color(v: object) -> int:
    """Accept an int or a hex string like "#FFAA00" / "0xFFAA00"."""
    if isinstance(v, int):
        return int(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s.startswith("#"):
            s = s[1:]
        if s.startswith("0x"):
            s = s[2:]
        try:
            return int(s, 16)
        except Exception:
            return 0x5865F2
    return 0x5865F2


def _opt_str(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _opt_str_list(d: dict, key: str) -> list[str] | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, list):
        return None
    out = [str(x).strip() for x in v if str(x).strip()]
    return out or None


def _opt_str_dict(d: dict, key: str) -> dict[str, str] | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, dict):
        return None
    out = {str(k).strip(): str(val).strip() for k, val in v.items() if str(k).strip() and str(val).strip()}
    return out or None


def _styledef_from_dict(d: dict) -> StyleDef:
    sid = (d.get("id") or d.get("style_id") or "").strip().lower()
    if not sid:
        raise ValueError("missing 'id'")

    # "random" is reserved and should never be a real character id.
    # Some users accidentally create a character named/id "random" while testing.
    if sid == "random":
        raise ValueError("reserved id: random")

    rarity = (d.get("rarity") or "common").strip().lower()
    if rarity not in set(RARITY_ORDER):
        raise ValueError(f"invalid rarity: {rarity}")

    tips = d.get("tips")
    if tips is None:
        tips = []
    if not isinstance(tips, list):
        raise ValueError("tips must be a list")

    return StyleDef(
        style_id=sid,
        display_name=str(d.get("display_name") or d.get("name") or sid).strip(),
        rarity=rarity,  # type: ignore[arg-type]
        color=_parse_color(d.get("color")),
        prompt=str(d.get("prompt") or "").strip(),
        description=str(d.get("description") or "").strip(),
        tips=[str(x) for x in tips if str(x).strip()],
        image_url=(str(d.get("image_url")).strip() if d.get("image_url") else None),
        rollable=bool(d.get("rollable", True)),
        pack_id=str(d.get("pack_id") or "core").strip() or "core",
        tags=([str(x) for x in (d.get("tags") or [])] if d.get("tags") is not None else None),
        max_bond_level=(int(d.get("max_bond_level")) if d.get("max_bond_level") is not None else None),
        emotion_images=(
            {str(k).strip().lower(): str(v).strip() for k, v in (d.get("emotion_images") or {}).items() if str(k).strip() and str(v).strip()}
            if isinstance(d.get("emotion_images"), dict)
            else None
        ),
        bond_images=(
            [str(x).strip() for x in (d.get("bond_images") or []) if str(x).strip()]
            if isinstance(d.get("bond_images"), list)
            else None
        ),
        # Structured persona fields
        backstory=_opt_str(d, "backstory"),
        personality_traits=_opt_str_list(d, "personality_traits"),
        quirks=_opt_str_list(d, "quirks"),
        speech_style=_opt_str(d, "speech_style"),
        fears=_opt_str_list(d, "fears"),
        desires=_opt_str_list(d, "desires"),
        likes=_opt_str_list(d, "likes"),
        dislikes=_opt_str_list(d, "dislikes"),
        catchphrases=_opt_str_list(d, "catchphrases"),
        secrets=_opt_str_list(d, "secrets"),
        lore=_opt_str(d, "lore"),
        age=_opt_str(d, "age"),
        occupation=_opt_str(d, "occupation"),
        relationships=_opt_str_dict(d, "relationships"),
        topic_reactions=_opt_str_dict(d, "topic_reactions"),
        # World context fields
        world=_opt_str(d, "world"),
        original_world=_opt_str(d, "original_world"),
        world_knowledge=_opt_str(d, "world_knowledge"),
    )


def _capture_shop_item(payload: dict, style_id: str) -> None:
    """If the payload contains a ``shop_item`` block, store it in ``_SHOP_ITEM_DEFS``."""
    raw = payload.get("shop_item")
    if not isinstance(raw, dict):
        return
    si = dict(raw)
    si.setdefault("item_id", style_id)
    si.setdefault("style_id", style_id)
    si.setdefault("kind", "character_grant")
    _SHOP_ITEM_DEFS[style_id] = si


def load_external_characters(*, directory: str | None = None) -> dict[str, StyleDef]:
    """Load characters from JSON files, scanning subdirectories recursively.

    Supports three file formats:
        A) a single character object (type omitted or type="character")
        B) a pack object with ``type: "pack"`` and ``characters: [...]``
        C) an array of character objects

    Directory structure example::

        data/characters/
            core/          # built-in rollable characters
            shop/          # limited shop characters
            seasonal/      # seasonal event characters

    Override the root directory with env var ``CHARACTER_DIR``.
    """
    dir_path = (directory or os.getenv("CHARACTER_DIR") or "data/characters").strip()
    if not dir_path:
        return {}

    if not os.path.isdir(dir_path):
        return {}

    out: dict[str, StyleDef] = {}
    for root, _dirs, files in os.walk(dir_path):
        for name in sorted(files):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)

                if isinstance(payload, dict) and (payload.get("type") == "pack"):
                    pack_id = str(payload.get("pack_id") or "core").strip() or "core"
                    chars = payload.get("characters") or []
                    if not isinstance(chars, list):
                        raise ValueError("pack.characters must be a list")
                    for c in chars:
                        if not isinstance(c, dict):
                            continue
                        c = dict(c)
                        c.setdefault("pack_id", pack_id)
                        s = _styledef_from_dict(c)
                        out[s.style_id] = s
                        _capture_shop_item(c, s.style_id)
                elif isinstance(payload, dict):
                    s = _styledef_from_dict(payload)
                    out[s.style_id] = s
                    _capture_shop_item(payload, s.style_id)
                elif isinstance(payload, list):
                    for c in payload:
                        if not isinstance(c, dict):
                            continue
                        s = _styledef_from_dict(c)
                        out[s.style_id] = s
                        _capture_shop_item(c, s.style_id)
                else:
                    raise ValueError("unsupported JSON root")

            except Exception as e:
                logger.warning("Failed to load character file %s: %s", path, e)

    return out


def merge_pack_payload(payload: dict) -> int:
    """Merge a pack payload (same shape as JSON pack files).

    This is used by /packs to make newly-created custom packs available
    immediately without a restart.
    """
    try:
        if not isinstance(payload, dict):
            return 0
        if str(payload.get("type") or "").lower() != "pack":
            return 0
        pack_id = str(payload.get("pack_id") or "core").strip() or "core"
        chars = payload.get("characters") or []
        if not isinstance(chars, list):
            return 0

        added = 0
        for c in chars:
            if not isinstance(c, dict):
                continue
            cc = dict(c)
            cc.setdefault("pack_id", pack_id)
            s = _styledef_from_dict(cc)
            STYLE_DEFS[s.style_id] = s  # allow override for dynamic packs
            added += 1
        return added
    except Exception:
        return 0


def _merge_external_definitions() -> None:
    """Merge external JSON characters into STYLE_DEFS.

    By default, external files do NOT override built-ins.
    Set env var `CHARACTER_OVERRIDE=1` to allow overriding.
    """
    ext = load_external_characters()
    if not ext:
        return

    allow_override = str(os.getenv("CHARACTER_OVERRIDE") or "").strip() in {"1", "true", "yes"}
    added = 0
    for sid, s in ext.items():
        if sid in STYLE_DEFS and not allow_override:
            continue
        STYLE_DEFS[sid] = s
        added += 1

    if added:
        logger.info("Loaded %d external character definitions.", added)


# Load external JSON characters at import time (safe no-op if folder doesn't exist).
_merge_external_definitions()


def _clone_with_pack(s: StyleDef, pack_id: str) -> StyleDef:
    return StyleDef(
        style_id=s.style_id,
        display_name=s.display_name,
        rarity=s.rarity,
        color=s.color,
        prompt=s.prompt,
        description=s.description,
        tips=list(s.tips or []),
        image_url=s.image_url,
        rollable=s.rollable,
        pack_id=pack_id,
        tags=list(s.tags) if s.tags is not None else None,
        max_bond_level=s.max_bond_level,
        emotion_images=(dict(s.emotion_images) if s.emotion_images is not None else None),
        bond_images=(list(s.bond_images) if s.bond_images is not None else None),
        backstory=s.backstory,
        personality_traits=list(s.personality_traits) if s.personality_traits is not None else None,
        quirks=list(s.quirks) if s.quirks is not None else None,
        speech_style=s.speech_style,
        fears=list(s.fears) if s.fears is not None else None,
        desires=list(s.desires) if s.desires is not None else None,
        likes=list(s.likes) if s.likes is not None else None,
        dislikes=list(s.dislikes) if s.dislikes is not None else None,
        catchphrases=list(s.catchphrases) if s.catchphrases is not None else None,
        secrets=list(s.secrets) if s.secrets is not None else None,
        lore=s.lore,
        age=s.age,
        occupation=s.occupation,
        relationships=dict(s.relationships) if s.relationships is not None else None,
        topic_reactions=dict(s.topic_reactions) if s.topic_reactions is not None else None,
        world=s.world,
        original_world=s.original_world,
        world_knowledge=s.world_knowledge,
    )


def _assign_builtin_pack_defaults() -> None:
    """Assign all rollable characters with pack_id=="core" into the nardologybot pack.

    This covers both hardcoded in-code styles AND JSON-loaded characters from
    data/characters/core/ that default to pack_id="core".  Any character that
    explicitly sets a different pack_id (e.g. shop or seasonal packs) is left alone.
    """
    for sid in list(STYLE_DEFS.keys()):
        s = STYLE_DEFS.get(sid)
        if s is None:
            continue
        if not s.rollable:
            continue
        if (s.pack_id or "core") != "core":
            continue
        STYLE_DEFS[sid] = _clone_with_pack(s, "nardologybot")


_assign_builtin_pack_defaults()

def get_style(style_id: str) -> StyleDef | None:
    sid = (style_id or "").strip().lower()
    if is_style_disabled(sid):
        return None
    return STYLE_DEFS.get(sid)

def list_rollable_by_rarity(r: Rarity) -> list[StyleDef]:
    return [
        s for s in STYLE_DEFS.values()
        if s.rollable and s.rarity == r and not is_style_disabled(s.style_id)
    ]


def list_rollable(
    *,
    rarity: Rarity | None = None,
    pack_ids: set[str] | None = None,
) -> list[StyleDef]:
    """List rollable characters with optional rarity + pack filtering."""
    out: list[StyleDef] = []
    for s in STYLE_DEFS.values():
        # NOTE: StyleDef uses `style_id` (not `id`).
        if is_style_disabled(s.style_id):
            continue
        if not s.rollable:
            continue
        if rarity is not None and s.rarity != rarity:
            continue
        if pack_ids is not None and (s.pack_id or "core") not in pack_ids:
            continue
        out.append(s)
    return out

# ---- Your rarity model as sequential checks (matches your 1/x notes) ----
# mythic: 1/1000
# else legendary: 1/100
# else rare: 1/20
# else uncommon: 1/10
# else common
#
# Pity: doubles ONLY legendary+mythic odds each miss, capped (no free guarantees).
def _cap(p: float, cap: float = 0.50) -> float:
    return min(max(p, 0.0), cap)

def choose_rarity(
    *,
    pity_legendary: int,
    pity_mythic: int,
    rng: random.Random,
    legendary_mult: float = 1.0,
    mythic_mult: float = 1.0,
) -> Rarity:
    """Choose rarity with optional multipliers.

    Notes on pity (product sanity):
    - Legendary: soft ramp after 20 misses; hard guarantee at 100th roll (pity >= 99).
    - Mythic: soft ramp after 200 misses; hard guarantee at 1000th roll (pity >= 999).

    Multipliers are applied *before* pity adjustments.
    """
    pity_legendary = max(0, int(pity_legendary or 0))
    pity_mythic = max(0, int(pity_mythic or 0))

    base_mythic = (1.0 / 1000.0) * max(0.0, float(mythic_mult or 1.0))
    base_legendary = (1.0 / 100.0) * max(0.0, float(legendary_mult or 1.0))
    base_rare = 1.0 / 20.0
    base_uncommon = 1.0 / 10.0

    # --- Mythic (check first)
    if pity_mythic >= 999:
        return "mythic"

    # Soft-ramp mythic after 200 misses: +0.002% per miss, capped at 1%.
    mythic_ramp = max(0, pity_mythic - 200) * 0.00002
    p_mythic = min(base_mythic + mythic_ramp, 0.01)
    if rng.random() < p_mythic:
        return "mythic"

    # --- Legendary
    if pity_legendary >= 99:
        return "legendary"

    # Soft-ramp legendary after 20 misses: +0.05% per miss, capped at 10%.
    legendary_ramp = max(0, pity_legendary - 20) * 0.0005
    p_legendary = min(base_legendary + legendary_ramp, 0.10)
    if rng.random() < p_legendary:
        return "legendary"

    if rng.random() < base_rare:
        return "rare"
    if rng.random() < base_uncommon:
        return "uncommon"
    return "common"

def roll_style(
    *,
    pity_legendary: int,
    pity_mythic: int,
    rng: random.Random,
    legendary_mult: float = 1.0,
    mythic_mult: float = 1.0,
    pack_ids: set[str] | None = None,
) -> StyleDef:
    rarity = choose_rarity(
        pity_legendary=pity_legendary,
        pity_mythic=pity_mythic,
        rng=rng,
        legendary_mult=legendary_mult,
        mythic_mult=mythic_mult,
    )

    # If no styles exist in that rarity, fall back downward.
    idx = RARITY_ORDER.index(rarity)
    for i in range(idx, -1, -1):
        pool = list_rollable(rarity=RARITY_ORDER[i], pack_ids=pack_ids)
        if pool:
            return rng.choice(pool)

    # If nothing rollable exists under the requested pack filter, do NOT silently
    # leak a character from a different pack (this is how "pirate" can show up
    # even when its pack is disabled). Instead:
    #   1) If pack_ids were provided, try any rollable in those packs (ignoring rarity)
    #   2) If still empty, fall back to any rollable overall
    if pack_ids is not None:
        pool_any = list_rollable(rarity=None, pack_ids=pack_ids)
        if pool_any:
            return rng.choice(pool_any)

    pool_all = list_rollable(rarity=None, pack_ids=None)
    if pool_all:
        return rng.choice(pool_all)

    # Absolute last resort: return pirate if it exists.
    return STYLE_DEFS.get("pirate") or next(iter(STYLE_DEFS.values()))
