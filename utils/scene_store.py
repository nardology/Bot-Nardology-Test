from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Sequence

from utils.backpressure import get_redis_or_none

# ----------------------------
# Data models (lightweight)
# ----------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RPScene:
    id: int
    guild_id: int
    channel_id: int
    creator_user_id: int
    p1_user_id: int
    p1_style_id: str
    p2_user_id: int
    p2_style_id: str
    turn_user_id: int
    setting: str | None
    is_active: bool
    is_public: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class RPSceneLine:
    id: int
    scene_id: int
    guild_id: int
    channel_id: int
    speaker_user_id: int
    speaker_style_id: str
    content: str
    created_at: datetime


# ----------------------------
# Key helpers
# ----------------------------

def _scene_key(scene_id: int) -> str:
    return f"scene:{int(scene_id)}"


def _scene_lines_key(scene_id: int) -> str:
    return f"scene:{int(scene_id)}:lines"


def _scene_next_id_key() -> str:
    return "scene:next_id"


def _active_guild_key(guild_id: int) -> str:
    return f"scene:active:guild:{int(guild_id)}"


def _active_channel_key(guild_id: int, channel_id: int) -> str:
    return f"scene:active:guild:{int(guild_id)}:channel:{int(channel_id)}"


def _active_user_key(guild_id: int, user_id: int) -> str:
    return f"scene:active:guild:{int(guild_id)}:user:{int(user_id)}"


