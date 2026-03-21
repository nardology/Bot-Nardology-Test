"""Weekly AI-generated character talk topics (3 discovery paths; +50 pts each).

Rows are keyed by (guild_id, user_id, style_id, week_id). Quest progress uses
GLOBAL_GUILD_ID; weekly topic rows use the same guild scope as points (0) for
one consistent row per user/character/week.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from utils.points_store import GLOBAL_GUILD_ID
from utils.db import get_sessionmaker

try:
    from sqlalchemy import select, update  # type: ignore
except Exception:  # pragma: no cover
    select = None  # type: ignore
    update = None  # type: ignore

logger = logging.getLogger("bot.character_weekly_topics")


def current_iso_week_id(dt: datetime | None = None) -> str:
    """ISO week label, e.g. '2026-W11'."""
    d = dt or datetime.now(timezone.utc)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _safe_json_list(s: str, *, expect_len: int | None = None) -> list[Any]:
    try:
        v = json.loads(s or "[]")
        if isinstance(v, list):
            if expect_len is not None and len(v) != expect_len:
                return []
            return v
    except Exception:
        pass
    return []


def _kw_tokens(s: str) -> set[str]:
    """Same heuristic as /talk daily topic bonus (anti-keyword-stuffing)."""
    out: set[str] = set()
    for raw in (s or "").lower().replace("\n", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) < 4:
            continue
        if w in {
            "that", "this", "with", "from", "have", "your", "youre", "what", "when",
            "they", "them", "just", "like", "really", "about", "would", "could",
        }:
            continue
        out.add(w)
    return out


@dataclass(frozen=True)
class WeeklyTopicsBundle:
    week_id: str
    topics: list[dict[str, str]]
    keywords: list[list[str]]
    hints: list[str]
    topic_version: int
    claimed_mask: int


def _bundle_from_row(row: Any) -> WeeklyTopicsBundle | None:
    if row is None:
        return None
    raw_topics = _safe_json_list(getattr(row, "topics_json", "") or "", expect_len=None)
    topics: list[dict[str, str]] = []
    for item in raw_topics[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("t") or "").strip()
        desc = str(item.get("description") or item.get("d") or "").strip()
        if title:
            topics.append({"title": title, "description": desc})

    kws_raw = _safe_json_list(getattr(row, "keywords_json", "") or "")
    keywords: list[list[str]] = []
    for i in range(3):
        block = kws_raw[i] if i < len(kws_raw) else []
        if isinstance(block, list):
            keywords.append([str(x).strip().lower() for x in block if str(x).strip()][:12])
        else:
            keywords.append([])

    hints_raw = _safe_json_list(getattr(row, "hints_json", "") or "")
    hints = [str(h).strip() for h in hints_raw[:3] if str(h).strip()]
    while len(hints) < 3:
        hints.append("")

    # Pad topics to 3 placeholders for consistent indexing
    while len(topics) < 3:
        topics.append({"title": "", "description": ""})

    return WeeklyTopicsBundle(
        week_id=str(getattr(row, "week_id", "") or ""),
        topics=topics[:3],
        keywords=keywords[:3],
        hints=hints[:3],
        topic_version=int(getattr(row, "topic_version", 1) or 1),
        claimed_mask=int(getattr(row, "claimed_mask", 0) or 0),
    )


async def load_weekly_topics_bundle(
    *,
    user_id: int,
    style_id: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> WeeklyTopicsBundle | None:
    """Load this week's topics row for the user + character, if present."""
    if select is None:
        return None
    sid = (style_id or "").strip().lower()
    if not sid:
        return None
    wid = current_iso_week_id()
    from utils.models import CharacterWeeklyTopics  # noqa: WPS433

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(CharacterWeeklyTopics)
                .where(CharacterWeeklyTopics.guild_id == int(guild_id))
                .where(CharacterWeeklyTopics.user_id == int(user_id))
                .where(CharacterWeeklyTopics.style_id == sid)
                .where(CharacterWeeklyTopics.week_id == wid)
                .limit(1)
            )
            row = res.scalar_one_or_none()
            return _bundle_from_row(row)
        except Exception:
            logger.exception("load_weekly_topics_bundle failed")
            return None


