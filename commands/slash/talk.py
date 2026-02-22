# commands/slash/talk.py
from __future__ import annotations

import asyncio
import logging
import time
import random
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

from core.access import decide_ai_access
from core.ai_gateway import request_text
from core.entitlements import get_entitlements
from core.ui_messages import send_error, send_warning, send_info

from utils.talk_memory import load_memory_lines, append_memory_exchange, clear_memory
from utils.ai_limits import get_guild_limiter, get_user_limiter
from utils.audit import audit_log
from utils.quests import apply_quest_event
from utils.AI_penalties import is_user_penalized, record_cooldown_strike
from utils.storage import get_guild_settings
from utils.premium import get_premium_tier, get_product_caps
from utils.emotion_images import (
    bond_image_url_for_level,
    character_emotion_image_url,
    character_has_bond_images,
    character_has_emotion_images,
    emotion_image_url_for_key,
    parse_emotion_tag,
)
from utils.media_assets import resolve_embed_image_url, fetch_embed_image_as_file, get_discord_file_for_asset
from utils.emotion_predictor import predict_emotion, detect_topics

from utils.talk_prompts import normalize_mode, build_talk_system_prompt, build_active_topic_block, build_awareness_block
from utils.mood_tracker import advance_mood_turn, build_mood_prompt_block, analyze_mood_background
from utils.world_events import build_world_events_prompt_block
from utils.world_lore import build_world_context_block, build_reverse_relationships_block
from utils.character_memory import (
    load_memories, extract_keyword_memories, save_memory,
    build_memory_prompt_block, extract_ai_memories_background,
)
from utils.safety import safety_gate
from utils.metrics import MetricsTimer, emit
from utils.bonds import DAILY_XP_CAP_PER_CHARACTER, level_from_xp, title_for_level, tier_for_level
from utils.bonds_store import add_bond_xp, get_bond
from utils.bonds_prompt import build_bond_prompt_context, bond_system_lines
from utils.character_registry import BASE_STYLE_IDS
from utils.character_store import owns_style
from utils.reporting import send_report
from utils.character_streak import record_character_talk
from utils.leaderboard import update_all_periods, CATEGORY_TALK, CATEGORY_BOND, GLOBAL_GUILD_ID
from utils.analytics import track_ai_call
from core.kai_mascot import (
    embed_kailove,
    get_kai_first_talk_message,
    get_kai_bond_level_message,
)

# (Internals still use the style system; user-facing wording uses "character")
from utils.character_registry import get_style, merge_pack_payload, STYLE_DEFS
from utils.pack_badges import badges_for_style_id
from utils.character_store import owns_style, load_state, get_all_owned_style_ids
from utils.packs_store import list_custom_packs, normalize_style_id


logger = logging.getLogger("bot.talk")

# -----------------------
# Budget-safe memory knobs
# -----------------------
MEMORY_TTL_SECONDS = 7 * 24 * 3600   # 7 days
MEMORY_MAX_LINES = 4                # 2 user/assistant turns (2 exchanges)
MEMORY_HEADER = "MEMORY (recent conversation context; may be incomplete):"

# -----------------------
# Autocomplete: /talk character dropdown
# -----------------------
AUTOCOMPLETE_MAX = 25

# -----------------------
# Bond Milestones
# -----------------------
BOND_MILESTONES = (3, 5, 10, 15, 20)


def _milestone_crossed(old_level: int, new_level: int) -> int | None:
    """Returns the highest milestone reached this update, or None."""
    if new_level <= old_level:
        return None
    reached = [m for m in BOND_MILESTONES if old_level < m <= new_level]
    return reached[-1] if reached else None


async def append_character_footer(
    text: str,
    *,
    style_id: str,
    bond_footer: str | None = None,
    emotion_footer: str | None = None,
) -> str:
    """
    Adds a small footer showing character rarity (preferred) or model fallback,
    plus optional bond info. Designed to preserve immersion.
    """
    style_obj = get_style(style_id)
    label = style_obj.display_name if style_obj else style_id
    rarity = getattr(style_obj, "rarity", None) if style_obj else None

    out = (text or "").rstrip()

    if rarity:
        out += f"\n\n*Rarity: {rarity}*"
    else:
        out += f"\n\n*Rarity: Custom*"

    badge = await badges_for_style_id(style_id)
    out += f"\n*Character: {label}*"
    if badge:
        out += f"\n*{badge}*"

    if emotion_footer:
        out += f"\n*Emotion: {emotion_footer}*"

    if bond_footer:
        out += f"\n*{bond_footer}*"

    return out