def _j(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _unj(s: str | bytes | None, default=None):
    if s is None:
        return default
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("utf-8", errors="ignore")
    try:
        return json.loads(s)
    except Exception:
        return default


def _to_scene(d: dict) -> RPScene:
    def dt(x):
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(float(x), tz=timezone.utc)
        if isinstance(x, str):
            try:
                return datetime.fromisoformat(x)
            except Exception:
                pass
        return _now_utc()

    return RPScene(
        id=int(d["id"]),
        guild_id=int(d["guild_id"]),
        channel_id=int(d["channel_id"]),
        creator_user_id=int(d["creator_user_id"]),
        p1_user_id=int(d["p1_user_id"]),
        p1_style_id=str(d.get("p1_style_id") or ""),
        p2_user_id=int(d["p2_user_id"]),
        p2_style_id=str(d.get("p2_style_id") or ""),
        turn_user_id=int(d["turn_user_id"]),
        setting=d.get("setting"),
        is_active=bool(d.get("is_active", True)),
        is_public=bool(d.get("is_public", False)),
        created_at=dt(d.get("created_at")),
        updated_at=dt(d.get("updated_at")),
    )


def _to_line(scene_id: int, guild_id: int, channel_id: int, d: dict) -> RPSceneLine:
    created_at = d.get("created_at")
    if isinstance(created_at, (int, float)):
        dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
    elif isinstance(created_at, str):
        try:
            dt = datetime.fromisoformat(created_at)
        except Exception:
            dt = _now_utc()
    else:
        dt = _now_utc()
    return RPSceneLine(
        id=int(d.get("id") or 0),
        scene_id=int(scene_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        speaker_user_id=int(d.get("speaker_user_id") or 0),
        speaker_style_id=str(d.get("speaker_style_id") or ""),
        content=str(d.get("content") or ""),
        created_at=dt,
    )


async def _load_scene(scene_id: int) -> dict | None:
    r = await get_redis_or_none()
    if r is None:
        return None
    raw = await r.get(_scene_key(scene_id))
    return _unj(raw, default=None)


async def _save_scene(scene_id: int, data: dict) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    await r.set(_scene_key(scene_id), _j(data))


async def create_scene(
    *,
    guild_id: int,
    channel_id: int,
    creator_user_id: int,
    p1_user_id: int,
    p1_style_id: str,
    p2_user_id: int,
    p2_style_id: str,
    setting: str | None,
    turn_user_id: int,
    is_public: bool = False,
) -> RPScene:
    r = await get_redis_or_none()
    if r is None:
        raise RuntimeError("Redis unavailable")
    scene_id = int(await r.incr(_scene_next_id_key()))
    now = _now_utc()

    data = {
        "id": scene_id,
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "creator_user_id": int(creator_user_id),
        "p1_user_id": int(p1_user_id),
        "p1_style_id": str(p1_style_id),
        "p2_user_id": int(p2_user_id),
        "p2_style_id": str(p2_style_id),
        "turn_user_id": int(turn_user_id),
        "setting": setting,
        "is_active": True,
        "is_public": bool(is_public),
        "created_at": now.timestamp(),
        "updated_at": now.timestamp(),
    }
    pipe = r.pipeline()
    pipe.set(_scene_key(scene_id), _j(data))
    pipe.sadd(_active_guild_key(guild_id), str(scene_id))
    pipe.sadd(_active_channel_key(guild_id, channel_id), str(scene_id))
    pipe.sadd(_active_user_key(guild_id, p1_user_id), str(scene_id))
    pipe.sadd(_active_user_key(guild_id, p2_user_id), str(scene_id))
    await pipe.execute()
    return _to_scene(data)


async def get_scene(*, scene_id: int) -> RPScene | None:
    d = await _load_scene(int(scene_id))
    if not d:
        return None
    return _to_scene(d)


async def end_scene(*, scene_id: int) -> bool:
    scene_id = int(scene_id)
    d = await _load_scene(scene_id)
    if not d:
        return False
    if not bool(d.get("is_active", True)):
        return True

    d["is_active"] = False
    d["updated_at"] = _now_utc().timestamp()
    await _save_scene(scene_id, d)

    # Remove from active indexes
    r = await get_redis_or_none()
    if r is None:
        return True
    guild_id = int(d["guild_id"])
    channel_id = int(d["channel_id"])
    p1 = int(d["p1_user_id"])
    p2 = int(d["p2_user_id"])
    pipe = r.pipeline()
    pipe.srem(_active_guild_key(guild_id), str(scene_id))
    pipe.srem(_active_channel_key(guild_id, channel_id), str(scene_id))
    pipe.srem(_active_user_key(guild_id, p1), str(scene_id))
    pipe.srem(_active_user_key(guild_id, p2), str(scene_id))
    await pipe.execute()
    return True


async def add_scene_line(
    *,
    scene_id: int,
    guild_id: int,
    channel_id: int,
    speaker_user_id: int,
    speaker_style_id: str,
    content: str,
) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    # line id is best-effort: list length + 1
    line = {
        "speaker_user_id": int(speaker_user_id),
        "speaker_style_id": str(speaker_style_id),
        "content": str(content),
        "created_at": _now_utc().timestamp(),
    }
    await r.rpush(_scene_lines_key(scene_id), _j(line))

    # Touch updated_at for the scene
    d = await _load_scene(scene_id)
    if d:
        d["updated_at"] = _now_utc().timestamp()
        await _save_scene(scene_id, d)


async def get_recent_scene_lines(*, scene_id: int, limit: int = 6) -> list[RPSceneLine]:
    limit = max(1, min(int(limit or 6), 50))
    d = await _load_scene(int(scene_id))
    if not d:
        return []
    r = await get_redis_or_none()
    if r is None:
        return []
    raw = await r.lrange(_scene_lines_key(scene_id), -limit, -1)
    out: list[RPSceneLine] = []
    i0 = max(0, int(await r.llen(_scene_lines_key(scene_id))) - len(raw))
    for idx, item in enumerate(raw or []):
        line_d = _unj(item, default={}) or {}
        out.append(_to_line(scene_id, int(d["guild_id"]), int(d["channel_id"]), {"id": i0 + idx + 1, **line_d}))
    return out


async def delete_scene_lines(*, scene_id: int) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    key = _scene_lines_key(int(scene_id))
    n = int(await r.llen(key))
    await r.delete(key)
    return n


async def flip_turn(*, scene_id: int) -> int | None:
    scene_id = int(scene_id)
    d = await _load_scene(scene_id)
    if not d or not bool(d.get("is_active", True)):
        return None

    if int(d["turn_user_id"]) == int(d["p1_user_id"]):
        d["turn_user_id"] = int(d["p2_user_id"])
    else:
        d["turn_user_id"] = int(d["p1_user_id"])

    d["updated_at"] = _now_utc().timestamp()
    await _save_scene(scene_id, d)
    return int(d["turn_user_id"])


async def find_active_scene_between(
    *,
    guild_id: int,
    channel_id: int,
    user_a: int,
    user_b: int,
) -> RPScene | None:
    r = await get_redis_or_none()
    if r is None:
        return None
    ids = await r.smembers(_active_channel_key(guild_id, channel_id))
    for sid_b in ids or set():
        sid = int(sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else sid_b)
        d = await _load_scene(sid)
        if not d or not bool(d.get("is_active", True)):
            continue
        p1 = int(d.get("p1_user_id", 0))
        p2 = int(d.get("p2_user_id", 0))
        if (p1 == int(user_a) and p2 == int(user_b)) or (p1 == int(user_b) and p2 == int(user_a)):
            return _to_scene(d)
    return None


async def list_active_scenes_in_channel(
    *,
    guild_id: int,
    channel_id: int,
    limit: int = 10,
) -> list[RPScene]:
    limit = max(1, min(int(limit or 10), 50))
    r = await get_redis_or_none()
    if r is None:
        return []
    ids = await r.smembers(_active_channel_key(guild_id, channel_id))
    scenes: list[RPScene] = []
    for sid_b in ids or set():
        sid = int(sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else sid_b)
        d = await _load_scene(sid)
        if not d or not bool(d.get("is_active", True)):
            continue
        scenes.append(_to_scene(d))
    scenes.sort(key=lambda s: s.updated_at, reverse=True)
    return scenes[:limit]


async def count_active_scenes_in_channel(*, guild_id: int, channel_id: int) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    return int(await r.scard(_active_channel_key(guild_id, channel_id)))


async def count_active_scenes_in_guild(*, guild_id: int) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    return int(await r.scard(_active_guild_key(guild_id)))


async def count_active_scenes_for_user(*, guild_id: int, user_id: int) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    return int(await r.scard(_active_user_key(guild_id, user_id)))


async def expire_scene_if_stale(*, scene_id: int, ttl_seconds: int) -> bool:
    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0:
        return False

    d = await _load_scene(int(scene_id))
    if not d or not bool(d.get("is_active", True)):
        return False

    updated_at = float(d.get("updated_at") or d.get("created_at") or _now_utc().timestamp())
    age = (_now_utc().timestamp() - updated_at)
    if age <= ttl_seconds:
        return False

    await end_scene(scene_id=int(scene_id))
    try:
        await delete_scene_lines(scene_id=int(scene_id))
    except Exception:
        pass
    return True


async def expire_stale_scenes_in_channel(*, guild_id: int, channel_id: int, ttl_seconds: int) -> int:
    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0:
        return 0

    cutoff_ts = _now_utc().timestamp() - ttl_seconds
    r = await get_redis_or_none()
    if r is None:
        return 0
    ids = await r.smembers(_active_channel_key(guild_id, channel_id))
    expired = 0
    for sid_b in ids or set():
        sid = int(sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else sid_b)
        d = await _load_scene(sid)
        if not d or not bool(d.get("is_active", True)):
            continue
        updated_at = float(d.get("updated_at") or d.get("created_at") or 0)
        if updated_at < cutoff_ts:
            if await end_scene(scene_id=sid):
                expired += 1
            try:
                await delete_scene_lines(scene_id=sid)
            except Exception:
                pass
    return expired
