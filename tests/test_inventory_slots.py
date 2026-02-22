"""Tests for inventory slot enforcement in add_style_to_inventory."""
from __future__ import annotations

from dataclasses import field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.character_store import CharacterState, compute_limits, _count_inventory_nonbase


# ---------------------------------------------------------------------------
# Pure-logic tests (no DB needed)
# ---------------------------------------------------------------------------

class TestComputeLimits:

    def test_free_slots(self):
        _rolls, slots = compute_limits(is_pro=False)
        assert slots == 3

    def test_pro_slots(self):
        _rolls, slots = compute_limits(is_pro=True)
        assert slots == 10

    def test_free_rolls(self):
        from utils.character_store import ROLLS_PER_DAY_FREE
        rolls, _ = compute_limits(is_pro=False)
        assert rolls == ROLLS_PER_DAY_FREE

    def test_pro_rolls(self):
        from utils.character_store import ROLLS_PER_DAY_PRO
        rolls, _ = compute_limits(is_pro=True)
        assert rolls == ROLLS_PER_DAY_PRO


class TestCountInventoryNonbase:

    def test_empty(self):
        assert _count_inventory_nonbase([]) == 0
        assert _count_inventory_nonbase(set()) == 0

    def test_custom_only(self):
        assert _count_inventory_nonbase(["alpha", "beta", "gamma"]) == 3

    def test_deduplication(self):
        assert _count_inventory_nonbase(["alpha", "alpha", "beta"]) == 2

    def test_case_insensitive(self):
        assert _count_inventory_nonbase(["Alpha", "ALPHA"]) == 1


# ---------------------------------------------------------------------------
# add_style_to_inventory integration-style tests (mocked DB/registry)
# ---------------------------------------------------------------------------

def _make_state(user_id: int = 100, owned: list[str] | None = None) -> CharacterState:
    return CharacterState(
        user_id=user_id,
        active_style_id="",
        points=0,
        roll_day="",
        roll_used=0,
        pity_mythic=0,
        pity_legendary=0,
        owned_custom=sorted(owned or []),
    )


class TestAddStyleToInventory:

    @pytest.mark.asyncio
    async def test_invalid_style_id_rejected(self):
        from utils.character_store import add_style_to_inventory
        ok, msg = await add_style_to_inventory(user_id=1, style_id="", is_pro=False)
        assert ok is False
        assert "Invalid" in msg

    @pytest.mark.asyncio
    async def test_fun_style_rejected(self):
        from utils.character_store import add_style_to_inventory
        ok, msg = await add_style_to_inventory(user_id=1, style_id="fun", is_pro=False)
        assert ok is False
        assert "Invalid" in msg

    @pytest.mark.asyncio
    async def test_unknown_character_rejected(self):
        from utils.character_store import add_style_to_inventory
        with patch("utils.character_store.get_style", return_value=None):
            with patch("utils.character_store.list_custom_packs", new_callable=AsyncMock, return_value=[]):
                ok, msg = await add_style_to_inventory(user_id=1, style_id="nonexistent", is_pro=False)
                assert ok is False
                assert "Unknown" in msg

    @pytest.mark.asyncio
    async def test_duplicate_rejected(self):
        from utils.character_store import add_style_to_inventory
        fake_style = MagicMock()
        with patch("utils.character_store.get_style", return_value=fake_style):
            with patch("utils.character_store.owns_style", new_callable=AsyncMock, return_value=True):
                ok, msg = await add_style_to_inventory(user_id=1, style_id="knight", is_pro=False)
                assert ok is False
                assert "already own" in msg.lower()

    @pytest.mark.asyncio
    async def test_full_inventory_rejected(self):
        from utils.character_store import add_style_to_inventory

        fake_style = MagicMock()
        state = _make_state(owned=["a", "b", "c", "fun"])

        with patch("utils.character_store.get_style", return_value=fake_style):
            with patch("utils.character_store.owns_style", new_callable=AsyncMock, return_value=False):
                with patch("utils.character_store.load_state", new_callable=AsyncMock, return_value=state):
                    with patch("utils.character_store.get_inventory_upgrades", new_callable=AsyncMock, return_value=0):
                        ok, msg = await add_style_to_inventory(user_id=1, style_id="new_char", is_pro=False)
                        assert ok is False
                        assert "full" in msg.lower()

    @pytest.mark.asyncio
    async def test_under_limit_accepted(self):
        from utils.character_store import add_style_to_inventory

        fake_style = MagicMock()
        state = _make_state(owned=["a", "fun"])

        with patch("utils.character_store.get_style", return_value=fake_style):
            with patch("utils.character_store.owns_style", new_callable=AsyncMock, return_value=False):
                with patch("utils.character_store.load_state", new_callable=AsyncMock, return_value=state):
                    with patch("utils.character_store.get_inventory_upgrades", new_callable=AsyncMock, return_value=0):
                        with patch("utils.character_store.append_owned_style", new_callable=AsyncMock):
                            ok, msg = await add_style_to_inventory(user_id=1, style_id="new_char", is_pro=False)
                            assert ok is True
                            assert "Added" in msg

    @pytest.mark.asyncio
    async def test_upgrades_extend_capacity(self):
        """With 2 upgrades (+10 slots), a free user gets 3+10=13 slots."""
        from utils.character_store import add_style_to_inventory

        fake_style = MagicMock()
        owned = [f"char_{i}" for i in range(3)] + ["fun"]
        state = _make_state(owned=owned)

        with patch("utils.character_store.get_style", return_value=fake_style):
            with patch("utils.character_store.owns_style", new_callable=AsyncMock, return_value=False):
                with patch("utils.character_store.load_state", new_callable=AsyncMock, return_value=state):
                    with patch("utils.character_store.get_inventory_upgrades", new_callable=AsyncMock, return_value=2):
                        with patch("utils.character_store.append_owned_style", new_callable=AsyncMock):
                            ok, msg = await add_style_to_inventory(user_id=1, style_id="new_char", is_pro=False)
                            assert ok is True

    @pytest.mark.asyncio
    async def test_pro_has_more_slots(self):
        """Pro has 10 base slots vs free's 3."""
        from utils.character_store import add_style_to_inventory

        fake_style = MagicMock()
        owned = [f"char_{i}" for i in range(3)] + ["fun"]
        state = _make_state(owned=owned)

        with patch("utils.character_store.get_style", return_value=fake_style):
            with patch("utils.character_store.owns_style", new_callable=AsyncMock, return_value=False):
                with patch("utils.character_store.load_state", new_callable=AsyncMock, return_value=state):
                    with patch("utils.character_store.get_inventory_upgrades", new_callable=AsyncMock, return_value=0):
                        with patch("utils.character_store.append_owned_style", new_callable=AsyncMock):
                            # Free at 3 non-base -> full
                            ok_free, _ = await add_style_to_inventory(user_id=1, style_id="new_char", is_pro=False)
                            assert ok_free is False

                            # Pro at 3 non-base -> still has room
                            ok_pro, _ = await add_style_to_inventory(user_id=1, style_id="new_char", is_pro=True)
                            assert ok_pro is True
