from __future__ import annotations

from typing import Any

from utils.redis_kv import hget_json, hgetall_json, hset_json, kv_del, sadd, srem, smembers_str

# Redis keyspace:
# - guild settings stored as hash of JSON values:
#   key: guild:{guild_id}:settings  (fields: setting_key -> json)
# - "list" settings stored as redis set of strings:
#   key: guild:{guild_id}:list:{key}


def _settings_key(guild_id: int) -> str:
    return f"guild:{int(guild_id)}:settings"


def _list_key(guild_id: int, key: str) -> str:
    return f"guild:{int(guild_id)}:list:{key}"


async def get_guild_setting(guild_id: int, key: str, default: Any = None) -> Any:
    return await hget_json(_settings_key(guild_id), key, default=default)


async def get_guild_settings(guild_id: int) -> dict[str, Any]:
    # Note: returns JSON-decoded values.
    return await hgetall_json(_settings_key(guild_id))


async def set_guild_setting(guild_id: int, key: str, value: Any) -> None:
    await hset_json(_settings_key(guild_id), key, value)

    # Fast migration: mirror premium tier into Postgres (source-of-truth).
    # This keeps /settings premium toggles working without changing command code.
    if str(key) == "premium_tier":
        try:
            from utils.db import get_sessionmaker
            from utils.models import PremiumEntitlement
            from sqlalchemy import select  # type: ignore

            tier = str(value or "free").strip().lower()
            if tier not in {"free", "pro"}:
                tier = "free"

            Session = get_sessionmaker()
            async with Session() as session:
                res = await session.execute(
                    select(PremiumEntitlement).where(PremiumEntitlement.guild_id == int(guild_id))
                )
                row = res.scalar_one_or_none()
                if row is None:
                    row = PremiumEntitlement(guild_id=int(guild_id), tier=tier, source="manual")
                    session.add(row)
                else:
                    row.tier = tier
                    row.source = getattr(row, "source", "manual") or "manual"
                await session.commit()
        except Exception:
            # Do not break settings if DB is down; get_premium_tier() has a Redis fallback.
            return


async def delete_guild_setting(guild_id: int, key: str) -> None:
    # Redis HDEL returns count; but we don't need it.
    from utils.backpressure import get_redis_or_none
    r = await get_redis_or_none()
    if r is None:
        return
    await r.hdel(_settings_key(guild_id), key)


# ---------- "list settings" (like allowed channels, etc.) ----------

async def list_add(guild_id: int, key: str, value: str) -> bool:
    """
    Add a value to a guild list setting.  Returns True if the value was
    newly added, False if it was already present.
    """
    added = await sadd(_list_key(guild_id, key), str(value))
    # Redis SADD returns 1 if the element was added, 0 if it already existed
    return bool(int(added))


async def list_remove(guild_id: int, key: str, value: str) -> bool:
    """
    Remove a value from a guild list setting.  Returns True if the value
    was removed, False if it was not present.
    """
    removed = await srem(_list_key(guild_id, key), str(value))
    # Redis SREM returns the number of removed elements (0 or 1)
    return bool(int(removed))


async def list_members(guild_id: int, key: str) -> set[str]:
    return await smembers_str(_list_key(guild_id, key))

# -------------------------------------------------------------------
# Compatibility helpers
# Some modules (ex: utils.permissions) expect list_get() to exist.
# -------------------------------------------------------------------

def list_get(obj, key: str, default=None):
    """
    Safe getter for list-like settings stored in dicts.
    Returns default if missing / wrong type.
    """
    if default is None:
        default = []

    if obj is None:
        return default

    # common case: settings dict
    if isinstance(obj, dict):
        val = obj.get(key, default)
    else:
        # fallback: attribute access
        val = getattr(obj, key, default)

    # normalize
    if val is None:
        return default
    if isinstance(val, list):
        return val
    if isinstance(val, (set, tuple)):
        return list(val)

    # allow comma-separated strings just in case
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(",") if p.strip()]
        return parts if parts else default

    return default

