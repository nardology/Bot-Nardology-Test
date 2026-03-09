from __future__ import annotations

import config
import discord

def is_bot_owner(user_or_id) -> bool:
    """Return True if user is in BOT_OWNER_IDS.

    Accepts a discord User/Member or a raw int ID.
    """
    try:
        user_id = int(getattr(user_or_id, "id", user_or_id))
    except Exception:
        return False
    return bool(config.BOT_OWNER_IDS) and (user_id in config.BOT_OWNER_IDS)
