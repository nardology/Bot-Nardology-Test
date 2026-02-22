"""Tests for analytics key formatting and day logic (no Redis/DB)."""
from __future__ import annotations

import time

import pytest

from utils.analytics import (
    utc_day_str,
    _k_count,
    _k_active,
    _k_dirty,
    GLOBAL_GUILD_ID,
    METRIC_DAILY_ROLLS,
    METRIC_DAILY_TALK_CALLS,
)


def test_utc_day_str():
    # Without arg uses current time
    day = utc_day_str()
    assert len(day) == 8
    assert day.isdigit()
    # With timestamp
    ts = 1700000000  # 2023-11-15 or so
    d = utc_day_str(ts)
    assert d == time.strftime("%Y%m%d", time.gmtime(ts))


def test_k_count():
    assert _k_count("20260209", 0, METRIC_DAILY_ROLLS) == "analytics:count:20260209:0:daily_rolls"
    assert _k_count("20260209", 12345, METRIC_DAILY_TALK_CALLS) == "analytics:count:20260209:12345:daily_talk_calls"


def test_k_active():
    assert _k_active("20260209", 0) == "analytics:active:20260209:0"


def test_k_dirty():
    assert _k_dirty("20260209") == "analytics:dirty:20260209"


def test_global_guild_id():
    assert GLOBAL_GUILD_ID == 0
