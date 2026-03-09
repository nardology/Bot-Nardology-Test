"""Tests for character registry merge logic (utils/character_registry.py)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# merge_pack_payload
# ---------------------------------------------------------------------------

class TestMergePackPayload:

    def test_valid_pack_adds_characters(self):
        from utils.character_registry import merge_pack_payload, STYLE_DEFS

        payload = {
            "type": "pack",
            "pack_id": "test_pack",
            "characters": [
                {"id": "test_merge_a", "display_name": "Merge A", "rarity": "common", "prompt": "hello"},
                {"id": "test_merge_b", "display_name": "Merge B", "rarity": "rare", "prompt": "world"},
            ],
        }

        added = merge_pack_payload(payload)
        assert added == 2
        assert "test_merge_a" in STYLE_DEFS
        assert "test_merge_b" in STYLE_DEFS
        assert STYLE_DEFS["test_merge_a"].pack_id == "test_pack"

        # Cleanup
        STYLE_DEFS.pop("test_merge_a", None)
        STYLE_DEFS.pop("test_merge_b", None)

    def test_non_pack_dict_returns_zero(self):
        from utils.character_registry import merge_pack_payload

        assert merge_pack_payload({"type": "character", "id": "foo"}) == 0
        assert merge_pack_payload({}) == 0

    def test_non_dict_returns_zero(self):
        from utils.character_registry import merge_pack_payload

        assert merge_pack_payload("not a dict") == 0  # type: ignore[arg-type]
        assert merge_pack_payload(123) == 0  # type: ignore[arg-type]
        assert merge_pack_payload(None) == 0  # type: ignore[arg-type]

    def test_malformed_characters_returns_zero_no_corruption(self):
        from utils.character_registry import merge_pack_payload, STYLE_DEFS

        before = set(STYLE_DEFS.keys())

        payload = {
            "type": "pack",
            "pack_id": "bad_pack",
            "characters": "not a list",
        }
        assert merge_pack_payload(payload) == 0
        assert set(STYLE_DEFS.keys()) == before

    def test_skips_non_dict_entries(self):
        from utils.character_registry import merge_pack_payload, STYLE_DEFS

        payload = {
            "type": "pack",
            "pack_id": "mixed_pack",
            "characters": [
                "just a string",
                42,
                {"id": "test_merge_valid", "display_name": "Valid", "rarity": "common", "prompt": "ok"},
            ],
        }

        added = merge_pack_payload(payload)
        assert added == 1
        assert "test_merge_valid" in STYLE_DEFS

        STYLE_DEFS.pop("test_merge_valid", None)

    def test_override_existing_character(self):
        from utils.character_registry import merge_pack_payload, STYLE_DEFS

        payload = {
            "type": "pack",
            "pack_id": "override_pack",
            "characters": [
                {"id": "test_merge_override", "display_name": "V1", "rarity": "common", "prompt": "v1"},
            ],
        }
        merge_pack_payload(payload)
        assert STYLE_DEFS["test_merge_override"].display_name == "V1"

        payload["characters"] = [
            {"id": "test_merge_override", "display_name": "V2", "rarity": "common", "prompt": "v2"},
        ]
        merge_pack_payload(payload)
        assert STYLE_DEFS["test_merge_override"].display_name == "V2"

        STYLE_DEFS.pop("test_merge_override", None)

    def test_default_pack_id_is_core(self):
        from utils.character_registry import merge_pack_payload, STYLE_DEFS

        payload = {
            "type": "pack",
            "characters": [
                {"id": "test_merge_default_pack", "display_name": "D", "rarity": "common", "prompt": "d"},
            ],
        }
        merge_pack_payload(payload)
        assert STYLE_DEFS["test_merge_default_pack"].pack_id == "core"

        STYLE_DEFS.pop("test_merge_default_pack", None)


# ---------------------------------------------------------------------------
# load_external_characters
# ---------------------------------------------------------------------------

class TestLoadExternalCharacters:

    def test_single_character_file(self, tmp_path):
        from utils.character_registry import load_external_characters

        char_file = tmp_path / "hero.json"
        char_file.write_text(json.dumps({
            "id": "test_ext_hero",
            "display_name": "Hero",
            "rarity": "common",
            "prompt": "brave",
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_ext_hero" in result
        assert result["test_ext_hero"].display_name == "Hero"

    def test_pack_file(self, tmp_path):
        from utils.character_registry import load_external_characters

        pack_file = tmp_path / "my_pack.json"
        pack_file.write_text(json.dumps({
            "type": "pack",
            "pack_id": "ext_pack",
            "characters": [
                {"id": "test_ext_a", "display_name": "A", "rarity": "common", "prompt": "a"},
                {"id": "test_ext_b", "display_name": "B", "rarity": "rare", "prompt": "b"},
            ],
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_ext_a" in result
        assert "test_ext_b" in result
        assert result["test_ext_a"].pack_id == "ext_pack"

    def test_array_file(self, tmp_path):
        from utils.character_registry import load_external_characters

        arr_file = tmp_path / "batch.json"
        arr_file.write_text(json.dumps([
            {"id": "test_ext_c", "display_name": "C", "rarity": "common", "prompt": "c"},
            {"id": "test_ext_d", "display_name": "D", "rarity": "rare", "prompt": "d"},
        ]))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_ext_c" in result
        assert "test_ext_d" in result

    def test_nonexistent_directory_returns_empty(self):
        from utils.character_registry import load_external_characters

        result = load_external_characters(directory="/nonexistent/path/xyz")
        assert result == {}

    def test_empty_directory(self, tmp_path):
        from utils.character_registry import load_external_characters

        result = load_external_characters(directory=str(tmp_path))
        assert result == {}

    def test_malformed_file_skipped(self, tmp_path):
        from utils.character_registry import load_external_characters

        bad_file = tmp_path / "broken.json"
        bad_file.write_text("{this is not valid json")

        good_file = tmp_path / "good.json"
        good_file.write_text(json.dumps({
            "id": "test_ext_good",
            "display_name": "Good",
            "rarity": "common",
            "prompt": "good",
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_ext_good" in result
        assert len(result) == 1

    def test_recursive_subdirectories(self, tmp_path):
        from utils.character_registry import load_external_characters

        sub = tmp_path / "subdir"
        sub.mkdir()

        char_file = sub / "nested.json"
        char_file.write_text(json.dumps({
            "id": "test_ext_nested",
            "display_name": "Nested",
            "rarity": "common",
            "prompt": "deep",
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_ext_nested" in result


# ---------------------------------------------------------------------------
# Shop item capture from JSON
# ---------------------------------------------------------------------------

class TestShopItemCapture:

    def test_shop_item_captured_from_character_json(self, tmp_path):
        from utils.character_registry import load_external_characters, _SHOP_ITEM_DEFS

        char_file = tmp_path / "shop_test.json"
        char_file.write_text(json.dumps({
            "id": "test_shop_char",
            "display_name": "Shop Test",
            "rarity": "rare",
            "prompt": "A test shop character",
            "shop_item": {
                "item_id": "test_shop_char",
                "kind": "character_grant",
                "cost": 500,
                "title": "Test Shop Char",
                "active": True,
            },
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_shop_char" in result
        assert "test_shop_char" in _SHOP_ITEM_DEFS
        assert _SHOP_ITEM_DEFS["test_shop_char"]["cost"] == 500
        assert _SHOP_ITEM_DEFS["test_shop_char"]["active"] is True

        del _SHOP_ITEM_DEFS["test_shop_char"]

    def test_character_without_shop_item_not_captured(self, tmp_path):
        from utils.character_registry import load_external_characters, _SHOP_ITEM_DEFS

        char_file = tmp_path / "no_shop.json"
        char_file.write_text(json.dumps({
            "id": "test_no_shop",
            "display_name": "No Shop",
            "rarity": "common",
            "prompt": "Not a shop character",
        }))

        result = load_external_characters(directory=str(tmp_path))
        assert "test_no_shop" in result
        assert "test_no_shop" not in _SHOP_ITEM_DEFS

    def test_cupid_loads_from_real_shop_directory(self):
        """Verify the renamed valentine_cupid.json file loads correctly."""
        from utils.character_registry import get_shop_item_defs, STYLE_DEFS

        assert "valentine_cupid" in STYLE_DEFS, "Cupid should be loaded in STYLE_DEFS"
        shop_defs = get_shop_item_defs()
        assert "valentine_cupid" in shop_defs, "Cupid should appear in shop item defs"
        assert shop_defs["valentine_cupid"]["active"] is True
