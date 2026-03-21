"""Delivery mode picker covers refusal path."""
from __future__ import annotations

import random

from utils.talk_prompts import format_delivery_mode_instruction, pick_delivery_mode


def test_pick_delivery_mode_all_modes_exist():
    seen = {pick_delivery_mode(random.Random(i)) for i in range(3000)}
    assert "micro" in seen
    assert "refusal" in seen


def test_format_delivery_refusal_mentions_stubborn():
    s = format_delivery_mode_instruction("refusal")
    assert "stubborn" in s.lower() or "not in the mood" in s.lower()
