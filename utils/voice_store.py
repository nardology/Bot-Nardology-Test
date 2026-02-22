from __future__ import annotations

"""DB helpers for custom voice sound registry.

Durable mapping:
  (guild_id, name) -> storage_mode, object_key, url

Used by /voice commands so custom uploads can work across multiple instances.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy import select, delete

from utils.db import get_sessionmaker
from utils.models import VoiceSound


@dataclass(frozen=True)
class VoiceSoundRecord:
    guild_id: int
    name: str
    storage_mode: str
    object_key: Optional[str]
    url: Optional[str]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def list_voice_sounds(*, guild_id: int) -> List[VoiceSoundRecord]:
    Session = get_sessionmaker()
    async with Session() as session:
        stmt = select(VoiceSound).where(VoiceSound.guild_id == int(guild_id))
        res = await session.execute(stmt)
        rows = res.scalars().all()
        out: List[VoiceSoundRecord] = []
        for r in rows:
            out.append(
                VoiceSoundRecord(
                    guild_id=int(r.guild_id),
                    name=str(r.name),
                    storage_mode=str(r.storage_mode or "local"),
                    object_key=(str(r.object_key) if r.object_key else None),
                    url=(str(r.url) if r.url else None),
                )
            )
        return out


async def get_voice_sound(*, guild_id: int, name: str) -> Optional[VoiceSoundRecord]:
    Session = get_sessionmaker()
    async with Session() as session:
        stmt = (
            select(VoiceSound)
            .where(VoiceSound.guild_id == int(guild_id))
            .where(VoiceSound.name == str(name))
            .limit(1)
        )
        res = await session.execute(stmt)
        r = res.scalars().first()
        if not r:
            return None
        return VoiceSoundRecord(
            guild_id=int(r.guild_id),
            name=str(r.name),
            storage_mode=str(r.storage_mode or "local"),
            object_key=(str(r.object_key) if r.object_key else None),
            url=(str(r.url) if r.url else None),
        )


async def upsert_voice_sound(
    *,
    guild_id: int,
    name: str,
    storage_mode: str,
    object_key: Optional[str],
    url: Optional[str],
) -> None:
    """Insert or update the voice sound record."""
    Session = get_sessionmaker()
    async with Session() as session:
        existing = await get_voice_sound(guild_id=guild_id, name=name)
        now = _now_utc()
        if existing is None:
            session.add(
                VoiceSound(
                    guild_id=int(guild_id),
                    name=str(name),
                    storage_mode=str(storage_mode or "local"),
                    object_key=object_key,
                    url=url,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            stmt = (
                VoiceSound.__table__.update()
                .where(VoiceSound.guild_id == int(guild_id))
                .where(VoiceSound.name == str(name))
                .values(
                    storage_mode=str(storage_mode or "local"),
                    object_key=object_key,
                    url=url,
                    updated_at=now,
                )
            )
            await session.execute(stmt)
        await session.commit()


async def delete_voice_sound(*, guild_id: int, name: str) -> bool:
    Session = get_sessionmaker()
    async with Session() as session:
        stmt = delete(VoiceSound).where(VoiceSound.guild_id == int(guild_id)).where(VoiceSound.name == str(name))
        res = await session.execute(stmt)
        await session.commit()
        return (res.rowcount or 0) > 0