async def is_eligible_for_weekly_topics(
    *,
    user_id: int,
    style_id: str,
    progress_day_key: str | None = None,
) -> bool:
    """Daily quest progress sum > 5 for the given UTC day (default: today) and selected character."""
    from utils.quests import sum_daily_quest_progress_for_day, sum_daily_quest_progress_today  # noqa: WPS433

    st = await _load_state_safe(user_id)
    active = (getattr(st, "active_style_id", "") or "").strip().lower()
    if not active or active != (style_id or "").strip().lower():
        return False
    if progress_day_key:
        total = await sum_daily_quest_progress_for_day(
            user_id=int(user_id),
            day_key=progress_day_key,
        )
    else:
        total = await sum_daily_quest_progress_today(user_id=int(user_id))
    return total > 5


async def _load_state_safe(user_id: int) -> Any:
    try:
        from utils.character_store import load_state  # noqa: WPS433

        return await load_state(user_id)
    except Exception:
        class _Dummy:
            active_style_id = ""

        return _Dummy()


async def generate_weekly_topics_ai(style: Any) -> dict[str, Any] | None:
    """Call OpenAI to produce strict JSON: topics, keywords, hints."""
    try:
        from utils.ai_client import generate_text
        import config
    except Exception:
        return None

    persona = (
        (getattr(style, "prompt", None) or "").strip()
        or "A friendly character."
    )
    name = style.display_name if getattr(style, "display_name", None) else "Character"

    system = (
        "You output a single JSON object only, no markdown. Schema:\n"
        '{"topics":[{"title":"short title","description":"1 sentence"},{"title":"","description":""},{"title":"","description":""}],'
        '"keywords":[[\"kw1\",\"kw2\",...],[...],[...]],'
        '"hints":["one short DM hint line for the user","",""]}\n'
        "Rules:\n"
        "- Exactly 3 topics.\n"
        "- Each keywords array has 4-8 words related to that topic (lowercase).\n"
        "- Hints must NOT mention quests, points, streaks, or Discord. They hint what to talk about in character.\n"
        "- Topics must fit the character's personality.\n"
    )
    user = f"Character name: {name}\nPersona:\n{persona[:4000]}\n"

    model = getattr(config, "OPENAI_MODEL", None) or "gpt-4.1-mini"
    try:
        result = await generate_text(
            system=system,
            user=user,
            max_output_tokens=1200,
            timeout_s=45.0,
            model=model,
        )
    except Exception:
        logger.debug("generate_weekly_topics_ai failed", exc_info=True)
        return None

    if hasattr(result, "text"):
        text = str(getattr(result, "text", "") or "").strip()
    else:
        text = str(result).strip()
    if not text:
        return None
    # Strip markdown fences if any
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        data = json.loads(text)
    except Exception:
        logger.debug("weekly topics JSON parse failed: %s", text[:200])
        return None

    topics = data.get("topics")
    keywords = data.get("keywords")
    hints = data.get("hints")
    if not isinstance(topics, list) or len(topics) != 3:
        return None
    if not isinstance(keywords, list) or len(keywords) != 3:
        return None
    if not isinstance(hints, list) or len(hints) != 3:
        return None

    out_topics: list[dict[str, str]] = []
    for t in topics:
        if not isinstance(t, dict):
            return None
        title = str(t.get("title") or "").strip()
        desc = str(t.get("description") or "").strip()
        if not title:
            return None
        out_topics.append({"title": title[:200], "description": desc[:400]})

    out_kw: list[list[str]] = []
    for block in keywords:
        if not isinstance(block, list):
            return None
        words = [str(x).strip().lower() for x in block if str(x).strip()][:12]
        out_kw.append(words)

    out_hints = [str(h).strip()[:240] for h in hints]

    return {"topics": out_topics, "keywords": out_kw, "hints": out_hints}


