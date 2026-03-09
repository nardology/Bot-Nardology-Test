"""Verify all registered cogs can be imported (no Discord connection)."""
from __future__ import annotations

import pytest


# Extensions list from bot.py (single source of truth would be to read from bot, but we avoid starting bot)
EXTENSIONS = [
    "commands.slash.analytics",
    "commands.slash.basic",
    "commands.slash.start",
    "commands.slash.settings",
    "commands.slash.help",
    "commands.slash.talk",
    "commands.slash.voice",
    "commands.slash.feedback",
    "commands.slash.limits",
    "commands.slash.usage",
    "commands.slash.bond",
    "commands.slash.penalty",
    "commands.slash.character",
    "commands.slash.points",
    "commands.slash.packs",
    "commands.slash.owner",
    "commands.slash.report",
    "commands.slash.z_server",
    "commands.slash.appeal",
    "commands.slash.verification_appeal",
    "commands.slash.leaderboard",
    "commands.slash.inspect",
    "commands.slash.cosmetic",
    "commands.slash.legal",
    "commands.slash.premium",
    "commands.slash.privacy",
    "commands.slash.tutorial",
]


@pytest.mark.parametrize("ext", EXTENSIONS)
def test_extension_imports(ext: str):
    """Each extension module must import without error."""
    import importlib
    mod = importlib.import_module(ext)
    assert mod is not None
