"""Global / guild-scoped monthly quest: training from /talk (guild vs global formulas in _apply_training_delta)."""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from utils.points_store import GLOBAL_GUILD_ID, adjust_points
from utils.db import get_sessionmaker

try:
    from sqlalchemy import select, func, update  # type: ignore
except Exception:  # pragma: no cover
    select = func = update = None  # type: ignore

logger = logging.getLogger("bot.global_quest")
_JSON_LOCK = threading.Lock()


def _json_mode_enabled() -> bool:
    raw = (os.getenv("GLOBAL_QUEST_JSON_MODE", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _json_path() -> Path:
    raw = (os.getenv("GLOBAL_QUEST_JSON_PATH", "data/global_quest.json") or "data/global_quest.json").strip()
    return Path(raw)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _from_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        txt = str(s).strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        d = datetime.fromisoformat(txt)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _default_event_dict(*, event_id: int = 1) -> dict[str, Any]:
    now = _now()
    return {
        "id": int(event_id),
        "slug": "global-quest",
        "title": "Community Global Quest",
        "description": "",
        "image_url": None,
        "image_url_secondary": None,
        "scope": "global",
        "guild_id": None,
        "starts_at": _iso(now),
        "ends_at": _iso(now),
        "activated_at": None,
        "target_training_points": 100000,
        "status": "draft",
        "character_multipliers": {},
        "reward_points": 0,
        "failure_points": 0,
        "success_badge_emoji": "🏆",
        "success_badge_label": "Quest Winner",
        "grant_success_badge": True,
        "resolution_applied": False,
    }


def _default_store() -> dict[str, Any]:
    return {"events": [_default_event_dict(event_id=1)], "contributions": {}}


def _is_nested_contributions(raw: dict) -> bool:
    for v in raw.values():
        return isinstance(v, dict)
    return False


def _normalize_contributions_dict(
    raw: Any, default_event_id: int
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    if not isinstance(raw, dict) or not raw:
        return out
    if _is_nested_contributions(raw):
        for eid, bucket in raw.items():
            if not isinstance(bucket, dict):
                continue
            inner: dict[str, int] = {}
            for ck, cv in bucket.items():
                try:
                    inner[str(ck)] = max(0, int(cv or 0))
                except Exception:
                    continue
            out[str(eid)] = inner
        return out
    inner_flat: dict[str, int] = {}
    for ck, cv in raw.items():
        try:
            inner_flat[str(ck)] = max(0, int(cv or 0))
        except Exception:
            continue
    if inner_flat:
        out[str(int(default_event_id))] = inner_flat
    return out


def _coerce_store_in_place(data: dict[str, Any]) -> None:
    events_in = data.get("events")
    if isinstance(events_in, list) and events_in:
        events = [dict(e) for e in events_in if isinstance(e, dict)]
    else:
        legacy = data.get("event")
        if isinstance(legacy, dict):
            events = [dict(legacy)]
        else:
            events = []
    if not events:
        events = [_default_event_dict(event_id=1)]
    data["events"] = events
    if "event" in data:
        del data["event"]
    try:
        eid0 = int(events[0].get("id") or 1)
    except Exception:
        eid0 = 1
    if not isinstance(data.get("contributions"), dict):
        data["contributions"] = {}
    data["contributions"] = _normalize_contributions_dict(
        data.get("contributions"), eid0
    )


def _read_store_sync() -> dict[str, Any]:
    p = _json_path()
    with _JSON_LOCK:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            data = _default_store()
            p.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
            return data
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        _coerce_store_in_place(data)
        return data


def _write_store_sync(data: dict[str, Any]) -> None:
    p = _json_path()
    with _JSON_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")


async def _read_store() -> dict[str, Any]:
    return _read_store_sync()


async def _write_store(data: dict[str, Any]) -> None:
    _write_store_sync(data)


@dataclass
class ActiveQuestView:
    event_id: int
    slug: str
    title: str
    description: str
    image_url: str | None
    image_url_secondary: str | None
    scope: str
    guild_id: int | None
    starts_at: datetime
    ends_at: datetime
    activated_at: datetime | None
    target_training_points: int
    status: str
    character_multipliers: dict[str, float]
    total_training: int
    guild_training: int  # for this guild only
    user_training: int  # this user sum across characters
    user_character_training: int  # selected character only
    days_left: int
    progress_pct: float


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_multipliers(raw: str | None) -> dict[str, float]:
    try:
        d = json.loads(raw or "{}")
        if not isinstance(d, dict):
            return {}
        out: dict[str, float] = {}
        for k, v in d.items():
            try:
                out[str(k).strip().lower()] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


async def get_active_events_for_guild(*, guild_id: int) -> list[Any]:
    """Active events that apply to this guild (global + guild-specific)."""
    if _json_mode_enabled():
        data = await _read_store()
        now = _now()
        out: list[Any] = []
        for ev in data.get("events") or []:
            if not isinstance(ev, dict):
                continue
            status = (str(ev.get("status") or "").strip().lower())
            if status != "active":
                continue
            ends = _from_iso(ev.get("ends_at"))
            if ends and ends < now:
                continue
            sc = (str(ev.get("scope") or "global").strip().lower() or "global")
            eg = ev.get("guild_id")
            if sc == "guild":
                try:
                    if eg is None or int(eg) != int(guild_id):
                        continue
                except Exception:
                    continue
            wrapped = SimpleNamespace(
                id=int(ev.get("id") or 1),
                slug=str(ev.get("slug") or ""),
                title=str(ev.get("title") or ""),
                description=str(ev.get("description") or ""),
                image_url=ev.get("image_url"),
                image_url_secondary=ev.get("image_url_secondary"),
                scope=sc,
                guild_id=(int(eg) if eg is not None and str(eg).strip().lstrip("-").isdigit() else None),
                starts_at=_from_iso(ev.get("starts_at")) or now,
                ends_at=ends or now,
                activated_at=_from_iso(ev.get("activated_at")),
                target_training_points=int(ev.get("target_training_points") or 1),
                status=status,
                character_multipliers_json=json.dumps(ev.get("character_multipliers") or {}, separators=(",", ":")),
                reward_points=int(ev.get("reward_points") or 0),
                failure_points=int(ev.get("failure_points") or 0),
                success_badge_emoji=str(ev.get("success_badge_emoji") or "🏆"),
                success_badge_label=str(ev.get("success_badge_label") or ev.get("title") or "Quest"),
                grant_success_badge=bool(ev.get("grant_success_badge", True)),
                resolution_applied=bool(ev.get("resolution_applied", False)),
            )
            out.append(wrapped)
        return out
    if select is None:
        return []
    from utils.models import GlobalQuestEvent

    gid = int(guild_id)
    now = _now()
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            # Query broadly, then normalize/filter in Python to tolerate legacy status values.
            res = await session.execute(select(GlobalQuestEvent))
            rows = res.scalars().all()
            out: list[Any] = []
            for ev in rows:
                st = (getattr(ev, "status", "") or "").strip().lower()
                if st != "active":
                    continue
                ends = getattr(ev, "ends_at", None)
                if ends is not None:
                    if getattr(ends, "tzinfo", None) is None:
                        ends = ends.replace(tzinfo=timezone.utc)
                    if ends < now:
                        continue
                sc = (getattr(ev, "scope", "") or "").strip().lower() or "global"
                eg = getattr(ev, "guild_id", None)
                if sc == "global":
                    out.append(ev)
                elif sc == "guild" and eg is not None and int(eg) == gid:
                    out.append(ev)
            return out
        except Exception:
            logger.exception("get_active_events_for_guild failed")
            return []


async def _sum_training(*, event_id: int, guild_id: int | None = None) -> int:
    if _json_mode_enabled():
        data = await _read_store()
        contrib_all = data.get("contributions") or {}
        bucket = contrib_all.get(str(int(event_id)))
        if not isinstance(bucket, dict):
            bucket = {}
        total = 0
        for key, raw in bucket.items():
            try:
                egid_s, _, _ = str(key).split(":", 2)
                egid = int(egid_s)
                pts = int(raw or 0)
            except Exception:
                continue
            if guild_id is not None and egid != int(guild_id):
                continue
            total += max(0, pts)
        return int(total)
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    Session = get_sessionmaker()
    async with Session() as session:
        q = select(func.sum(GlobalQuestContribution.training_points)).where(
            GlobalQuestContribution.event_id == int(event_id)
        )
        if guild_id is not None:
            q = q.where(GlobalQuestContribution.guild_id == int(guild_id))
        row = (await session.execute(q)).scalar()
        return int(row or 0)


async def _user_training_sum(*, event_id: int, user_id: int, guild_id: int) -> int:
    if _json_mode_enabled():
        data = await _read_store()
        contrib_all = data.get("contributions") or {}
        bucket = contrib_all.get(str(int(event_id)))
        if not isinstance(bucket, dict):
            bucket = {}
        total = 0
        prefix = f"{int(guild_id)}:{int(user_id)}:"
        for key, raw in bucket.items():
            if not str(key).startswith(prefix):
                continue
            try:
                total += max(0, int(raw or 0))
            except Exception:
                continue
        return int(total)
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    Session = get_sessionmaker()
    async with Session() as session:
        row = (
            await session.execute(
                select(func.sum(GlobalQuestContribution.training_points)).where(
                    GlobalQuestContribution.event_id == int(event_id),
                    GlobalQuestContribution.guild_id == int(guild_id),
                    GlobalQuestContribution.user_id == int(user_id),
                )
            )
        ).scalar()
        return int(row or 0)


async def _user_style_training(
    *, event_id: int, user_id: int, guild_id: int, style_id: str
) -> int:
    if _json_mode_enabled():
        data = await _read_store()
        contrib_all = data.get("contributions") or {}
        bucket = contrib_all.get(str(int(event_id)))
        if not isinstance(bucket, dict):
            bucket = {}
        key = f"{int(guild_id)}:{int(user_id)}:{(style_id or '').strip().lower()}"
        try:
            return int(bucket.get(key) or 0)
        except Exception:
            return 0
    from utils.models import GlobalQuestContribution

    if select is None:
        return 0
    sid = (style_id or "").strip().lower()
    Session = get_sessionmaker()
    async with Session() as session:
        row = (
            await session.execute(
                select(GlobalQuestContribution.training_points).where(
                    GlobalQuestContribution.event_id == int(event_id),
                    GlobalQuestContribution.guild_id == int(guild_id),
                    GlobalQuestContribution.user_id == int(user_id),
                    GlobalQuestContribution.style_id == sid,
                )
            )
        ).scalar_one_or_none()
        return int(row or 0)


async def build_quest_view_for_user(
    *,
    guild_id: int,
    user_id: int,
    selected_style_id: str | None,
) -> ActiveQuestView | None:
    """Single embed-friendly view: prefer guild-scoped event, else first global."""
    events = await get_active_events_for_guild(guild_id=guild_id)
    if not events:
        return None

    def _embed_pick_key(e: Any) -> tuple[int, int]:
        sc = (getattr(e, "scope", "") or "").strip().lower()
        return (0 if sc == "guild" else 1, int(getattr(e, "id", 0) or 0))

    ev = sorted(events, key=_embed_pick_key)[0]

    eid = int(getattr(ev, "id", 0))
    scope = (getattr(ev, "scope", "") or "").strip().lower()
    eg = getattr(ev, "guild_id", None)
    total = await _sum_training(event_id=eid, guild_id=None)
    if scope == "guild" and eg is not None:
        gtrain = await _sum_training(event_id=eid, guild_id=int(eg))
    else:
        gtrain = await _sum_training(event_id=eid, guild_id=int(guild_id))

    ut = await _user_training_sum(event_id=eid, user_id=user_id, guild_id=int(guild_id))
    sid = (selected_style_id or "").strip().lower()
    uc = await _user_style_training(
        event_id=eid, user_id=user_id, guild_id=int(guild_id), style_id=sid
    ) if sid else 0

    target = max(1, int(getattr(ev, "target_training_points", 1) or 1))
    if scope == "guild" and eg is not None:
        bar_total = float(gtrain)
    else:
        bar_total = float(total)
    pct = min(100.0, 100.0 * bar_total / float(target))

    ends = getattr(ev, "ends_at", None)
    if ends and getattr(ends, "tzinfo", None) is None:
        ends = ends.replace(tzinfo=timezone.utc)
    dl = 0
    if ends:
        dl = max(0, int((ends - _now()).total_seconds() // 86400))

    mult = _parse_multipliers(getattr(ev, "character_multipliers_json", None))

    act_at = getattr(ev, "activated_at", None)
    if act_at and getattr(act_at, "tzinfo", None) is None:
        act_at = act_at.replace(tzinfo=timezone.utc)

    return ActiveQuestView(
        event_id=eid,
        slug=str(getattr(ev, "slug", "") or ""),
        title=str(getattr(ev, "title", "") or ""),
        description=str(getattr(ev, "description", "") or ""),
        image_url=getattr(ev, "image_url", None),
        image_url_secondary=getattr(ev, "image_url_secondary", None),
        scope=scope,
        guild_id=int(eg) if eg is not None else None,
        starts_at=getattr(ev, "starts_at", _now()),
        ends_at=ends or _now(),
        activated_at=act_at,
        target_training_points=target,
        status=str(getattr(ev, "status", "") or ""),
        character_multipliers=mult,
        total_training=total,
        guild_training=gtrain,
        user_training=ut,
        user_character_training=uc,
        days_left=dl,
        progress_pct=round(pct, 1),
    )


async def record_training_from_talk(
    *,
    guild_id: int,
    user_id: int,
    style_id: str,
    selected_style_id: str,
    bond_xp_gained: int,
    bond_level: int | None = None,
) -> list[dict[str, Any]]:
    """Award training when user talks with their selected owned character."""
    sid = (style_id or "").strip().lower()
    sel = (selected_style_id or "").strip().lower()
    if not sid or not sel or sid != sel:
        return []

    events = await get_active_events_for_guild(guild_id=int(guild_id))
    if not events:
        return []
    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            delta = await _apply_training_delta(
                event=ev,
                guild_id=int(guild_id),
                user_id=int(user_id),
                style_id=sid,
                bond_xp_gained=int(bond_xp_gained),
                bond_level=int(bond_level) if bond_level is not None else None,
            )
            out.append(
                {
                    "event_id": int(getattr(ev, "id", 0) or 0),
                    "title": str(getattr(ev, "title", "") or "Global quest"),
                    "delta": int(delta or 0),
                }
            )
        except Exception:
            logger.debug("training event failed", exc_info=True)
    return out


async def _apply_training_delta(
    *,
    event: Any,
    guild_id: int,
    user_id: int,
    style_id: str,
    bond_xp_gained: int,
    bond_level: int | None = None,
) -> int:
    if _json_mode_enabled():
        eid = int(getattr(event, "id", 0) or 1)
        mult = _parse_multipliers(getattr(event, "character_multipliers_json", None)).get(
            style_id, 1.0
        )
        scope = (getattr(event, "scope", "") or "").strip().lower()
        if scope == "global":
            from utils.bonds import tier_for_level
            lvl = max(1, int(bond_level or 1))
            tier = tier_for_level(lvl)
            tp = 2 * max(1, min(5, tier))
            delta = max(1, int(round(tp * float(mult))))
        else:
            base = 1 + max(0, min(50, int(bond_xp_gained)))
            delta = max(1, int(round(base * float(mult))))
        data = await _read_store()
        contrib_all = data.setdefault("contributions", {})
        if not isinstance(contrib_all, dict):
            contrib_all = {}
            data["contributions"] = contrib_all
        bucket = contrib_all.setdefault(str(int(eid)), {})
        if not isinstance(bucket, dict):
            bucket = {}
            contrib_all[str(int(eid))] = bucket
        key = f"{int(guild_id)}:{int(user_id)}:{(style_id or '').strip().lower()}"
        prev = int(bucket.get(key) or 0)
        bucket[key] = int(prev + delta)
        await _write_store(data)
        await resolve_event_if_needed(event_id=eid)
        return int(delta)
    from utils.models import GlobalQuestContribution

    eid = int(getattr(event, "id", 0))
    mult = _parse_multipliers(getattr(event, "character_multipliers_json", None)).get(
        style_id, 1.0
    )
    scope = (getattr(event, "scope", "") or "").strip().lower()
    if scope == "global":
        # Bond image tier 0–5 (Soulbound = tier 5); training = 2 × tier, min 2 (tier 0).
        from utils.bonds import tier_for_level

        lvl = max(1, int(bond_level or 1))
        tier = tier_for_level(lvl)
        tp = 2 * max(1, min(5, tier))
        delta = max(1, int(round(tp * float(mult))))
    else:
        base = 1 + max(0, min(50, int(bond_xp_gained)))
        delta = max(1, int(round(base * float(mult))))

    if select is None:
        return 0

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution)
            .where(GlobalQuestContribution.event_id == eid)
            .where(GlobalQuestContribution.guild_id == guild_id)
            .where(GlobalQuestContribution.user_id == user_id)
            .where(GlobalQuestContribution.style_id == style_id)
            .limit(1)
        )
        row = res.scalar_one_or_none()
        now = _now()
        if row is None:
            row = GlobalQuestContribution(
                event_id=eid,
                guild_id=guild_id,
                user_id=user_id,
                style_id=style_id,
                training_points=int(delta),
                updated_at=now,
            )
            session.add(row)
        else:
            row.training_points = int(
                getattr(row, "training_points", 0) or 0
            ) + int(delta)
            row.updated_at = now
        await session.commit()

    await resolve_event_if_needed(event_id=eid)
    return int(delta)


async def resolve_event_if_needed(*, event_id: int) -> None:
    """Complete or fail event; apply rewards once."""
    if _json_mode_enabled():
        data = await _read_store()
        events = data.get("events") or []
        ev: dict[str, Any] | None = None
        idx = -1
        for i, row in enumerate(events):
            if not isinstance(row, dict):
                continue
            if int(row.get("id") or 0) == int(event_id):
                ev = row
                idx = i
                break
        if ev is None:
            return
        if bool(ev.get("resolution_applied", False)):
            return
        if str(ev.get("status") or "").strip().lower() != "active":
            return
        scope = (str(ev.get("scope") or "global").strip().lower() or "global")
        eg = ev.get("guild_id")
        target = max(1, int(ev.get("target_training_points") or 1))
        ends = _from_iso(ev.get("ends_at"))
        if scope == "guild" and eg is not None:
            total = await _sum_training(event_id=event_id, guild_id=int(eg))
        else:
            total = await _sum_training(event_id=event_id, guild_id=None)
        now = _now()
        success = total >= target
        timed_out = ends is not None and now > ends
        if not success and not timed_out:
            return
        reward = int(ev.get("reward_points") or 0)
        failure = int(ev.get("failure_points") or 0)
        grant_badge = bool(ev.get("grant_success_badge", True))
        emoji = str(ev.get("success_badge_emoji") or "🏆")[:16]
        label = str(ev.get("success_badge_label") or ev.get("title") or "Quest")[:120]
        slug = str(ev.get("slug") or "")
        badge_key = f"gq:{slug}:{int(event_id)}"[:128]
        display = f"{emoji} {label}".strip()
        ev["status"] = "completed_success" if success else "completed_fail"
        ev["resolution_applied"] = True
        if idx >= 0:
            events[idx] = ev
            data["events"] = events
        await _write_store(data)
        if success:
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=reward,
                reason="global_quest_success",
            )
            if grant_badge:
                await _grant_badges_to_contributors(
                    event_id=event_id,
                    badge_key=badge_key,
                    display_text=display,
                )
        elif timed_out:
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=failure,
                reason="global_quest_fail",
            )
        return
    from utils.models import GlobalQuestEvent

    if select is None:
        return

    Session = get_sessionmaker()
    async with Session() as session:
        ev = await session.get(GlobalQuestEvent, int(event_id))
        if ev is None or getattr(ev, "resolution_applied", False):
            return
        if getattr(ev, "status", "") != "active":
            return

        scope = (getattr(ev, "scope", "") or "").strip().lower()
        eg = getattr(ev, "guild_id", None)
        target = max(1, int(getattr(ev, "target_training_points", 1) or 1))
        ends = getattr(ev, "ends_at", None)
        if ends and getattr(ends, "tzinfo", None) is None:
            ends = ends.replace(tzinfo=timezone.utc)

        if scope == "guild" and eg is not None:
            total = await _sum_training(event_id=event_id, guild_id=int(eg))
        else:
            total = await _sum_training(event_id=event_id, guild_id=None)

        now = _now()
        success = total >= target
        timed_out = ends is not None and now > ends

        if not success and not timed_out:
            return

        if success:
            await session.execute(
                update(GlobalQuestEvent)
                .where(GlobalQuestEvent.id == int(event_id))
                .values(
                    status="completed_success",
                    resolution_applied=True,
                    updated_at=now,
                )
            )
            await session.commit()
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=int(getattr(ev, "reward_points", 0) or 0),
                reason="global_quest_success",
            )
            if getattr(ev, "grant_success_badge", True):
                emoji = (getattr(ev, "success_badge_emoji", None) or "🏆")[:16]
                label = (getattr(ev, "success_badge_label", None) or getattr(ev, "title", "Quest"))[:120]
                badge_key = f"gq:{getattr(ev, 'slug', '')}:{event_id}"[:128]
                display = f"{emoji} {label}".strip()
                await _grant_badges_to_contributors(
                    event_id=event_id,
                    badge_key=badge_key,
                    display_text=display,
                )
            return

        if timed_out:
            await session.execute(
                update(GlobalQuestEvent)
                .where(GlobalQuestEvent.id == int(event_id))
                .values(
                    status="completed_fail",
                    resolution_applied=True,
                    updated_at=now,
                )
            )
            await session.commit()
            await _apply_points_to_contributors(
                event_id=event_id,
                delta=int(getattr(ev, "failure_points", 0) or 0),
                reason="global_quest_fail",
            )


