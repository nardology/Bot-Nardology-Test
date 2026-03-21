"""Weekly topic keyword matching (anti-trivial gates)."""
from __future__ import annotations

from utils.character_weekly_topics import WeeklyTopicsBundle, match_weekly_topic_indices


def _bundle() -> WeeklyTopicsBundle:
    return WeeklyTopicsBundle(
        week_id="2026-W11",
        topics=[
            {"title": "Rainy day comfort", "description": "Tea, blankets, quiet thoughts"},
            {"title": "Second topic", "description": "x"},
            {"title": "Third topic", "description": "y"},
        ],
        keywords=[
            ["rain", "blanket", "cozy", "tea", "quiet"],
            ["alpha", "beta"],
            ["gamma", "delta"],
        ],
        hints=["hint0", "hint1", "hint2"],
        topic_version=1,
        claimed_mask=0,
    )


def test_match_rejects_short_message():
    b = _bundle()
    assert match_weekly_topic_indices("short", bundle=b) == []


def test_match_finds_overlap():
    b = _bundle()
    text = (
        "I have been thinking about rainy day comfort and how I love a warm blanket "
        "with tea when it pours outside, quiet moments really help me unwind today."
    )
    idx = match_weekly_topic_indices(text, bundle=b)
    assert 0 in idx


def test_match_rejects_trivial_title_only():
    b = _bundle()
    low = "rainy day comfort"
    text = "rainy day comfort " + "word " * 20  # still might pass word count
    # exact title as whole message is trivial
    trivial = "rainy day comfort"
    assert match_weekly_topic_indices(trivial, bundle=b) == []
