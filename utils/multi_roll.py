"""Multi-roll logic for 5-pull and 10-pull shop purchases.

Rolls N unique characters (no duplicates within a batch). Pity applies per roll.
Does NOT consume daily roll credits.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from utils.character_registry import list_rollable, roll_style
from utils.character_store import load_state, set_pity
from utils.packs_store import get_enabled_pack_ids
from utils.points_store import get_booster_stack

if TYPE_CHECKING:
    from utils.character_registry import StyleDef


async def do_multi_roll(
    guild_id: int,
    user_id: int,
    count: int,
) -> tuple[list["StyleDef"], str | None]:
    """Roll N unique characters.

    Returns (list of StyleDef, error_message). If error_message is set, list is empty.
    No duplicates within the batch. Pity is updated. Does NOT consume daily rolls.
    """
    if count not in (5, 10):
        return [], f"Invalid count: {count}. Use 5 or 10."

    # Merge server-only characters into the runtime registry
    try:
        from utils.server_chars_store import list_server_chars, to_pack_payload
        from utils.character_registry import merge_pack_payload

        server_chars = await list_server_chars(guild_id)
        if server_chars:
            merge_pack_payload(to_pack_payload(guild_id, server_chars))
    except Exception:
        pass

    enabled_packs = await get_enabled_pack_ids(guild_id)
    if not list_rollable(pack_ids=enabled_packs):
        return [], (
            "No rollable characters are available in this server's enabled packs. "
            "Ask the server owner/admin to enable a pack via **/packs browse** or **/packs enable**."
        )

    state = await load_state(user_id=user_id)
    pity_legendary = int(getattr(state, "pity_legendary", 0) or 0)
    pity_mythic = int(getattr(state, "pity_mythic", 0) or 0)

    legendary_mult = 1.0
    mythic_mult = 1.0
    try:
        stacks, remaining_s = await get_booster_stack(guild_id=guild_id, user_id=user_id, kind="lucky")
        if stacks > 0 and remaining_s > 0:
            mult = float(1.5 ** int(stacks))
            legendary_mult = mult
            mythic_mult = mult
    except Exception:
        pass

    rng = random.Random()
    seen_in_batch: set[str] = set()
    results: list["StyleDef"] = []
    max_attempts = count * 20  # safety limit
    attempts = 0

    while len(results) < count and attempts < max_attempts:
        attempts += 1
        rolled = roll_style(
            pity_legendary=pity_legendary,
            pity_mythic=pity_mythic,
            rng=rng,
            legendary_mult=legendary_mult,
            mythic_mult=mythic_mult,
            pack_ids=enabled_packs,
        )
        sid = (getattr(rolled, "style_id", "") or "").strip().lower()
        if sid and sid not in seen_in_batch:
            seen_in_batch.add(sid)
            results.append(rolled)

        # Update pity for next roll
        r_pity = str(getattr(rolled, "rarity", "") or "").strip().lower()
        if r_pity == "mythic":
            pity_legendary = 0
            pity_mythic = 0
        elif r_pity == "legendary":
            pity_legendary = 0
            pity_mythic = min(999, pity_mythic + 1)
        else:
            pity_legendary = min(99, pity_legendary + 1)
            pity_mythic = min(999, pity_mythic + 1)

    try:
        await set_pity(user_id=user_id, pity_mythic=pity_mythic, pity_legendary=pity_legendary)
    except Exception:
        pass

    if len(results) < count:
        return results, (
            f"Only rolled {len(results)} unique characters (pack may have fewer than {count}). "
            "Added what was possible."
        )

    return results, None