async def _apply_points_to_contributors(
    *,
    event_id: int,
    delta: int,
    reason: str,
) -> None:
    if delta == 0:
        return
    from utils.models import GlobalQuestContribution

    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution.user_id)
            .where(GlobalQuestContribution.event_id == int(event_id))
            .distinct()
        )
        uids = [int(r[0]) for r in res.all()]
    for uid in uids:
        try:
            await adjust_points(
                guild_id=GLOBAL_GUILD_ID,
                user_id=uid,
                delta=delta,
                reason=reason,
                meta={"event_id": event_id},
            )
        except Exception:
            logger.exception("global quest points uid=%s", uid)


async def _grant_badges_to_contributors(
    *,
    event_id: int,
    badge_key: str,
    display_text: str,
) -> None:
    from utils.models import GlobalQuestContribution, UserProfileBadge

    if select is None:
        return
    Session = get_sessionmaker()
    async with Session() as session:
        res = await session.execute(
            select(GlobalQuestContribution.user_id)
            .where(GlobalQuestContribution.event_id == int(event_id))
            .distinct()
        )
        uids = [int(r[0]) for r in res.all()]
        now = _now()
        for uid in uids:
            try:
                existing = await session.execute(
                    select(UserProfileBadge)
                    .where(UserProfileBadge.user_id == uid)
                    .where(UserProfileBadge.badge_key == badge_key)
                    .limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    continue
                session.add(
                    UserProfileBadge(
                        user_id=uid,
                        badge_key=badge_key,
                        display_text=display_text[:200],
                        source_event_id=int(event_id),
                        created_at=now,
                    )
                )
            except Exception:
                logger.debug("badge grant skip uid=%s", uid, exc_info=True)
        await session.commit()


async def list_user_badges(*, user_id: int, limit: int = 20) -> list[str]:
    """Display strings for /inspect."""
    from utils.models import UserProfileBadge

    if select is None:
        return []
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(UserProfileBadge.display_text)
                .where(UserProfileBadge.user_id == int(user_id))
                .order_by(UserProfileBadge.created_at.desc())
                .limit(limit)
            )
            return [str(r[0]) for r in res.all() if r[0]]
        except Exception:
            return []


