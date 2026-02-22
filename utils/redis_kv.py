from __future__ import annotations

import json
from typing import Any, Optional

from utils.backpressure import get_redis_or_none

_JSON = json.dumps
_JLOAD = json.loads


def _j(obj: Any) -> str:
    return _JSON(obj, separators=(",", ":"), ensure_ascii=False)


def _unj(s: str | bytes | None, default: Any = None) -> Any:
    if s is None:
        return default
    if isinstance(s, (bytes, bytearray)):
        s = s.decode("utf-8", errors="ignore")
    try:
        return _JLOAD(s)
    except Exception:
        return default


async def kv_get_json(key: str, default: Any = None) -> Any:
    r = await get_redis_or_none()
    if r is None:
        return default
    val = await r.get(key)
    return _unj(val, default=default)


async def kv_set_json(key: str, value: Any, *, ex: int | None = None) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    await r.set(key, _j(value), ex=ex)


async def kv_del(*keys: str) -> int:
    if not keys:
        return 0
    r = await get_redis_or_none()
    if r is None:
        return 0
    return int(await r.delete(*keys))


async def hgetall_json(key: str) -> dict[str, Any]:
    r = await get_redis_or_none()
    if r is None:
        return {}
    raw = await r.hgetall(key)
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        kk = k.decode("utf-8", errors="ignore") if isinstance(k, (bytes, bytearray)) else str(k)
        out[kk] = _unj(v, default=None)
    return out


async def hset_json(key: str, field: str, value: Any) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    await r.hset(key, field, _j(value))


async def hget_json(key: str, field: str, default: Any = None) -> Any:
    r = await get_redis_or_none()
    if r is None:
        return default
    val = await r.hget(key, field)
    return _unj(val, default=default)


async def sadd(key: str, *members: str) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    if not members:
        return 0
    return int(await r.sadd(key, *members))


async def srem(key: str, *members: str) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    if not members:
        return 0
    return int(await r.srem(key, *members))


async def smembers_str(key: str) -> set[str]:
    r = await get_redis_or_none()
    if r is None:
        return set()
    raw = await r.smembers(key)
    out=set()
    for m in raw or set():
        out.add(m.decode('utf-8', errors='ignore') if isinstance(m,(bytes,bytearray)) else str(m))
    return out


async def incr(key: str, amount: int = 1, *, ex: int | None = None) -> int:
    r = await get_redis_or_none()
    if r is None:
        return 0
    val = int(await r.incrby(key, amount))
    if ex:
        try:
            await r.expire(key, ex)
        except Exception:
            pass
    return val
