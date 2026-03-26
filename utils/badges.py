from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from utils.db import get_sessionmaker

try:
    from sqlalchemy import select  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _badge_key_from_label(label: str) -> str:
    raw = (label or "").strip().lower()
    out = []
    prev_sep = False
    for ch in raw:
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        else:
            if not prev_sep:
                out.append("_")
            prev_sep = True
    key = "".join(out).strip("_")
    key = key[:96] if key else "badge"
    return f"devbadge:{key}"


async def create_badge_definition(
    *,
    emoji: str,
    label: str,
    name: str | None = None,
    description: str | None = None,
    how_to_obtain: str | None = None,
    created_by_user_id: int | None = None,
) -> tuple[bool, str, str | None]:
    if select is None:
        return False, "sqlalchemy not available", None
    from utils.models import BadgeDefinition

    emj = (emoji or "").strip()[:16]
    lbl = (label or "").strip()[:120]
    if not emj or not lbl:
        return False, "Emoji and label are required.", None

    badge_key = _badge_key_from_label(lbl)
    Session = get_sessionmaker()
    async with Session() as session:
        existing = await session.execute(
            select(BadgeDefinition).where(BadgeDefinition.badge_key == badge_key).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return False, "A badge with this key already exists.", None
        session.add(
            BadgeDefinition(
                badge_key=badge_key,
                emoji=emj,
                label=lbl,
                name=(name or "").strip()[:120],
                description=(description or "").strip(),
                how_to_obtain=(how_to_obtain or "").strip(),
                created_by_user_id=(int(created_by_user_id) if created_by_user_id else None),
                created_at=_now(),
            )
        )
        await session.commit()
    return True, "Badge created.", badge_key


async def list_badge_definitions(*, limit: int = 200) -> list[dict[str, Any]]:
    if select is None:
        return []
    from utils.models import BadgeDefinition

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(BadgeDefinition).order_by(BadgeDefinition.created_at.desc()).limit(max(1, int(limit)))
        )
        rows = res.scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows or []:
        out.append(
            {
                "badge_key": str(getattr(r, "badge_key", "") or ""),
                "emoji": str(getattr(r, "emoji", "") or ""),
                "label": str(getattr(r, "label", "") or ""),
                "name": str(getattr(r, "name", "") or ""),
                "description": str(getattr(r, "description", "") or ""),
                "how_to_obtain": str(getattr(r, "how_to_obtain", "") or ""),
            }
        )
    return out


async def grant_defined_badge_to_user(*, user_id: int, badge_key: str) -> tuple[bool, str]:
    if select is None:
        return False, "sqlalchemy not available"
    from utils.models import BadgeDefinition, UserProfileBadge

    key = (badge_key or "").strip()[:128]
    if not key:
        return False, "badge_key required"

    Session = get_sessionmaker()
    async with Session() as session:
        r_def = await session.execute(
            select(BadgeDefinition).where(BadgeDefinition.badge_key == key).limit(1)
        )
        bdef = r_def.scalar_one_or_none()
        if bdef is None:
            return False, "Badge definition not found."

        r_existing = await session.execute(
            select(UserProfileBadge)
            .where(UserProfileBadge.user_id == int(user_id))
            .where(UserProfileBadge.badge_key == key)
            .limit(1)
        )
        if r_existing.scalar_one_or_none() is not None:
            return False, "User already has this badge."

        display = f"{str(getattr(bdef, 'emoji', '') or '').strip()} {str(getattr(bdef, 'label', '') or '').strip()}".strip()
        session.add(
            UserProfileBadge(
                user_id=int(user_id),
                badge_key=key,
                display_text=display[:200],
                source_event_id=None,
                created_at=_now(),
            )
        )
        await session.commit()
    return True, "Badge granted."


async def build_badge_catalog() -> list[dict[str, str]]:
    """Return all known badges with emoji, name and short obtain description."""
    out: list[dict[str, str]] = [
        {
            "emoji": "🏅",
            "name": "30-day Streak",
            "label": "30-day Streak",
            "description": "Claim daily points for 30 days.",
            "source": "streak",
        },
        {
            "emoji": "🏅",
            "name": "60-day Streak",
            "label": "60-day Streak",
            "description": "Claim daily points for 60 days.",
            "source": "streak",
        },
        {
            "emoji": "🏅",
            "name": "90-day Streak",
            "label": "90-day Streak",
            "description": "Claim daily points for 90 days.",
            "source": "streak",
        },
    ]

    try:
        if select is not None:
            from utils.models import GlobalQuestEvent

            Session = get_sessionmaker()
            async with Session() as session:
                res = await session.execute(select(GlobalQuestEvent))
                events = res.scalars().all()
            for ev in events or []:
                emoji = str(getattr(ev, "success_badge_emoji", "") or "").strip() or "🏆"
                label = str(getattr(ev, "success_badge_label", "") or "").strip() or str(getattr(ev, "title", "Quest Winner") or "Quest Winner")
                title = str(getattr(ev, "title", "") or "").strip() or "Global Quest"
                out.append(
                    {
                        "emoji": emoji,
                        "name": label,
                        "label": label,
                        "description": f"Rewarded for completing community event: {title}.",
                        "source": "global_quest",
                    }
                )
    except Exception:
        pass

    for d in await list_badge_definitions(limit=500):
        out.append(
            {
                "emoji": str(d.get("emoji") or "").strip() or "🏷️",
                "name": str(d.get("name") or d.get("label") or "Badge"),
                "label": str(d.get("label") or d.get("name") or "Badge"),
                "description": str(d.get("description") or d.get("how_to_obtain") or "Granted by developers."),
                "source": "dev",
            }
        )

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for row in out:
        key = (str(row.get("source") or ""), str(row.get("label") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped
