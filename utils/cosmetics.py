"""Cosmetic catalog and asset URLs for profile display (inspect, cosmetic-shop).

Assets live at: BotNardology-Assets/assets/cosmetics/
Files: tails.png, strong.png, cow.png, tam.png, joe.png, sans.png, walter.png
"""
from __future__ import annotations

from utils.character_emotion_manifest import _DEFAULT_CDN_BASE

# Cosmetics live in the GitHub repo (served via jsDelivr), not in the
# R2 bucket that ASSET_PUBLIC_BASE_URL may point to.  Always use the
# hardcoded repo CDN so images resolve correctly.
COSMETICS_BASE = f"{_DEFAULT_CDN_BASE}/cosmetics"

# Catalog: number 1–7 → (cosmetic_id, display_name). cosmetic_id = filename without .png
COSMETIC_CATALOG: list[tuple[int, str, str]] = [
    (1, "tails", "Tails"),
    (2, "strong", "Strong"),
    (3, "cow", "Cow"),
    (4, "tam", "Tam"),
    (5, "joe", "Joe"),
    (6, "sans", "Sans"),
    (7, "walter", "Walter"),
]

COSMETIC_PRICE = 500

# Map number -> cosmetic_id for shop buttons
NUM_TO_COSMETIC_ID: dict[int, str] = {num: cid for num, cid, _ in COSMETIC_CATALOG}

# All valid cosmetic IDs (for validation)
COSMETIC_IDS: set[str] = set(NUM_TO_COSMETIC_ID.values())


def cosmetic_image_url(cosmetic_id: str) -> str | None:
    """Return the image URL for a cosmetic (same path as assets: .../cosmetics/{id}.png)."""
    if not cosmetic_id or cosmetic_id not in COSMETIC_IDS:
        return None
    base = (COSMETICS_BASE or "").rstrip("/")
    return f"{base}/{cosmetic_id}.png" if base else None


def default_cosmetic_image_url(cosmetic_id: str) -> str | None:
    """Return the default CDN image URL for a cosmetic (same as primary now)."""
    return cosmetic_image_url(cosmetic_id)


def cosmetic_display_name(cosmetic_id: str) -> str:
    """Return display name for a cosmetic id."""
    for _num, cid, name in COSMETIC_CATALOG:
        if cid == cosmetic_id:
            return name
    return (cosmetic_id or "?").replace("_", " ").title()
