from __future__ import annotations

# All styles your bot supports
ALL_STYLES = ["fun", "serious", "pirate", "professional", "jojo"]

# Tier gating
FREE_STYLES = {"fun", "serious"}
PRO_STYLES = set(ALL_STYLES)  # everything, or explicitly list if you prefer


def normalize_style(style: str) -> str:
    return (style or "").lower().strip()