def enforce_limits(text: str, *, max_paragraphs: int = 3, max_chars: int = 1900) -> str:
    text = (text or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if len(paragraphs) > max_paragraphs:
        paragraphs = paragraphs[:max_paragraphs]
        text = "\n\n".join(paragraphs).strip() + "\n…(truncated)"

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…(truncated)"

    return text


async def get_answer_paragraph_limit(user_id: int) -> int:
    tier = await get_premium_tier(user_id)
    return 6 if tier == "pro" else 3


async def get_answer_char_limit(user_id: int) -> int:
    tier = await get_premium_tier(user_id)
    return 1900 if tier == "pro" else 500


async def ac_talk_character(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /talk character:
    - always includes base: fun, serious
    - includes user's owned custom styles (global inventory)
    """
    cur = (current or "").strip().lower()

    style_ids: list[str] = ["fun", "serious"]

    try:
        st = await asyncio.wait_for(load_state(interaction.user.id), timeout=2.0)
        if st and getattr(st, "owned_custom", None):
            style_ids.extend(list(st.owned_custom))
    except Exception:
        pass

    # de-dupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for sid in style_ids:
        sid = (sid or "").strip().lower()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)

    # filter
    if cur:
        filtered: list[str] = []
        for sid in deduped:
            s = get_style(sid)
            dn = (s.display_name.lower() if s and getattr(s, "display_name", None) else "")
            if cur in sid.lower() or (dn and cur in dn):
                filtered.append(sid)
        deduped = filtered

    choices: list[app_commands.Choice[str]] = []
    for sid in deduped[:AUTOCOMPLETE_MAX]:
        s = get_style(sid)
        if s:
            rarity = getattr(s, "rarity", None)
            label = f"{s.display_name}  [{rarity}]" if rarity else s.display_name
        else:
            label = sid
        choices.append(app_commands.Choice(name=label[:100], value=sid))

    return choices


class SlashTalk(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _send_private(self, interaction: discord.Interaction, content: str) -> None:
        """Always-ephemeral helper (works whether we've deferred or already responded)."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            pass

    async def _send_reply(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        files: list[discord.File] | None = None,
        ephemeral: bool,
    ) -> discord.Message | None:
        """Safely send a reply (supports embeds and files).

        - If deferred/already responded -> followup.send (returns a Message/WebhookMessage)
        - Else -> response.send_message then original_response (returns Message)
        """
        try:
            send_content = content if content is not None else ""
            # Prefer 'embeds' if provided; otherwise fall back to single 'embed'.
            payload_embeds: list[discord.Embed] | None = None
            if embeds is not None:
                payload_embeds = embeds
            elif embed is not None:
                payload_embeds = [embed]

            kwargs: dict = {"content": send_content, "embeds": payload_embeds, "ephemeral": ephemeral}
            if files:
                kwargs["files"] = files

            if interaction.response.is_done():
                return await interaction.followup.send(**kwargs)
            await interaction.response.send_message(**kwargs)
            try:
                return await interaction.original_response()
            except Exception:
                return None
        except Exception:
            logger.exception("Failed sending /talk reply")
            return None
    async def talk_forget(self, interaction: discord.Interaction, character: str | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        tier = await get_premium_tier(interaction.user.id)
        if tier != "pro":
            await interaction.response.send_message("⭐ Memory is a **Pro** feature.", ephemeral=True)
            return

        style_id = (character or "").strip().lower() or None

        existed = await clear_memory(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            style_id=style_id,  # None = clear all
        )

        if style_id:
            await interaction.response.send_message(
                "🧠 Memory cleared for that character." if existed else "🧠 No memory saved for that character yet.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🧠 All /talk memory cleared." if existed else "🧠 No memory saved yet.",
                ephemeral=True,
            )

    # ---------------------------
    # /talk
    # ---------------------------
    @app_commands.command(name="talk", description="Talk to a character")
    @app_commands.describe(
        prompt="What do you want to say?",
        public="If true, the reply is posted publicly in this channel (default: false)",
        character="Optional: pick one of YOUR characters (leave blank to use the server default)",
        mode="How the bot should reply (Normal, Roleplay, Scene, Texting)",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Normal", value="chat"),
        app_commands.Choice(name="Roleplay", value="rp"),
        app_commands.Choice(name="Scene", value="scene"),
        app_commands.Choice(name="Texting", value="texting"),
    ])
    @app_commands.autocomplete(character=ac_talk_character)
    async def talk(
        self,
        interaction: discord.Interaction,
        prompt: str,
        public: bool = False,
        character: str | None = None,
        mode: app_commands.Choice[str] | None = None,
    ):
        if interaction.guild is None:
            await send_error(interaction, "Use this command in a server, not DMs.")
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        chosen_mode = normalize_mode(mode.value if mode else None)

        prompt = (prompt or "").strip()
        if not prompt:
            await send_warning(interaction, "Please type something to say.")
            return
        if len(prompt) > 4000:
            await send_warning(interaction, "Message too long (max 4000 chars).")
            return

        # Decide visibility ONCE at the start.
        answer_ephemeral = not bool(public)
        start = time.perf_counter()

        # ---- Access checks (centralized) ----
        decision = await decide_ai_access(interaction, command_key="talk", user_text=prompt)
        if not decision.allowed:
            audit_log(
                "ASK_DENIED",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="talk",
                result="denied",
                reason=decision.reason or (decision.message or "denied"),
                fields={"prompt_len": len(prompt), "public": bool(public)},
            )
            await send_error(interaction, decision.message or "Not allowed.")
            return

        # ---- Penalty lock check ----
        locked, remaining = await is_user_penalized(guild_id, user_id)
        if locked:
            audit_log(
                "ASK_DENIED",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="talk",
                result="denied",
                reason="penalized",
                fields={"retry_after_s": remaining, "public": bool(public)},
            )
            await send_error(interaction, f"⛔ You’re temporarily restricted for spam. Try again in **{remaining}s**.")
            return

        tier = await get_premium_tier(user_id)
        product_caps = await get_product_caps(user_id)
        memory_max_lines = int(getattr(product_caps, "memory_max_lines", 4) or 4)

        # ---- Premium gating for public responses ----
        if public and tier != "pro":
            public = False
            answer_ephemeral = True
            # This sends a private message; after that, we must not "defer response" again.
            await self._send_private(
                interaction,
                "🔒 Public replies are a **Pro** feature in this server. I’ll answer you privately instead.",
            )

        # (mass-mention guard is handled inside decide_ai_access)

        # ---- Rate limiting (before we spend money) ----
        user_limiter = await get_user_limiter(guild_id)
        guild_limiter = await get_guild_limiter(guild_id)

        u = await user_limiter.check(f"user:{user_id}")
        if not u.allowed:
            ps = await record_cooldown_strike(guild_id, user_id)

            now = int(time.time())
            penalty_applied = bool(ps.is_penalized) or (int(ps.penalty_until or 0) > now)
            penalty_s = max(0, int(ps.penalty_until or 0) - now)

            audit_log(
                "TALK_COOLDOWN_USER",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="talk",
                result="cooldown",
                reason="user_rate_limit",
                fields={
                    "retry_after_s": u.retry_after_seconds,
                    "prompt_len": len(prompt),
                    "penalty_applied": bool(penalty_applied),
                    "penalty_s": int(penalty_s or 0),
                    "public": bool(public),
                },
            )

            msg = f"⏳ You’re on cooldown. Try again in **{u.retry_after_seconds}s**."
            if penalty_applied:
                msg += f"\n⛔ Repeated spam detected — you’re restricted for **{penalty_s}s**."

            await send_warning(interaction, msg)
            return

        g = await guild_limiter.check(f"guild:{guild_id}")
        if not g.allowed:
            audit_log(
                "TALK_COOLDOWN_GUILD",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="talk",
                result="cooldown",
                reason="guild_rate_limit",
                fields={"retry_after_s": g.retry_after_seconds, "prompt_len": len(prompt), "public": bool(public)},
            )
            await interaction.response.send_message(
                f"⏳ This server is on cooldown. Try again in **{g.retry_after_seconds}s**.",
                ephemeral=True,
            )
            return

        # ---- Budgets (daily + weekly) are enforced centrally in core.ai_gateway ----

        # ---- Determine effective character ----
        server_settings = await get_guild_settings(guild_id)
        server_style = (server_settings.get("style", "fun") or "fun").strip().lower()
        requested_style = (character or "").strip().lower()

        if requested_style:
            try:
                ok_owned = await owns_style(user_id, requested_style)
            except Exception:
                ok_owned = False

            if not ok_owned:
                await interaction.response.send_message(
                    "❌ You don’t own that character yet. Use `/character collection` or `/character roll` to get more.",
                    ephemeral=True,
                )
                return
            effective_style = requested_style
        else:
            user_active = None
            try:
                st = await load_state(user_id)
                user_active = (st.active_style_id or "").strip().lower() if st else None
            except Exception:
                user_active = None
            effective_style = user_active or server_style or "fun"

        # ---- Bond context (fetch early, build block after style_obj is resolved) ----
        b = None
        _bond_ctx: object = None
        try:
            b = await get_bond(guild_id=guild_id, user_id=user_id, style_id=effective_style)
            if b:
                _bond_ctx = build_bond_prompt_context(xp=int(b.xp or 0), nickname=b.nickname)
        except Exception:
            logger.exception("Failed to load bond context")

        # ---- Safety gate ----
        sg_reason = await safety_gate(guild_id, prompt)
        if sg_reason:
            await self._send_private(interaction, f"🚫 {sg_reason}")
            return

        # ---- Defer (only if we haven't already responded) ----
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True, ephemeral=answer_ephemeral)
        except Exception:
            pass

        try:
            if getattr(u, "remaining", 9999) <= 1:
                try:
                    await interaction.followup.send(
                        "⚠️ Heads up: you’re close to your AI limit for this time window.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

            audit_log(
                "ASK_STARTED",
                guild_id=guild_id,
                channel_id=interaction.channel_id,
                user_id=user_id,
                username=interaction.user.name,
                command="talk",
                result="started",
                fields={
                    "prompt_len": len(prompt),
                    "public": bool(public),
                    "style": effective_style,
                    "style_requested": bool(requested_style),
                    "mode": chosen_mode,
                },
            )

            # ---- 6a: Load & decay mood (before prompt build) ----
            _mood_data: dict | None = None
            try:
                _mood_data = await advance_mood_turn(user_id, effective_style)
            except Exception:
                logger.debug("Mood load skipped", exc_info=True)

            # ---- Build system prompt ----
            max_chars = 1900 if tier == "pro" else 500
            max_paras = 6 if tier == "pro" else 3
            language = (server_settings.get("language", "english") or "english")

            style_obj = get_style(effective_style)
            if not style_obj:
                # Lazy-load: packadmin (shop_only / internal) characters may not
                # be in the in-memory registry after a bot restart.  Search Redis
                # and merge the containing pack so emotions/rarity/images work.
                try:
                    target = normalize_style_id(effective_style)
                    packs = await list_custom_packs(limit=600, include_internal=True, include_shop_only=True)
                    for p in packs or []:
                        if not isinstance(p, dict):
                            continue
                        chars = p.get("characters") or []
                        if not isinstance(chars, list):
                            continue
                        for c in chars:
                            if not isinstance(c, dict):
                                continue
                            sid = normalize_style_id(str(c.get("id") or c.get("style_id") or ""))
                            if sid == target:
                                try:
                                    merge_pack_payload(p)
                                except Exception:
                                    pass
                                break
                except Exception:
                    pass
                style_obj = get_style(effective_style)
            style_obj = style_obj or get_style("fun")
            style_prompt = style_obj.prompt if style_obj and getattr(style_obj, "prompt", None) else "Be playful, helpful, and friendly."

            system = build_talk_system_prompt(
                style_prompt=style_prompt,
                mode=chosen_mode,
                max_chars=max_chars,
                max_paragraphs=max_paras,
                style_obj=style_obj,
            )
            if language:
                system = (system or "").rstrip() + f"\n\nLanguage: {language}"
            if _bond_ctx:
                _secrets = getattr(style_obj, "secrets", None) if style_obj else None
                bond_block = "\n\n# Bond Context\n" + bond_system_lines(_bond_ctx, secrets=_secrets) + "\n"
                system = (system or "").rstrip() + "\n" + bond_block

            # ---- World context injection ----
            try:
                if style_obj:
                    _world_block = build_world_context_block(style_obj)
                    if _world_block:
                        system = system.rstrip() + "\n\n" + _world_block + "\n"
            except Exception:
                logger.debug("World context block skipped", exc_info=True)

            # ---- World events injection ----
            try:
                _we_block = build_world_events_prompt_block(effective_style)
                if _we_block:
                    system = system.rstrip() + "\n\n" + _we_block + "\n"
            except Exception:
                logger.debug("World events block skipped", exc_info=True)

            # ---- 6d: Active topic detection ----
            try:
                _topic_reactions = getattr(style_obj, "topic_reactions", None) if style_obj else None
                _active_topics = detect_topics(prompt, _topic_reactions)
                if _active_topics:
                    system = system.rstrip() + "\n\n" + build_active_topic_block(_active_topics) + "\n"
            except Exception:
                logger.debug("Topic detection skipped", exc_info=True)

            # ---- 6c: Inter-character awareness ----
            try:
                if style_obj and getattr(style_obj, "relationships", None):
                    _owned_ids = await get_all_owned_style_ids(user_id)
                    _awareness = build_awareness_block(style_obj, _owned_ids, STYLE_DEFS)
                    if _awareness:
                        system = system.rstrip() + "\n\n" + _awareness + "\n"
            except Exception:
                logger.debug("Awareness block skipped", exc_info=True)

            # ---- Reverse relationships (what others think of you) ----
            try:
                _reverse_rels = build_reverse_relationships_block(effective_style, STYLE_DEFS)
                if _reverse_rels:
                    system = system.rstrip() + "\n\n" + _reverse_rels + "\n"
            except Exception:
                logger.debug("Reverse relationships block skipped", exc_info=True)

            # ---- 6a: Mood injection ----
            if _mood_data:
                try:
                    system = system.rstrip() + "\n\n" + build_mood_prompt_block(_mood_data) + "\n"
                except Exception:
                    logger.debug("Mood block injection skipped", exc_info=True)

            # ---- 6b: Persistent memory anchors (Pro-only) ----
            _persistent_memories: list[dict] = []
            if tier == "pro":
                try:
                    _persistent_memories = await load_memories(user_id, effective_style)
                except Exception:
                    logger.debug("Persistent memory load skipped", exc_info=True)

                try:
                    _kw_mems = extract_keyword_memories(prompt)
                    for km in _kw_mems:
                        await save_memory(user_id, effective_style, km["key"], km["value"], source="keyword")
                except Exception:
                    logger.debug("Keyword memory extraction skipped", exc_info=True)

                if _persistent_memories:
                    try:
                        _bond_level_for_mem = level_from_xp(int(getattr(b, "xp", 0) or 0)) if b else 0
                        _mem_block = build_memory_prompt_block(_persistent_memories, _bond_level_for_mem)
                        if _mem_block:
                            system = system.rstrip() + "\n\n" + _mem_block + "\n"
                    except Exception:
                        logger.debug("Memory block injection skipped", exc_info=True)

            # ---- Pro-only short memory ----
            prompt_for_model = prompt
            _has_memory = False
            if tier == "pro":
                try:
                    # /talk memory is per user per guild (not per character). This keeps
                    # the short memory consistent even when the user changes characters.
                    mem_lines = await load_memory_lines(
                        guild_id=guild_id,
                        user_id=user_id,
                        style_id=None,
                        ttl_seconds=MEMORY_TTL_SECONDS,
                        max_items=memory_max_lines,
                    )
                    if not isinstance(mem_lines, list):
                        mem_lines = []
                    mem_lines = [str(x) for x in mem_lines if str(x).strip()]
                except Exception:
                    mem_lines = []

                if mem_lines:
                    _has_memory = True
                    mem_lines = mem_lines[-memory_max_lines:]
                    prompt_for_model = (
                        f"{MEMORY_HEADER}\n"
                        + "\n".join(mem_lines)
                        + "\n\nUSER MESSAGE:\n"
                        + prompt
                    )

            # ---- AI call (via core gateway) ----
            ent = await get_entitlements(user_id=user_id, guild_id=guild_id)

            _actual_model = config.OPENAI_MODEL if tier == "pro" else getattr(config, "OPENAI_MODEL_FREE", config.OPENAI_MODEL)
            mt = MetricsTimer(
                "talk",
                guild_id,
                user_id,
                input_chars=len(prompt_for_model),
                model=_actual_model,
            )

            resp = await request_text(
                guild_id=guild_id,
                user_id=user_id,
                tier=tier,
                mode="talk",
                system=system,
                user_prompt=prompt_for_model,
                max_output_tokens=ent.max_output_tokens_talk,
                timeout_s=float(getattr(config, "OPENAI_TIMEOUT_S", 20.0) or 20.0),
                character_id=effective_style,
                has_memory=_has_memory,
            )
            if not resp.ok:
                emit(mt.finish(ok=False, error_type=resp.error_type or "AIGatewayError"))
                await self._send_private(interaction, resp.user_message or "⚠️ Something went wrong.")
                return

            text = resp.text or ""

            # --- Parse the LLM emotion tag before anything else ---
            text, llm_emotion = parse_emotion_tag(text)

            emit(mt.finish(ok=True, output_chars=len(text or "")))

            # ---- Enforce output limits + footer ----
            max_chars2 = await get_answer_char_limit(user_id)
            max_paragraphs2 = await get_answer_paragraph_limit(user_id)

            # --- Emotion / bond image decision (for footer + optional image) ---
            BOND_TIER = {
                1: "Friend",
                2: "Trusted",
                3: "Close Companion",
                4: "Devoted",
                5: "Soulbound",
            }

            emotion_footer: str | None = None
            chosen_emotion_image_url: str | None = None

            if effective_style not in ("fun", "serious") and character_has_emotion_images(effective_style, style_obj=style_obj):
                # Use LLM-tagged emotion when available; fall back to keyword prediction.
                if llm_emotion:
                    emotion_key = llm_emotion
                else:
                    try:
                        emotion_key = predict_emotion(effective_style, prompt)
                    except Exception:
                        emotion_key = "neutral"

                emotion_footer = emotion_key.replace("mad", "angry").title() if emotion_key else None

                # 25% chance to show the bond image for the user's current bond TIER (Friend → Soulbound)
                bond_level = level_from_xp(int(getattr(b, "xp", 0) or 0)) if b else 0
                bond_tier = tier_for_level(bond_level) if bond_level else 0
                if bond_tier >= 1 and character_has_bond_images(effective_style, style_obj=style_obj):
                    if random.random() < 0.25:
                        bond_url = bond_image_url_for_level(effective_style, bond_tier, style_obj=style_obj)
                        if bond_url:
                            chosen_emotion_image_url = bond_url
                            tier_name = BOND_TIER.get(bond_tier, f"Bond Tier {bond_tier}")
                            emotion_footer = f"Bond image ({tier_name})"

                if not chosen_emotion_image_url:
                    if llm_emotion:
                        chosen_emotion_image_url = (
                            emotion_image_url_for_key(
                                effective_style,
                                llm_emotion,
                                style_obj=style_obj,
                            )
                            or None
                        )
                    else:
                        chosen_emotion_image_url = (
                            character_emotion_image_url(
                                effective_style,
                                prompt,
                                bond_level=None,
                                bond_chance=0.0,
                                style_obj=style_obj,
                            )
                            or None
                        )

            bond_footer: str | None = None  # ensure defined before use

            # Reserve space for footer (worst-case bond line, plus an emotion line)
            max_bond_footer = "Bond: Lvl 99 (Soulbound) • +2 XP"
            footer_preview = await append_character_footer(
                "",
                style_id=effective_style,
                bond_footer=max_bond_footer,
                emotion_footer="Soulbound bond image",
            )
            reserved = len(footer_preview) + 4
            trim_chars = max(50, max_chars2 - reserved)

            text = enforce_limits(text, max_paragraphs=max_paragraphs2, max_chars=trim_chars)
            text = await append_character_footer(
                text,
                style_id=effective_style,
                bond_footer=bond_footer,
                emotion_footer=emotion_footer,
            )
            text = enforce_limits(text, max_paragraphs=max_paragraphs2, max_chars=max_chars2)

            # ---- Send reply FIRST (so user always gets output) ----
            # Build an embed so the reply always includes the character image + the message.
            desc = text
            if len(desc) > 4096:
                desc = desc[:4093] + "..."
            title = getattr(style_obj, "display_name", None) or getattr(style_obj, "name", None) or "Reply"
            text_embed = discord.Embed(title=title, description=desc)

            # --- Image selection ---
            raw_style_id = getattr(style_obj, "style_id", None) or getattr(style_obj, "character_id", None)
            style_id = str(raw_style_id).lower() if raw_style_id else None

            img_url: str | None = chosen_emotion_image_url or getattr(style_obj, "image_url", None)
            if img_url:
                img_url = resolve_embed_image_url(img_url) or img_url

            logger.info("talk image: style=%s emotion_url=%s img_url=%s", style_id, chosen_emotion_image_url, img_url)

            talk_files: list[discord.File] = []
            talk_attach_name = "talk_image.png"
            if img_url:
                f = await fetch_embed_image_as_file(img_url, filename=talk_attach_name)
                if f:
                    talk_files.append(f)
                    img_url = f"attachment://{talk_attach_name}"
                    logger.info("talk image: fetched as file attachment")
                elif isinstance(img_url, str) and img_url.startswith("asset:"):
                    rel = img_url[len("asset:"):].strip().lstrip("/")
                    af = get_discord_file_for_asset(rel)
                    if af:
                        talk_files.append(af)
                        img_url = f"attachment://{af.filename}"
                    else:
                        logger.warning("talk image: asset file not found for %s", img_url)
                        img_url = None
                else:
                    if not img_url.startswith("https://"):
                        logger.warning("talk image: non-https URL dropped: %s", img_url)
                        img_url = None
                    else:
                        logger.info("talk image: using direct URL (fetch failed)")

            if img_url:
                text_embed.set_image(url=img_url)

            sent_msg: discord.Message | None = await self._send_reply(
                interaction,
                None,
                embeds=[text_embed],
                files=talk_files if talk_files else None,
                ephemeral=answer_ephemeral,
            )

            # ---- Quests (Phase 2 points) ----
            try:
                comps = await apply_quest_event(guild_id=guild_id, user_id=user_id, event="talk")
                if comps:
                    lines = [f"🎁 Quest ready to claim: **{c.name}** (+{c.points} points)" for c in comps]
                    lines.append("Use `/points quests` to claim rewards.")
                    await interaction.followup.send("\n".join(lines), ephemeral=True)
            except Exception:
                # Quests should never block /talk.
                pass

            # ---- Save memory AFTER successful send (Pro only) ----
            if tier == "pro":
                try:
                    await append_memory_exchange(
                        guild_id=guild_id,
                        user_id=user_id,
                        style_id=None,
                        user_text=prompt,
                        assistant_text=text,
                        max_items=memory_max_lines,
                    )
                except Exception:
                    logger.exception("Failed to save talk memory")

            # ---- 6a: Background mood analysis (all users) ----
            try:
                _char_name = getattr(style_obj, "display_name", effective_style) if style_obj else effective_style
                asyncio.create_task(
                    analyze_mood_background(
                        user_id, effective_style, _char_name, prompt, text,
                    )
                )
            except Exception:
                logger.debug("Mood background task spawn failed", exc_info=True)

            # ---- 6b: Background AI memory extraction (Pro-only) ----
            if tier == "pro":
                try:
                    _char_name_mem = getattr(style_obj, "display_name", effective_style) if style_obj else effective_style
                    asyncio.create_task(
                        extract_ai_memories_background(
                            user_id, effective_style, _char_name_mem, prompt, text,
                        )
                    )
                except Exception:
                    logger.debug("Memory background task spawn failed", exc_info=True)

            # ---- Bonds XP AFTER send ----
            bond_footer = None
            milestone_hit = None

            try:
                # Bond XP is only for user-owned, non-server-default characters.
                # This prevents bonding with "fun/serious" or other server defaults.
                _sid = (effective_style or "").strip().lower()
                if _sid in {s.lower() for s in BASE_STYLE_IDS}:
                    # Skip silently.
                    raise StopAsyncIteration
                if not await owns_style(user_id=user_id, style_id=_sid):
                    raise StopAsyncIteration

                bond_xp = 1 + (1 if chosen_mode == "scene" else 0)

                old_xp = int(b.xp or 0) if b else 0
                old_level = level_from_xp(old_xp)

                new_xp, _xp_today, capped_hit = await add_bond_xp(
                    guild_id=guild_id,  # guild_id is ignored (bonds are global)
                    user_id=user_id,
                    style_id=effective_style,
                    amount=bond_xp,
                )

                new_level = level_from_xp(int(new_xp or 0))
                title = title_for_level(new_level)

                gained = max(0, int(new_xp or 0) - int(old_xp or 0))
                bond_footer = f"Bond: Lvl {new_level} ({title}) • +{gained} XP"
                milestone_hit = _milestone_crossed(old_level, new_level)
                
                # Update leaderboard for bond XP (global + server)
                try:
                    from utils.bonds_store import list_bonds_for_user
                    bonds = await list_bonds_for_user(user_id)
                    total_bond_xp = sum(int(getattr(b, "xp", 0) or 0) for b in bonds)
                    await update_all_periods(
                        category=CATEGORY_BOND,
                        guild_id=GLOBAL_GUILD_ID,
                        user_id=user_id,
                        value=float(total_bond_xp),
                    )
                    if guild_id and int(guild_id) != GLOBAL_GUILD_ID:
                        await update_all_periods(
                            category=CATEGORY_BOND,
                            guild_id=int(guild_id),
                            user_id=user_id,
                            value=float(total_bond_xp),
                        )
                except Exception:
                    pass
                
                # Record character streak (global)
                try:
                    streak, continued = await record_character_talk(
                        user_id=user_id,
                        style_id=effective_style,
                        guild_id=guild_id,
                    )
                    # If this is a brand-new streak, fire off a DM (best-effort, non-blocking)
                    if streak == 1 and not continued:
                        async def _send_started_dm(uid: int, sid: str) -> None:
                            try:
                                from utils.streak_reminders import send_character_streak_started_dm
                                await send_character_streak_started_dm(interaction.client, uid, sid)
                            except Exception:
                                pass
                        asyncio.create_task(_send_started_dm(user_id, effective_style))
                except Exception:
                    pass

            except StopAsyncIteration:
                # Expected: bonding disabled for base or unowned styles.
                pass
            except Exception:
                logger.exception("Failed to award bond XP")

            # If we got a message object, try editing to include bond footer
            if bond_footer and sent_msg is not None and not answer_ephemeral:
                try:
                    embed.set_footer(text=bond_footer)
                    await sent_msg.edit(embed=embed)
                except Exception:
                    logger.exception("Failed to edit message with bond footer")
                    # fallback: private bond line
                    try:
                        await interaction.followup.send(f"*{bond_footer}*", ephemeral=True)
                    except Exception:
                        pass
            elif bond_footer:
                # ephemerals can't always be edited reliably -> just send bond info privately
                try:
                    await interaction.followup.send(f"*{bond_footer}*", ephemeral=True)
                except Exception:
                    pass

            # ---- Milestone ping (always private) ----
            if milestone_hit:
                try:
                    s = get_style(effective_style)
                    name = s.display_name if s else effective_style
                    # derive new level again safely
                    # (we don't need exact number if something changed; but we still try)
                    try:
                        b2 = await get_bond(guild_id=guild_id, user_id=user_id, style_id=effective_style)  # guild_id ignored (bonds are global)
                        lvl = level_from_xp(int(b2.xp or 0)) if b2 else None
                    except Exception:
                        lvl = None

                    if lvl is not None:
                        t = title_for_level(lvl)
                        await interaction.followup.send(
                            f"🎉 Bond leveled up with **{name}** → **Lvl {lvl} ({t})**!",
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(
                            f"🎉 Bond milestone reached with **{name}**!",
                            ephemeral=True,
                        )
                except Exception:
                    pass

            # ---- KAI mascot: Kailove on first talk or bond level up ----
            if bond_footer:
                try:
                    s = get_style(effective_style)
                    name = s.display_name if s else effective_style
                    first_talk = old_xp == 0 and int(new_xp or 0) > 0
                    level_up = new_level > old_level
                    if first_talk:
                        msg = get_kai_first_talk_message(name)
                        await interaction.followup.send(embed=embed_kailove(msg), ephemeral=True)
                    elif level_up:
                        title = title_for_level(new_level)
                        msg = get_kai_bond_level_message(title, name)
                        await interaction.followup.send(embed=embed_kailove(msg), ephemeral=True)
                except Exception:
                    logger.exception("KAI bond followup failed")

            # Usage recording happens in core.ai_gateway after success.
            
            # Update leaderboard for talk calls (global + server)
            try:
                await update_all_periods(
                    category=CATEGORY_TALK,
                    guild_id=GLOBAL_GUILD_ID,
                    user_id=user_id,
                    value=1.0,
                )
                if guild_id and int(guild_id) != GLOBAL_GUILD_ID:
                    await update_all_periods(
                        category=CATEGORY_TALK,
                        guild_id=int(guild_id),
                        user_id=user_id,
                        value=1.0,
                    )
            except Exception:
                pass

            # ---- Reporting must NEVER block the user response ----
            try:
                await send_report(
                    bot=self.bot,
                    guild_id=guild_id,
                    title="Talk command used",
                    description=(
                        f"**User:** <@{user_id}>\n"
                        f"**Channel:** <#{interaction.channel_id}>\n"
                        f"**Command:** /talk\n\n"
                        f"**Prompt:**\n{prompt[:800]}\n\n"
                        f"**Response:**\n{text[:800]}"
                    ),
                )
            except Exception:
                pass


        except Exception as e:
            logger.exception("/talk failed", exc_info=e)
            try:
                await interaction.edit_original_response(content="⚠️ Something went wrong while generating this response. Please try again.")
            except Exception:
                pass
            return
async def setup(bot: commands.Bot):
    if bot.get_cog("SlashTalk") is None:
        await bot.add_cog(SlashTalk(bot))

