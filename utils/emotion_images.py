from __future__ import annotations

import random
import re
from typing import Optional

from utils.character_emotion_manifest import CHARACTER_IMAGES, _DEFAULT_CDN_BASE
from utils.emotion_predictor import predict_emotion

# Regex to find an [EMOTION:xxx] tag anywhere in text (case-insensitive).
_EMOTION_TAG_RE = re.compile(r"\[EMOTION:\s*(\w+)\s*\]", re.IGNORECASE)

VALID_EMOTIONS = frozenset({
    "happy", "sad", "angry", "mad", "scared",
    "confused", "neutral", "affectionate", "excited",
})


def parse_emotion_tag(text: str) -> tuple[str, str | None]:
    """Extract and strip an ``[EMOTION:xxx]`` tag from LLM output.

    Returns ``(cleaned_text, emotion_key)`` where *emotion_key* is
    lowercased and validated, or ``None`` if no valid tag was found.
    """
    m = _EMOTION_TAG_RE.search(text)
    if not m:
        return text, None
    raw = m.group(1).strip().lower()
    cleaned = text[:m.start()] + text[m.end():]
    cleaned = cleaned.strip()
    if raw in VALID_EMOTIONS:
        return cleaned, raw
    return cleaned, None


def _as_url(path_or_url: str) -> str:
    """Convert a manifest path into a full URL.

    Manifest values are relative to the static jsDelivr CDN (not the R2
    override), since emotion/bond images ship with the GitHub repo.
    Full URLs and ``asset:`` paths pass through unchanged.
    """
    s = (path_or_url or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("asset:"):
        return s
    base = (_DEFAULT_CDN_BASE or "").rstrip("/")
    return f"{base}/{s}" if base else ""


def _style_emotions(style_obj) -> dict[str, str] | None:
    """Best-effort extraction of per-style emotion images.

    Custom pack characters can store these directly on the StyleDef as `emotion_images`.
    Values may be full URLs or manifest-style filenames.
    """
    if style_obj is None:
        return None
    em = getattr(style_obj, "emotion_images", None)
    if isinstance(em, dict):
        out: dict[str, str] = {}
        for k, v in em.items():
            ks = str(k or "").strip().lower()
            vs = str(v or "").strip()
            if ks and vs:
                out[ks] = vs
        return out or None
    return None


def _style_bond_images(style_obj) -> list[str] | None:
    bi = getattr(style_obj, "bond_images", None)
    if isinstance(bi, list):
        out = [str(x or "").strip() for x in bi if str(x or "").strip()]
        return out or None
    return None


def character_has_emotion_images(character_id: str, *, style_obj=None) -> bool:
    """True if the character has any emotion images configured.

    Checks per-style custom uploads first, then the static manifest.
    """
    if _style_emotions(style_obj):
        return True
    cid = (character_id or "").strip().lower()
    if not cid:
        return False
    images = CHARACTER_IMAGES.get(cid)
    return bool(images and images.emotions and any((v or "").strip() for v in images.emotions.values()))


def character_has_bond_images(character_id: str, *, style_obj=None) -> bool:
    if _style_bond_images(style_obj):
        return True
    cid = (character_id or "").strip().lower()
    images = CHARACTER_IMAGES.get(cid)
    return bool(images and images.bond_images)


def bond_image_url_for_level(character_id: str, bond_level: int, *, style_obj=None) -> str | None:
    """Return the single bond image URL for the given bond tier (1..5).

    This implements your rule: at any bond level, only **one** bond image is eligible.
    - 1 = Friend
    - 2 = Trusted
    - 3 = Close Companion
    - 4 = Devoted
    - 5 = Soulbound
    """
    # Prefer per-style bond images if present (custom pack characters)
    bond_list = _style_bond_images(style_obj)
    if bond_list:
        try:
            lvl = int(bond_level)
        except Exception:
            return None
        if lvl <= 0:
            return None
        idx = min(lvl, len(bond_list)) - 1
        if 0 <= idx < len(bond_list):
            return _as_url(bond_list[idx])
        return None

    cid = (character_id or "").strip().lower()
    images = CHARACTER_IMAGES.get(cid)
    if not images or not images.bond_images:
        return None
    try:
        lvl = int(bond_level)
    except Exception:
        return None
    if lvl <= 0:
        return None
    idx = min(lvl, len(images.bond_images)) - 1
    if 0 <= idx < len(images.bond_images):
        return _as_url(images.bond_images[idx])
    return None


def character_emotion_image_url(
    character_id: str,
    user_prompt: str,
    *,
    bond_level: int | None = None,
    bond_chance: float = 0.25,
    style_obj=None,
) -> str:
    """Return the best emotion (or bond) image URL for the given character.

    - If bond_level is provided and a corresponding bond image exists, it may override
      the normal emotion image based on bond_chance.
    - Otherwise predicts an emotion and returns the configured emotion filename.
    - If missing config, returns "" (caller should fall back to character's normal image).
    """
    cid = (character_id or "").strip().lower()

    # Prefer per-style custom images if present.
    custom_emotions = _style_emotions(style_obj) or {}
    custom_bonds = _style_bond_images(style_obj) or []

    images = CHARACTER_IMAGES.get(cid)
    manifest_emotions = (images.emotions if images else {}) or {}
    manifest_bonds = (images.bond_images if images else []) or []

    # If neither custom nor manifest is configured, bail out.
    if not custom_emotions and not manifest_emotions and not custom_bonds and not manifest_bonds:
        return ""

    # Bond override: ONLY the single image corresponding to the current bond tier.
    # bond_level: 1..5 (Friend..Soulbound)
    bond_list = custom_bonds or manifest_bonds
    if bond_level is not None and bond_list:
        try:
            lvl = max(0, int(bond_level))
        except Exception:
            lvl = 0
        if lvl > 0:
            idx = min(lvl, len(bond_list)) - 1
            if 0 <= idx < len(bond_list) and random.random() < float(bond_chance):
                return _as_url(bond_list[idx])

    emotion = predict_emotion(cid, user_prompt=user_prompt)
    emotions = custom_emotions or manifest_emotions
    return _resolve_emotion_filename(emotions, emotion)


def _resolve_emotion_filename(emotions: dict[str, str], emotion: str) -> str:
    """Look up an emotion image path with alias fallback, then convert to URL."""
    aliases = {
        "angry": ["mad"],
        "mad": ["angry"],
        "happy": ["excited"],
        "excited": ["happy"],
    }
    filename = emotions.get(emotion, "")
    if not filename:
        for alt in aliases.get(emotion, []):
            filename = emotions.get(alt, "")
            if filename:
                break
    if not filename:
        filename = emotions.get("neutral", "")
    return _as_url(filename)


def emotion_image_url_for_key(
    character_id: str,
    emotion_key: str,
    *,
    style_obj=None,
) -> str:
    """Resolve an emotion image URL from a *known* emotion key (no prediction).

    Used when the LLM itself has tagged the emotion via ``[EMOTION:xxx]``.
    Falls back through aliases then to neutral, same as the prediction path.
    """
    cid = (character_id or "").strip().lower()
    custom_emotions = _style_emotions(style_obj) or {}
    images = CHARACTER_IMAGES.get(cid)
    manifest_emotions = (images.emotions if images else {}) or {}
    if not custom_emotions and not manifest_emotions:
        return ""
    emotions = custom_emotions or manifest_emotions
    return _resolve_emotion_filename(emotions, emotion_key)
