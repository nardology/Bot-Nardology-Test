"""Spending dashboard helpers (no DB)."""
from __future__ import annotations

from utils.dashboard_queries import _day_list_last_n_days


def test_day_list_last_n_days_unique_and_length():
    ts = 1700000000
    days = _day_list_last_n_days(now_ts=ts, n=14)
    assert len(days) == 14
    assert len(set(days)) == 14