def _apply_admin_body_to_event(ev: dict[str, Any], body: dict[str, Any], *, now: datetime) -> None:
    bid = body.get("id")
    if bid is not None and str(bid).strip().lstrip("-").isdigit():
        ev["id"] = int(bid)
    ev["slug"] = str(body.get("slug") or ev.get("slug") or "global-quest")[:64]
    ev["title"] = str(body.get("title") or ev.get("title") or "Untitled")[:200]
    ev["description"] = str(body.get("description") or "")
    ev["image_url"] = body.get("image_url") if "image_url" in body else ev.get("image_url")
    ev["image_url_secondary"] = (
        body.get("image_url_secondary")
        if "image_url_secondary" in body
        else ev.get("image_url_secondary")
    )
    scope = str(body.get("scope") or ev.get("scope") or "global").strip().lower()
    ev["scope"] = scope if scope in {"global", "guild"} else "global"
    gid = body.get("guild_id")
    if "guild_id" in body:
        ev["guild_id"] = (
            int(gid) if gid is not None and str(gid).strip().lstrip("-").isdigit() else None
        )
    elif "guild_id" not in ev:
        ev["guild_id"] = None
    ev["starts_at"] = str(body.get("starts_at") or ev.get("starts_at") or _iso(now))
    ev["ends_at"] = str(body.get("ends_at") or ev.get("ends_at") or _iso(now))
    if "activated_at" in body:
        ev["activated_at"] = body.get("activated_at")
    ev["target_training_points"] = int(
        body.get("target_training_points") or ev.get("target_training_points") or 100000
    )
    mult = body.get("character_multipliers")
    if mult is None:
        mult = ev.get("character_multipliers") or {}
    ev["character_multipliers"] = mult if isinstance(mult, dict) else {}
    ev["status"] = str(body.get("status") or ev.get("status") or "draft").strip().lower()
    ev["reward_points"] = int(body.get("reward_points") or ev.get("reward_points") or 0)
    ev["failure_points"] = int(body.get("failure_points") or ev.get("failure_points") or 0)
    ev["success_badge_emoji"] = str(
        body.get("success_badge_emoji") or ev.get("success_badge_emoji") or "🏆"
    )[:16]
    ev["success_badge_label"] = str(
        body.get("success_badge_label") or ev.get("success_badge_label") or ev["title"]
    )[:120]
    ev["grant_success_badge"] = bool(
        body.get("grant_success_badge", ev.get("grant_success_badge", True))
    )
    ev["resolution_applied"] = bool(ev.get("resolution_applied", False))


