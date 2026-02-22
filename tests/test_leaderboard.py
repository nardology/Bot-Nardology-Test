"""Tests for leaderboard key/format (no Redis)."""
from __future__ import annotations

import pytest

from utils.leaderboard import (
    GLOBAL_GUILD_ID,
    CATEGORY_POINTS,
    CATEGORY_ROLLS,
    CATEGORY_ACTIVITY,
    _leaderboard_key,
    _member_key,
    _parse_member,
)


def test_leaderboard_categories():
    assert CATEGORY_POINTS == "points"
    assert CATEGORY_ROLLS == "rolls"
    assert CATEGORY_ACTIVITY == "activity"


def test_leaderboard_key_global():
    assert _leaderboard_key(CATEGORY_POINTS, GLOBAL_GUILD_ID, "alltime") == "leaderboard:global:points:alltime"


def test_leaderboard_key_guild():
    assert _leaderboard_key(CATEGORY_ROLLS, 12345, "daily") == "leaderboard:guild:12345:rolls:daily"


def test_member_key_global():
    assert _member_key(GLOBAL_GUILD_ID, 999) == "999"


def test_member_key_guild():
    assert _member_key(12345, 999) == "12345:999"


def test_parse_member():
    g, u = _parse_member("12345:999")
    assert g == 12345 and u == 999
    g, u = _parse_member("999")
    assert g == 0 and u == 999