async def insert_weekly_topics_row(
    *,
    user_id: int,
    style_id: str,
    payload: dict[str, Any],
    guild_id: int = GLOBAL_GUILD_ID,
) -> bool:
    if select is None:
        return False
    sid = (style_id or "").strip().lower()
    if not sid:
        return False
    wid = current_iso_week_id()
    from utils.models import CharacterWeeklyTopics  # noqa: WPS433

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(CharacterWeeklyTopics)
                .where(CharacterWeeklyTopics.guild_id == int(guild_id))
                .where(CharacterWeeklyTopics.user_id == int(user_id))
                .where(CharacterWeeklyTopics.style_id == sid)
                .where(CharacterWeeklyTopics.week_id == wid)
                .limit(1)
            )
            if res.scalar_one_or_none() is not None:
                return True

            row = CharacterWeeklyTopics(
                guild_id=int(guild_id),
                user_id=int(user_id),
                style_id=sid,
                week_id=wid,
                topics_json=json.dumps(payload.get("topics") or [], separators=(",", ":")),
                keywords_json=json.dumps(payload.get("keywords") or [], separators=(",", ":")),
                hints_json=json.dumps(payload.get("hints") or [], separators=(",", ":")),
                topic_version=1,
                claimed_mask=0,
            )
            session.add(row)
            await session.commit()
            return True
        except Exception:
            logger.exception("insert_weekly_topics_row failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return False


async def ensure_weekly_topics_row(
    *,
    user_id: int,
    style_id: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> WeeklyTopicsBundle | None:
    """Load existing row; if missing and user is eligible, generate and insert."""
    existing = await load_weekly_topics_bundle(user_id=user_id, style_id=style_id, guild_id=guild_id)
    if existing and any(t.get("title") for t in existing.topics):
        return existing

    if not await is_eligible_for_weekly_topics(user_id=user_id, style_id=style_id):
        return existing

    from utils.character_registry import get_style  # noqa: WPS433

    style = get_style(style_id)
    if style is None:
        return None

    payload = await generate_weekly_topics_ai(style)
    if not payload:
        return None

    ok = await insert_weekly_topics_row(user_id=user_id, style_id=style_id, payload=payload, guild_id=guild_id)
    if not ok:
        return None
    return await load_weekly_topics_bundle(user_id=user_id, style_id=style_id, guild_id=guild_id)


def match_weekly_topic_indices(
    user_text: str,
    *,
    bundle: WeeklyTopicsBundle,
) -> list[int]:
    """Return indices of topics matched (0..2) using daily-topic-style gates."""
    user_text = (user_text or "").strip()
    if len(user_text) < 30 or len(user_text.split()) < 6:
        return []

    low = user_text.lower()
    matched: list[int] = []

    for i in range(3):
        topic = bundle.topics[i] if i < len(bundle.topics) else {}
        title = str((topic or {}).get("title") or "").strip()
        desc = str((topic or {}).get("description") or "").strip()
        if not title:
            continue

        t = title.lower()
        trivial = (low == t) or (
            low.replace(".", "").replace("!", "").replace("?", "").strip() == t
        )
        if trivial:
            continue

        meaning_src = " ".join([title, desc] + [" ".join(bundle.keywords[i])])
        meaning_kws = _kw_tokens(meaning_src)
        msg_kws = _kw_tokens(user_text)
        overlap = len(meaning_kws.intersection(msg_kws)) if meaning_kws else 0

        topic_in_text = bool(t) and (t in low)
        ok_topic = (overlap >= 2) or (topic_in_text and len(user_text.split()) >= 10)

        if ok_topic:
            matched.append(i)

    return matched


async def claim_weekly_topic_indices(
    *,
    guild_id: int,
    user_id: int,
    style_id: str,
    indices: list[int],
    store_guild_id: int = GLOBAL_GUILD_ID,
) -> tuple[int, int]:
    """Award +50 per newly claimed index; returns (points_awarded, new_mask)."""
    if not indices or update is None:
        return 0, 0

    wid = current_iso_week_id()
    sid = (style_id or "").strip().lower()
    from utils.models import CharacterWeeklyTopics  # noqa: WPS433

    Session = get_sessionmaker()
    async with Session() as session:
        try:
            res = await session.execute(
                select(CharacterWeeklyTopics)
                .where(CharacterWeeklyTopics.guild_id == int(store_guild_id))
                .where(CharacterWeeklyTopics.user_id == int(user_id))
                .where(CharacterWeeklyTopics.style_id == sid)
                .where(CharacterWeeklyTopics.week_id == wid)
                .limit(1)
                .with_for_update()
            )
            row = res.scalar_one_or_none()
            if row is None:
                return 0, 0

            mask = int(getattr(row, "claimed_mask", 0) or 0)
            running = mask
            awarded = 0
            for i in indices:
                if i < 0 or i > 2:
                    continue
                bit = 1 << i
                if running & bit:
                    continue
                running |= bit
                awarded += 50
            new_mask = running

            if new_mask == mask:
                return 0, mask

            await session.execute(
                update(CharacterWeeklyTopics)
                .where(CharacterWeeklyTopics.guild_id == int(store_guild_id))
                .where(CharacterWeeklyTopics.user_id == int(user_id))
                .where(CharacterWeeklyTopics.style_id == sid)
                .where(CharacterWeeklyTopics.week_id == wid)
                .values(claimed_mask=new_mask)
            )
            await session.commit()

            if awarded > 0:
                from utils.points_store import adjust_points  # noqa: WPS433

                await adjust_points(
                    guild_id=guild_id,
                    user_id=int(user_id),
                    delta=awarded,
                    reason="weekly_character_topic",
                    meta={"week_id": wid, "indices": indices, "style_id": sid},
                )

            return awarded, new_mask
        except Exception:
            logger.exception("claim_weekly_topic_indices failed")
            try:
                await session.rollback()
            except Exception:
                pass
            return 0, 0


def blur_teaser(title: str) -> str:
    t = (title or "").strip()
    if len(t) <= 4:
        return "• • • • •"
    return f"{t[:2]}{'·' * min(8, len(t))}{t[-1]}"


async def weekly_topics_quest_embed_lines(
    *,
    user_id: int,
    style_id: str | None,
) -> list[str]:
    """Lines for /points quests weekly section."""
    if not style_id:
        return ["Select a character to see weekly topic progress."]
    bundle = await load_weekly_topics_bundle(user_id=user_id, style_id=style_id)
    if not bundle or not any(t.get("title") for t in bundle.topics):
        return [
            "No weekly topics yet. Earn **>5** daily quest progress today with your selected character.",
        ]

    lines: list[str] = []
    matched = 0
    for i in range(3):
        if int(bundle.claimed_mask) & (1 << i):
            matched += 1

    lines.append(f"Progress: **{matched}/3** matched *(+50 each)*")
    for i in range(3):
        t = bundle.topics[i].get("title") if i < len(bundle.topics) else ""
        if not (t or "").strip():
            lines.append(f"- Topic {i + 1}: *(pending)*")
            continue
        claimed = bool(int(bundle.claimed_mask) & (1 << i))
        mark = "✅" if claimed else "▫️"
        lines.append(f"{mark} {blur_teaser(t)}")
    return lines


async def get_weekly_hint_for_streak_dm(user_id: int, style_id: str) -> str | None:
    """One rotating hint line for Pro streak DMs (reminder stage)."""
    bundle = await load_weekly_topics_bundle(user_id=user_id, style_id=style_id)
    if not bundle:
        return None
    hints = [h for h in bundle.hints if h.strip()]
    if not hints:
        return None
    d = datetime.now(timezone.utc).weekday()
    return hints[d % len(hints)]