async def json_admin_list_events() -> list[dict[str, Any]]:
    data = await _read_store()
    rows: list[dict[str, Any]] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        out = dict(ev)
        out["character_multipliers"] = ev.get("character_multipliers") or {}
        rows.append(out)
    rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
    return rows


async def json_admin_save_event(*, body: dict[str, Any]) -> int:
    data = await _read_store()
    events = [dict(e) for e in (data.get("events") or []) if isinstance(e, dict)]
    now = _now()
    bid = body.get("id")
    target_id = int(bid) if bid is not None and str(bid).strip().lstrip("-").isdigit() else 0

    for i, row in enumerate(events):
        if int(row.get("id") or 0) == target_id:
            _apply_admin_body_to_event(row, body, now=now)
            events[i] = row
            data["events"] = events
            await _write_store(data)
            return int(row["id"])

    new_id = max((int(e.get("id") or 0) for e in events), default=0) + 1
    ev = _default_event_dict(event_id=new_id)
    _apply_admin_body_to_event(ev, body, now=now)
    events.append(ev)
    data["events"] = events
    await _write_store(data)
    return int(new_id)


async def json_admin_activate_event(*, event_id: int) -> tuple[str | None, str | None]:
    data = await _read_store()
    events = data.get("events") or []
    ev: dict[str, Any] | None = None
    idx = -1
    for i, row in enumerate(events):
        if not isinstance(row, dict):
            continue
        if int(row.get("id") or 0) == int(event_id):
            ev = dict(row)
            idx = i
            break
    if ev is None:
        return None, None
    now = _now()
    ev["status"] = "active"
    ev["starts_at"] = _iso(now)
    ev["activated_at"] = _iso(now)
    ends = _from_iso(ev.get("ends_at"))
    if ends is None or ends <= now:
        ev["ends_at"] = _iso(now + timedelta(days=30))
    ev["resolution_applied"] = False
    if idx >= 0:
        events[idx] = ev
        data["events"] = events
    await _write_store(data)
    return ev.get("activated_at"), ev.get("ends_at")


async def json_admin_cancel_event(*, event_id: int) -> bool:
    data = await _read_store()
    events = data.get("events") or []
    for i, row in enumerate(events):
        if not isinstance(row, dict):
            continue
        if int(row.get("id") or 0) != int(event_id):
            continue
        ev = dict(row)
        ev["status"] = "cancelled"
        ev["resolution_applied"] = True
        events[i] = ev
        data["events"] = events
        await _write_store(data)
        return True
    return False


async def json_admin_delete_event(*, event_id: int) -> bool:
    data = await _read_store()
    events = [e for e in (data.get("events") or []) if isinstance(e, dict)]
    before = len(events)
    events = [e for e in events if int(e.get("id") or 0) != int(event_id)]
    if len(events) == before:
        return False
    contrib = data.get("contributions")
    if isinstance(contrib, dict) and str(int(event_id)) in contrib:
        del contrib[str(int(event_id))]
        data["contributions"] = contrib
    if not events:
        events = [_default_event_dict(event_id=1)]
        data["contributions"] = {}
    data["events"] = events
    await _write_store(data)
    return True
