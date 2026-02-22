"""Shared helpers for limited (shop) character creation.

Used by limited_character_add (add to pack) and limited_character_create_direct (direct buy).
"""
from __future__ import annotations

from typing import Any

from utils.packs_store import normalize_pack_id, normalize_style_id
from utils.character_registry import RARITY_ORDER

def _parse_bond_cap(value: str | None) -> int | None:
    v = (value or "").strip().lower()
    if not v or v in {"max", "none"}:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


async def build_limited_character_payload(
    *,
    character_id: str,
    display_name: str,
    description: str,
    prompt: str,
    rarity: str,
    pack_id: str,
    image: Any = None,
    image_url: str | None = None,
    traits: str | None = None,
    max_bond_cap: str | None = None,
    rollable: bool = True,
    # Emotion images (optional)
    emotion_neutral: Any = None,
    emotion_happy: Any = None,
    emotion_sad: Any = None,
    emotion_mad: Any = None,
    emotion_confused: Any = None,
    emotion_excited: Any = None,
    emotion_affectionate: Any = None,
    # Bond images (optional)
    bond1: Any = None,
    bond2: Any = None,
    bond3: Any = None,
    bond4: Any = None,
    bond5: Any = None,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Build a character payload dict for limited pack/direct shop characters.

    Returns (char_payload, img_ref, error_message).
    If error_message is not None, char_payload may be incomplete and the caller should abort.
    """
    from utils.media_assets import save_attachment_image

    cid = normalize_style_id(character_id)
    pid = normalize_pack_id(pack_id)
    rar = (rarity or "rare").strip().lower()

    if not cid:
        return {}, None, "Invalid character_id."
    if rar not in set(RARITY_ORDER):
        return {}, None, "Invalid rarity."

    tags = [t.strip() for t in (traits or "").split(",") if t.strip()]
    img_ref: str | None = None

    async def _save(att: Any, rel_dir: str, basename: str) -> tuple[str | None, str | None]:
        """Save an attachment image. Returns (ref, error)."""
        ok_img, msg_img, rel = await save_attachment_image(
            attachment=att,
            rel_dir=rel_dir,
            basename=basename,
            max_bytes=20 * 1024 * 1024,
            upscale_min_px=1024,
        )
        if not ok_img:
            return None, msg_img or "Image save failed."
        if not rel:
            return None, None
        return (rel if rel.startswith("http") else f"asset:{rel}"), None

    if image is not None:
        ref, err = await _save(image, f"packs/{pid}", cid)
        if err:
            return {}, None, err
        img_ref = ref
    elif isinstance(image_url, str) and image_url.strip().lower().startswith("http"):
        img_ref = image_url.strip()

    # Emotion images
    emotion_images: dict[str, str] = {}
    emotion_inputs: dict[str, Any] = {
        "neutral": emotion_neutral,
        "happy": emotion_happy,
        "sad": emotion_sad,
        "mad": emotion_mad,
        "confused": emotion_confused,
        "excited": emotion_excited,
        "affectionate": emotion_affectionate,
    }
    for key, att in emotion_inputs.items():
        if att is None:
            continue
        ref, err = await _save(att, f"packs/{pid}/{cid}/emotions", key)
        if err:
            return {}, None, err
        if ref:
            emotion_images[key] = ref

    # Bond images
    bond_images: list[str] = []
    bond_atts = [bond1, bond2, bond3, bond4, bond5]
    for idx, att in enumerate(bond_atts, start=1):
        if att is None:
            continue
        ref, err = await _save(att, f"packs/{pid}/{cid}/bonds", f"bond{idx}")
        if err:
            return {}, None, err
        if ref:
            while len(bond_images) < idx:
                bond_images.append("")
            bond_images[idx - 1] = ref
    # Drop empty placeholders
    bond_images = [x for x in bond_images if x]

    max_bond = _parse_bond_cap(max_bond_cap)
    char_payload: dict[str, Any] = {
        "id": cid,
        "display_name": str(display_name or cid)[:64],
        "rarity": rar,
        "description": str(description or "").strip()[:400],
        "prompt": str(prompt or "").strip()[:6000],
        "image_url": img_ref,
        "emotion_images": emotion_images or None,
        "bond_images": bond_images or None,
        "tags": tags,
        "max_bond_level": max_bond,
        "rollable": bool(rollable),
    }
    return char_payload, img_ref, None
