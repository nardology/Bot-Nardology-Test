from __future__ import annotations

"""Character emotion + bond image manifest.

Images are organized in the BotNardology-Assets repo:

    assets/
        style_images/{character}.png       — main portrait
        emotions/{character}/{emotion}.ext — per-character emotion images
        bonds/{character}/tier{1-5}.ext    — per-character bond tier images
        ui/                                — roll animations, KAI mascot, sounds
        cosmetics/                         — profile cosmetic images

Supported emotion keys (7):
    affectionate, confused, excited, happy, mad, neutral, sad

Bond tiers (5):
    tier1 (Friend), tier2 (Trusted), tier3 (Close Companion),
    tier4 (Devoted), tier5 (Soulbound)
"""

from dataclasses import dataclass
from typing import Dict, List
import os

# Root CDN base for the entire assets repo.
_DEFAULT_CDN_BASE = "https://cdn.jsdelivr.net/gh/nardology/BotNardology-Assets@main/assets"


def _resolve_cdn_base() -> str:
    base = (os.getenv("ASSET_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        if base.endswith("/assets"):
            return base
        return base
    return _DEFAULT_CDN_BASE


ASSETS_CDN_BASE = _resolve_cdn_base()

# Sub-path bases derived from CDN root.
ASSETS_UI_BASE = f"{ASSETS_CDN_BASE}/ui"
ASSETS_EMOTIONS_BASE = f"{ASSETS_CDN_BASE}/emotions"
ASSETS_BONDS_BASE = f"{ASSETS_CDN_BASE}/bonds"

# Roll animations always use jsDelivr directly (not R2) so they load reliably.
ROLL_ANIMATION_UI_BASE = f"{_DEFAULT_CDN_BASE}/ui"


@dataclass(frozen=True)
class CharacterImageSet:
    emotions: Dict[str, str]
    bond_images: List[str]


# ---------------------------------------------------------------------------
# Character image manifest
#
# Paths are relative to ASSETS_CDN_BASE.
# Example: "emotions/dog/happy.jpg" resolves to
#   https://cdn.jsdelivr.net/gh/nardology/BotNardology-Assets@main/assets/emotions/dog/happy.jpg
# ---------------------------------------------------------------------------

CHARACTER_IMAGES: Dict[str, CharacterImageSet] = {
    "dog": CharacterImageSet(
        emotions={
            "affectionate": "emotions/dog/affectionate.avif",
            "confused": "emotions/dog/confused.png",
            "excited": "emotions/dog/excited.jpg",
            "happy": "emotions/dog/happy.jpg",
            "mad": "emotions/dog/mad.png",
            "neutral": "emotions/dog/neutral.webp",
            "sad": "emotions/dog/sad.jpg",
        },
        bond_images=[
            "bonds/dog/tier1.png",
            "bonds/dog/tier2.jpg",
            "bonds/dog/tier3.jpg",
            "bonds/dog/tier4.jpg",
            "bonds/dog/tier5.jpg",
        ],
    ),

    "fun": CharacterImageSet(emotions={}, bond_images=[]),
    "serious": CharacterImageSet(emotions={}, bond_images=[]),

    "pirate": CharacterImageSet(
        emotions={
            "affectionate": "emotions/pirate/affectionate.jpg",
            "confused": "emotions/pirate/confused.webp",
            "excited": "emotions/pirate/excited.jpg",
            "happy": "emotions/pirate/happy.jpg",
            "mad": "emotions/pirate/mad.jpg",
            "neutral": "emotions/pirate/neutral.webp",
            "sad": "emotions/pirate/sad.jpg",
        },
        bond_images=[
            "bonds/pirate/tier1.gif",
            "bonds/pirate/tier2.jpg",
            "bonds/pirate/tier3.jpg",
            "bonds/pirate/tier4.jpg",
            "bonds/pirate/tier5.jpg",
        ],
    ),

    "robot": CharacterImageSet(
        emotions={
            "affectionate": "emotions/robot/affectionate.gif",
            "confused": "emotions/robot/confused.png",
            "excited": "emotions/robot/excited.png",
            "happy": "emotions/robot/happy.png",
            "mad": "emotions/robot/mad.png",
            "neutral": "emotions/robot/neutral.png",
            "sad": "emotions/robot/sad.png",
        },
        bond_images=[
            "bonds/robot/tier1.png",
            "bonds/robot/tier2.png",
            "bonds/robot/tier3.png",
            "bonds/robot/tier4.png",
            "bonds/robot/tier5.png",
        ],
    ),

    "nardology": CharacterImageSet(
        emotions={
            "affectionate": "emotions/nardology/affectionate.png",
            "confused": "emotions/nardology/confused.png",
            "excited": "emotions/nardology/excited.png",
            "happy": "emotions/nardology/happy.png",
            "mad": "emotions/nardology/mad.png",
            "neutral": "emotions/nardology/neutral.png",
            "sad": "emotions/nardology/sad.png",
        },
        bond_images=[
            "bonds/nardology/tier1.gif",
            "bonds/nardology/tier2.gif",
            "bonds/nardology/tier3.gif",
            "bonds/nardology/tier4.gif",
            "bonds/nardology/tier5.gif",
        ],
    ),

    "peasant": CharacterImageSet(
        emotions={
            "affectionate": "emotions/peasant/affectionate.png",
            "confused": "emotions/peasant/confused.png",
            "excited": "emotions/peasant/excited.png",
            "happy": "emotions/peasant/happy.png",
            "mad": "emotions/peasant/mad.png",
            "neutral": "emotions/peasant/neutral.jpg",
            "sad": "emotions/peasant/sad.png",
        },
        bond_images=[
            "bonds/peasant/tier1.png",
            "bonds/peasant/tier2.png",
            "bonds/peasant/tier3.png",
            "bonds/peasant/tier4.png",
            "bonds/peasant/tier5.png",
        ],
    ),

    "cat": CharacterImageSet(
        emotions={
            "affectionate": "emotions/cat/affectionate.jpg",
            "confused": "emotions/cat/confused.jpg",
            "excited": "emotions/cat/excited.jpg",
            "happy": "emotions/cat/happy.jpg",
            "mad": "emotions/cat/mad.png",
            "neutral": "emotions/cat/neutral.jpg",
            "sad": "emotions/cat/sad.jpg",
        },
        bond_images=[
            "bonds/cat/tier1.jpg",
            "bonds/cat/tier2.jpg",
            "bonds/cat/tier3.webp",
            "bonds/cat/tier4.jpg",
            "bonds/cat/tier5.jpg",
        ],
    ),

    "common_man": CharacterImageSet(
        emotions={
            "affectionate": "emotions/common_man/affectionate.png",
            "confused": "emotions/common_man/confused.png",
            "excited": "emotions/common_man/excited.png",
            "happy": "emotions/common_man/happy.png",
            "mad": "emotions/common_man/mad.png",
            "neutral": "emotions/common_man/neutral.png",
            "sad": "emotions/common_man/sad.png",
        },
        bond_images=[
            "bonds/common_man/tier1.png",
            "bonds/common_man/tier2.png",
            "bonds/common_man/tier3.png",
            "bonds/common_man/tier4.png",
            "bonds/common_man/tier5.png",
        ],
    ),

    "knight": CharacterImageSet(
        emotions={
            "affectionate": "emotions/knight/affectionate.png",
            "confused": "emotions/knight/confused.png",
            "excited": "emotions/knight/excited.png",
            "happy": "emotions/knight/happy.png",
            "mad": "emotions/knight/mad.png",
            "neutral": "emotions/knight/neutral.png",
            "sad": "emotions/knight/sad.png",
        },
        bond_images=[
            "bonds/knight/tier1.png",
            "bonds/knight/tier2.png",
            "bonds/knight/tier3.png",
            "bonds/knight/tier4.png",
            "bonds/knight/tier5.png",
        ],
    ),

    "samurai": CharacterImageSet(
        emotions={
            "affectionate": "emotions/samurai/affectionate.png",
            "confused": "emotions/samurai/confused.png",
            "excited": "emotions/samurai/excited.png",
            "happy": "emotions/samurai/happy.png",
            "mad": "emotions/samurai/mad.png",
            "neutral": "emotions/samurai/neutral.png",
            "sad": "emotions/samurai/sad.png",
        },
        bond_images=[
            "bonds/samurai/tier1.png",
            "bonds/samurai/tier2.png",
            "bonds/samurai/tier3.png",
            "bonds/samurai/tier4.png",
            "bonds/samurai/tier5.png",
        ],
    ),

    "millionaire_ceo": CharacterImageSet(
        emotions={
            "affectionate": "emotions/millionaire_ceo/affectionate.png",
            "confused": "emotions/millionaire_ceo/confused.png",
            "excited": "emotions/millionaire_ceo/excited.png",
            "happy": "emotions/millionaire_ceo/happy.png",
            "mad": "emotions/millionaire_ceo/mad.png",
            "neutral": "emotions/millionaire_ceo/neutral.png",
            "sad": "emotions/millionaire_ceo/sad.png",
        },
        bond_images=[
            "bonds/millionaire_ceo/tier1.png",
            "bonds/millionaire_ceo/tier2.png",
            "bonds/millionaire_ceo/tier3.png",
            "bonds/millionaire_ceo/tier4.png",
            "bonds/millionaire_ceo/tier5.png",
        ],
    ),

    "dude": CharacterImageSet(
        emotions={
            "affectionate": "emotions/dude/affectionate.png",
            "confused": "emotions/dude/confused.png",
            "excited": "emotions/dude/excited.png",
            "happy": "emotions/dude/happy.png",
            "mad": "emotions/dude/mad.png",
            "neutral": "emotions/dude/neutral.png",
            "sad": "emotions/dude/sad.png",
        },
        bond_images=[
            "bonds/dude/tier1.png",
            "bonds/dude/tier2.png",
            "bonds/dude/tier3.png",
            "bonds/dude/tier4.png",
            "bonds/dude/tier5.png",
        ],
    ),

    "billionaire_ceo": CharacterImageSet(
        emotions={
            "affectionate": "emotions/billionaire_ceo/affectionate.jpg",
            "confused": "emotions/billionaire_ceo/confused.jpg",
            "excited": "emotions/billionaire_ceo/excited.jpg",
            "happy": "emotions/billionaire_ceo/happy.jpg",
            "mad": "emotions/billionaire_ceo/mad.png",
            "neutral": "emotions/billionaire_ceo/neutral.jpg",
            "sad": "emotions/billionaire_ceo/sad.jpg",
        },
        bond_images=[
            "bonds/billionaire_ceo/tier1.jpg",
            "bonds/billionaire_ceo/tier2.webp",
            "bonds/billionaire_ceo/tier3.jpg",
            "bonds/billionaire_ceo/tier4.webp",
            "bonds/billionaire_ceo/tier5.jpg",
        ],
    ),

    "dragon": CharacterImageSet(
        emotions={
            "affectionate": "emotions/dragon/affectionate.png",
            "confused": "emotions/dragon/confused.png",
            "excited": "emotions/dragon/excited.png",
            "happy": "emotions/dragon/happy.png",
            "mad": "emotions/dragon/mad.png",
            "neutral": "emotions/dragon/neutral.png",
            "sad": "emotions/dragon/sad.png",
        },
        bond_images=[
            "bonds/dragon/tier1.png",
            "bonds/dragon/tier2.png",
            "bonds/dragon/tier3.png",
            "bonds/dragon/tier4.png",
            "bonds/dragon/tier5.png",
        ],
    ),
}
