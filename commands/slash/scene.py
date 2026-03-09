# commands/slash/scene.py
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.metrics import MetricsTimer, emit

from core.access import decide_ai_access
from core.ai_gateway import request_text
from core.ui_messages import send_error, send_warning, send_info

from utils.premium import get_premium_tier

from utils.storage import get_guild_settings
from utils.ai_limits import (
    get_guild_limiter,
    get_user_limiter,
    get_scene_limiter,
    get_summary_limiter,
    get_summary_daily_user_limiter,
    get_summary_daily_guild_limiter,
)

from utils.AI_penalties import is_user_penalized, record_cooldown_strike
from utils.safety import safety_gate

from utils.character_registry import get_style
from utils.character_store import load_state, owns_style

from utils.scene_caps import get_scene_caps

from utils.scene_store import (
    create_scene,
    get_scene,
    end_scene,
    add_scene_line,
    get_recent_scene_lines,
    flip_turn,
    find_active_scene_between,
    list_active_scenes_in_channel,
    count_active_scenes_in_channel,
    count_active_scenes_in_guild,
    count_active_scenes_for_user,
    expire_scene_if_stale,
    expire_stale_scenes_in_channel,
    delete_scene_lines,
)

from utils.backpressure import get_redis_or_none


async def _require_redis_for_scenes(interaction: discord.Interaction) -> bool:
    """Scenes rely on Redis (active sessions + transcript lines).

    If Redis is unavailable, we should gracefully refuse instead of crash-looping.
    """
    try:
        if await get_redis_or_none() is None:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⚠️ Scenes are temporarily unavailable (storage backend unavailable). Please try again later.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Scenes are temporarily unavailable (storage backend unavailable). Please try again later.",
                    ephemeral=True,
                )
            return False
    except Exception:
        # If something is wrong with the Redis client, fail safe.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⚠️ Scenes are temporarily unavailable. Please try again later.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Scenes are temporarily unavailable. Please try again later.",
                    ephemeral=True,
                )
        except Exception:
            pass
        return False
    return True

from utils.scene_prompts import build_scene_system_prompt, build_scene_user_prompt
from utils.quests import apply_quest_event
from utils.reporting import ReportView

logger = logging.getLogger("bot.scene")

# ----- Scene Settings ------
SCENE_SETTING_SUGGESTIONS = [
    "Dragon’s lair — molten gold, scorched stone, echoing roars",
    "Castle corridor — torchlight, steel on stone, tension in the air",
    "Tavern confrontation — crowded, loud, a sudden hush",
    "Moonlit forest — mist, distant howls, hidden eyes",
    "Arena duel — roaring crowd, sand underfoot, high stakes",
    "Throne room — velvet, power, and a dangerous accusation",
    "Dungeon cell — chains, whispers, and a desperate bargain",
    "Ship deck — storm winds, creaking wood, betrayal imminent",
    "Wizard’s tower — floating tomes, crackling wards, forbidden questions",
    "Marketplace chaos — pickpockets, guards, and a public challenge",
]

# 48h TTL for "scene memory"
SCENE_TTL_SECONDS = 48 * 3600

# ---- Cheap safety knobs ----
SCENE_MAX_DIRECTION_CHARS = 400
SCENE_MAX_MESSAGE_CHARS = 800
SCENE_CONTEXT_LINES = 6  # transcript lines included in prompt (cheap)

# ---- Summary knobs (cheaper + gated) ----
SUMMARY_TIMEOUT_S = 20.0
SUMMARY_MAX_TOKENS = 220
SUMMARY_CONTEXT_LINES = 12  # less transcript than full scene turn, keeps it cheap


async def _maintenance_expire_stale(guild_id: int, channel_id: int) -> None:
    """
    Cheap maintenance hook: called on /scene commands in this channel.
    This ends stale scenes (updated_at older than TTL).
    """
    try:
        await expire_stale_scenes_in_channel(
            guild_id=guild_id,
            channel_id=channel_id,
            ttl_seconds=SCENE_TTL_SECONDS,
        )
    except Exception:
        pass


async def ac_scene_setting(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = (current or "").strip().lower()
    out: list[app_commands.Choice[str]] = []
    for s in SCENE_SETTING_SUGGESTIONS:
        if not cur or cur in s.lower():
            name = s if len(s) <= 100 else s[:97] + "..."
            out.append(app_commands.Choice(name=name, value=s))
        if len(out) >= 20:
            break
    return out


def _clamp(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


async def ac_scene_active_for_user_in_channel(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    """
    Autocomplete for scene IDs:
    - only ACTIVE scenes
    - only in THIS channel (because /scene say enforces channel match)
    - only scenes the user participates in
    """
    if interaction.guild is None:
        return []

    guild_id = interaction.guild.id
    channel_id = interaction.channel_id
    user_id = interaction.user.id

    try:
        rows = await list_active_scenes_in_channel(
            guild_id=guild_id,
            channel_id=channel_id,
            limit=25,
        )
    except Exception:
        return []

    cur = (current or "").strip()
    out: list[app_commands.Choice[int]] = []

    for s in rows:
        if not bool(getattr(s, "is_active", True)):
            continue

        if int(user_id) not in (int(s.p1_user_id), int(s.p2_user_id)):
            continue

        sid = int(s.id)

        if cur and not str(sid).startswith(cur):
            continue

        name = f"Scene #{sid} — <@{int(s.p1_user_id)}> vs <@{int(s.p2_user_id)}>"
        if len(name) > 100:
            name = name[:97] + "..."

        out.append(app_commands.Choice(name=name, value=sid))

        if len(out) >= 25:
            break

    return out


async def _resolve_user_style_or_server_default(*, guild_id: int, user_id: int) -> str:
    """
    Use user's selected character if available; else server style; else 'fun'.
    """
    try:
        st = await load_state(user_id)
        active = (st.active_style_id or "").strip().lower() if st else ""
    except Exception:
        active = ""

    if active:
        return active

    server_settings = await get_guild_settings(guild_id)
    server_style = (server_settings.get("style", "fun") or "fun").strip().lower()
    return server_style or "fun"


def _format_scene_line(speaker_name: str, content: str) -> str:
    content = (content or "").strip()
    return f"{speaker_name}: {content}"


def _safe_style_name(style_id: str) -> str:
    s = get_style(style_id)
    return s.display_name if s else style_id


async def _scene_summary_rate_limit(*, guild_id: int, user_id: int, tier: str) -> tuple[bool, str]:
    """
    Keep /scene summary cheap and gated.
    Uses user limiter, but separate keyspace so it doesn't steal normal /scene usage.
    """
    try:
        user_limiter = await get_user_limiter(guild_id)
        key = f"user:{user_id}:scene_summary"
        chk = await user_limiter.check(key)
        if chk.allowed:
            return True, ""
        return False, f"⏳ Summary cooldown. Try again in **{chk.retry_after_seconds}s**."
    except Exception:
        # If limiter fails, don't block summary; concurrency gate still protects you.
        return True, ""


def _extract_generated_text(res) -> str:
    """
    generate_text() returns either:
      - str   (default)
      - AIResult (when return_raw=True)
    This helper handles both.
    """
    if isinstance(res, str):
        return res
    # AIResult-like
    return str(getattr(res, "text", "") or "")


async def _summarize_scene_text(
    *,
    guild_id: int,
    user_id: int,
    scene_id: int,
    language: str,
) -> str:
    """
    On-demand summary.
    - Cheap (small transcript + small token budget)
    - Uses breaker + Redis concurrency (ai_slot)
    - Summary limiter: independent cooldown from normal AI usage
    """
    s = await get_scene(scene_id=scene_id)
    if not s:
        return "Scene not found."

    p1_name = _safe_style_name(s.p1_style_id)
    p2_name = _safe_style_name(s.p2_style_id)

    # ---- Summary-only limiter (cheap gate) ----
    summary_limiter = await get_summary_limiter(guild_id)
    chk = await summary_limiter.check(f"guild:{guild_id}:scene_summary")
    if not chk.allowed:
        return f"⏳ Scene summary is on cooldown. Try again in **{chk.retry_after_seconds}s**."

    lines = await get_recent_scene_lines(scene_id=scene_id, limit=SUMMARY_CONTEXT_LINES)
    transcript: list[str] = []
    for ln in lines:
        speaker = p1_name if int(ln.speaker_user_id) == int(s.p1_user_id) else p2_name
        transcript.append(f"{speaker}: {ln.content}")

    setting = (s.setting or "").strip()

    system = (
        "You are a summarizer for a roleplay scene transcript.\n"
        f"Language: {language}\n"
        "Rules:\n"
        "- Keep it short.\n"
        "- 5-8 bullet points max.\n"
        "- Include: current tension/goal, what each character wants, and the latest state.\n"
        "- Do not invent events not in the transcript.\n"
    )

    user = "Summarize this roleplay scene.\n"
    if setting:
        user += f"\nSETTING: {setting}\n"
    user += "\nTRANSCRIPT:\n" + "\n".join(transcript)

    tier = await get_premium_tier(user_id)

    resp = await request_text(
        guild_id=guild_id,
        user_id=int(s.p1_user_id),
        tier=tier,
        mode="summary",
        system=system,
        user_prompt=user,
        max_output_tokens=SUMMARY_MAX_TOKENS,
        timeout_s=SUMMARY_TIMEOUT_S,
    )
    if not resp.ok:
        return resp.user_message or "⏳ AI is busy right now. Please try again in a minute."

    txt = (resp.text or "").strip()
    # AI usage metrics are recorded centrally in core.ai_gateway (real tokens when available).

    if not txt:
        return "⚠️ Failed to generate summary."
    if len(txt) > 1200:
        txt = txt[:1199].rstrip() + "…"
    return txt


class SlashScene(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    scene = app_commands.Group(name="scene", description="Turn-based roleplay scenes between two users")

    async def _send_private(self, interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            pass

    async def _dm_user(self, user_id: int, content: str | None = None, embed: discord.Embed | None = None) -> bool:
        """Best-effort DM. Returns True if DM likely sent."""
        try:
            u = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if not u:
                return False
            if content:
                await u.send(content)
            if embed is not None:
                await u.send(embed=embed)
            return True
        except Exception:
            return False

    def _scene_is_public(self, s) -> bool:
        """Works with either Boolean or Integer storage."""
        try:
            v = getattr(s, "is_public", False)
            return bool(v) and int(v) != 0
        except Exception:
            return False

    async def _deliver_scene_embed(
        self,
        *,
        s,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        view: discord.ui.View | None,
        guild_id: int,
        channel_id: int,
    ) -> tuple[bool, bool]:
        """
        If public: post in channel.
        If private: DM both participants.
        Returns (delivered_to_p1, delivered_to_p2).
        """
        if self._scene_is_public(s):
            try:
                if view is not None:
                    await channel.send(embed=embed, view=view)
                else:
                    await channel.send(embed=embed)
            except TypeError:
                await channel.send(embed=embed)
            return True, True

        # Private scene => DM both participants (do not attach View for DMs)
        ok1 = await self._dm_user(int(s.p1_user_id), embed=embed)
        ok2 = await self._dm_user(int(s.p2_user_id), embed=embed)
        return ok1, ok2

    # ---------------------------
    # /scene start
    # ---------------------------
    @scene.command(name="start", description="Start a scene with another user (turn-based)")
    @app_commands.describe(
        opponent="Who you want to roleplay with",
        setting="Optional: scene premise / setting",
        public="If true, announce the scene publicly in channel (Pro only). Default: false (private).",
    )
    @app_commands.autocomplete(setting=ac_scene_setting)
    async def start(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
        setting: str | None = None,
        public: bool = False,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id
        opp_id = opponent.id

        await _maintenance_expire_stale(guild_id, channel_id)

        if opp_id == user_id:
            await interaction.response.send_message("Pick someone else (you can’t scene with yourself).", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("Scenes are currently between two human users.", ephemeral=True)
            return

        decision = await decide_ai_access(interaction, command_key="scene", user_text=(setting or ""))
        if not decision.allowed:
            await send_error(interaction, decision.message or "Not allowed.")
            return

        tier = await get_premium_tier(user_id)

        if public and tier != "pro":
            public = False
            await self._send_private(interaction, "🔒 Public scenes are **Pro**. This scene will run in **DMs** instead.")

        setting_s = _clamp(setting or "", 800) if setting else None

        if setting_s:
            deny = await safety_gate(guild_id, setting_s)
            if deny:
                await interaction.response.send_message(f"🚫 {deny}", ephemeral=True)
                return

        caps = await get_scene_caps(user_id)

        active_guild = await count_active_scenes_in_guild(guild_id=guild_id)
        if active_guild >= caps.active_per_guild:
            await interaction.response.send_message(
                f"⛔ This server reached the active scene limit (**{caps.active_per_guild}**). "
                f"End an old one with `/scene end` or upgrade for more.",
                ephemeral=True,
            )
            return

        active_chan = await count_active_scenes_in_channel(guild_id=guild_id, channel_id=channel_id)
        if active_chan >= caps.active_per_channel:
            await interaction.response.send_message(
                f"⛔ This channel reached the active scene limit (**{caps.active_per_channel}**). "
                f"Try another channel or end an old scene with `/scene end`.",
                ephemeral=True,
            )
            return

        active_user = await count_active_scenes_for_user(guild_id=guild_id, user_id=user_id)
        if active_user >= caps.active_per_user:
            await interaction.response.send_message(
                f"⛔ You reached your active scene limit (**{caps.active_per_user}**). "
                f"End an old one with `/scene end` before starting another.",
                ephemeral=True,
            )
            return

        existing = await find_active_scene_between(
            guild_id=guild_id,
            channel_id=channel_id,
            user_a=user_id,
            user_b=opp_id,
        )
        if existing:
            await interaction.response.send_message(
                f"⚠️ You already have an active scene with {opponent.mention} in this channel: **Scene #{existing.id}**.\n"
                f"Use `/scene view scene:{existing.id}` or `/scene say scene:{existing.id}`.",
                ephemeral=True,
            )
            return

        p1_style = await _resolve_user_style_or_server_default(guild_id=guild_id, user_id=user_id)
        p2_style = await _resolve_user_style_or_server_default(guild_id=guild_id, user_id=opp_id)

        try:
            if not await owns_style(user_id, p1_style):
                p1_style = (await get_guild_settings(guild_id)).get("style", "fun") or "fun"
        except Exception:
            p1_style = "fun"

        try:
            if not await owns_style(opp_id, p2_style):
                p2_style = (await get_guild_settings(guild_id)).get("style", "fun") or "fun"
        except Exception:
            p2_style = "fun"

        scene_row = await create_scene(
            guild_id=guild_id,
            channel_id=channel_id,
            creator_user_id=user_id,
            p1_user_id=user_id,
            p1_style_id=p1_style,
            p2_user_id=opp_id,
            p2_style_id=p2_style,
            setting=setting_s,
            turn_user_id=user_id,
            is_public=public,
        )

        p1_name = _safe_style_name(p1_style)
        p2_name = _safe_style_name(p2_style)

        msg = (
            f"🎭 **Scene #{scene_row.id}** started!\n"
            f"**{interaction.user.mention}** as **{p1_name}** vs **{opponent.mention}** as **{p2_name}**\n"
            f"**Turn:** {interaction.user.mention}\n"
            f"Use `/scene say scene:{scene_row.id} direction:\"...\" message:\"...\"`\n"
            f"Tip: `/scene summary scene:{scene_row.id}` anytime.\n"
        )
        if setting_s:
            msg += f"\n**Setting:** {setting_s}"

        if not public:
            dm_msg = (
                f"🎭 **Scene #{scene_row.id}** started in **{interaction.guild.name}**.\n"
                f"You are roleplaying vs {interaction.user.mention}.\n"
                f"Your turns will be delivered via **this DM**.\n\n"
                f"Go to <#{channel_id}> and use:\n"
                f"`/scene say scene:{scene_row.id} direction:\"...\" message:\"...\"`\n"
                f"Tip: `/scene summary scene:{scene_row.id}` anytime."
            )
            if setting_s:
                dm_msg += f"\n\n**Setting:** {setting_s}"

            ok_dm = await self._dm_user(opp_id, dm_msg)
            if not ok_dm:
                await self._send_private(
                    interaction,
                    "⚠️ I couldn’t DM your opponent (their DMs may be closed). They won’t see a private scene unless they enable DMs.",
                )

        await interaction.response.send_message(msg, ephemeral=(not public))

        # ---- Quests (Phase 2 points) ----
        try:
            comps = await apply_quest_event(guild_id=int(guild_id), user_id=int(user_id), event="scene_start")
            if comps:
                lines = [f"🎁 Quest ready to claim: **{c.name}** (+{c.points} points)" for c in comps]
                lines.append("Use `/points quests` to claim rewards.")
                await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception:
            pass

    # ---------------------------
    # /scene view
    # ---------------------------
    @scene.command(name="view", description="View recent lines of a scene")
    @app_commands.describe(scene="Scene ID")
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def view(self, interaction: discord.Interaction, scene: int):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if s.is_active:
            expired = await expire_scene_if_stale(scene_id=int(scene), ttl_seconds=SCENE_TTL_SECONDS)
            if expired:
                await interaction.response.send_message("⏳ This scene expired due to inactivity (48h).", ephemeral=True)
                return

        lines = await get_recent_scene_lines(scene_id=int(scene), limit=10)

        p1_name = _safe_style_name(s.p1_style_id)
        p2_name = _safe_style_name(s.p2_style_id)

        transcript = ""
        for ln in lines:
            speaker = p1_name if int(ln.speaker_user_id) == int(s.p1_user_id) else p2_name
            transcript += f"**{speaker}:** {ln.content}\n"

        if not transcript:
            transcript = "_No lines yet._"

        status = "Active ✅" if s.is_active else "Ended ❌"
        turn = f"<@{int(s.turn_user_id)}>" if s.is_active else "—"

        embed = discord.Embed(
            title=f"Scene #{s.id} — {status}",
            description=transcript[:3900],
        )
        if s.setting:
            embed.add_field(name="Setting", value=s.setting[:1024], inline=False)
        embed.add_field(name="Turn", value=turn, inline=True)
        embed.add_field(name="Players", value=f"<@{int(s.p1_user_id)}> vs <@{int(s.p2_user_id)}>", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------
    # /scene summary
    # ---------------------------
    @scene.command(name="summary", description="Get a short summary of what happened so far")
    @app_commands.describe(scene="Scene ID", public="If true, posts summary in channel. Default: false (private).")
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def summary(self, interaction: discord.Interaction, scene: int, public: bool = False):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if not s.is_active:
            await interaction.response.send_message(
                "That scene has ended, so /scene summary is unavailable.",
                ephemeral=True,
            )
            return

        tier = await get_premium_tier(user_id)

        daily_user = await get_summary_daily_user_limiter(guild_id)
        daily_guild = await get_summary_daily_guild_limiter(guild_id)

        du = await daily_user.check(f"summary:day:user:{user_id}")
        if not du.allowed:
            per_day = getattr(daily_user, "max_events", None)
            per_day_txt = f"{per_day}/day" if per_day is not None else "today"
            await interaction.response.send_message(
                f"⛔ You hit your daily scene-summary limit (**{per_day_txt}**). "
                f"Try again in **{du.retry_after_seconds}s**.",
                ephemeral=True,
            )
            return

        dg = await daily_guild.check("summary:day:guild")
        if not dg.allowed:
            per_day = getattr(daily_guild, "max_events", None)
            per_day_txt = f"{per_day}/day" if per_day is not None else "today"
            await interaction.response.send_message(
                f"⛔ This server hit its daily scene-summary limit (**{per_day_txt}**). "
                f"Try again in **{dg.retry_after_seconds}s**.",
                ephemeral=True,
            )
            return

        if public and tier != "pro":
            public = False
            await self._send_private(interaction, "🔒 Public scene summaries are **Pro**. I’ll show it privately.")

        ok, msg = await _scene_summary_rate_limit(guild_id=guild_id, user_id=user_id, tier=tier)
        if not ok:
            await interaction.response.send_message(msg, ephemeral=True)
            return

        server_settings = await get_guild_settings(guild_id)
        language = (server_settings.get("language", "english") or "english")

        try:
            await interaction.response.defer(thinking=True, ephemeral=(not public))
        except Exception:
            pass

        txt = await _summarize_scene_text(guild_id=guild_id, user_id=user_id, scene_id=int(scene), language=language)

        embed = discord.Embed(title=f"🧾 Scene #{s.id} — Summary", description=txt[:3900])
        if s.setting:
            embed.add_field(name="Setting", value=(s.setting or "")[:1024], inline=False)

        if public:
            await interaction.channel.send(embed=embed)
            try:
                await interaction.followup.send("✅ Summary posted.", ephemeral=True)
            except Exception:
                pass
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------
    # /scene forget
    # ---------------------------
    @scene.command(name="forget", description="Delete scene memory (lines). Participants only.")
    @app_commands.describe(scene="Scene ID", confirm="Type DELETE to confirm")
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def forget(self, interaction: discord.Interaction, scene: int, confirm: str):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if int(user_id) not in (int(s.p1_user_id), int(s.p2_user_id)):
            await interaction.response.send_message("Only participants can delete scene memory.", ephemeral=True)
            return

        if (confirm or "").strip().upper() != "DELETE":
            await interaction.response.send_message("Confirmation failed. Type `DELETE` in confirm.", ephemeral=True)
            return

        try:
            n = await delete_scene_lines(scene_id=int(scene))
        except Exception:
            logger.exception("delete_scene_lines failed")
            await interaction.response.send_message("⚠️ Failed to delete memory.", ephemeral=True)
            return

        await interaction.response.send_message(f"🧹 Deleted scene memory ({n} lines).", ephemeral=True)

    # ---------------------------
    # /scene end
    # ---------------------------
    @scene.command(name="end", description="End a scene (participants only)")
    @app_commands.describe(
        scene="Scene ID",
        public="If true, announce the end publicly in channel (default: false).",
        summary="If true, DM both participants a final summary before memory is cleared (default: false).",
        public_summary="If summary is true AND public is true, post summary publicly in channel (Pro-only).",
        forget_memory="If true, delete scene lines after ending (default: true).",
    )
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def end(
        self,
        interaction: discord.Interaction,
        scene: int,
        summary: bool = False,
        public: bool = False,
        public_summary: bool = False,
        forget_memory: bool = True,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if int(user_id) not in (int(s.p1_user_id), int(s.p2_user_id)):
            await interaction.response.send_message("Only participants can end the scene.", ephemeral=True)
            return

        if not bool(getattr(s, "is_active", True)):
            await interaction.response.send_message("Scene already ended.", ephemeral=True)
            return

        ignored_public_summary = False
        if (not summary) and public_summary:
            ignored_public_summary = True
            public_summary = False

        ignored_public_channel = False
        if (not public) and public_summary:
            ignored_public_channel = True
            public_summary = False

        try:
            await interaction.response.defer(thinking=bool(summary), ephemeral=True)
        except Exception:
            pass

        embed: discord.Embed | None = None
        allow_public = False
        if summary:
            tier = await get_premium_tier(user_id)
            allow_public = bool(public and public_summary and tier == "pro")

            server_settings = await get_guild_settings(guild_id)
            language = (server_settings.get("language", "english") or "english")

            txt = await _summarize_scene_text(guild_id=guild_id, user_id=user_id, scene_id=int(scene), language=language)
            if not (txt or "").strip():
                txt = "(No scene memory was recorded, so there’s nothing to summarize.)"

            embed = discord.Embed(title=f"🧾 Scene #{s.id} — Final Summary", description=txt[:3900])
            if s.setting:
                embed.add_field(name="Setting", value=(s.setting or "")[:1024], inline=False)

        ok = await end_scene(scene_id=int(scene))

        if ok and forget_memory:
            try:
                await delete_scene_lines(scene_id=int(scene))
            except Exception:
                logger.exception("Failed to delete scene memory on end")

        if public:
            try:
                await interaction.channel.send(
                    f"🏁 **Scene #{s.id} ended** by <@{user_id}>. Participants: <@{int(s.p1_user_id)}> <@{int(s.p2_user_id)}>"
                )
            except Exception:
                pass

        async def _dm(uid: int) -> bool:
            try:
                u = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if not u:
                    return False
                await u.send(f"🏁 Scene #{s.id} ended.")
                if embed is not None:
                    await u.send(embed=embed)
                return True
            except Exception:
                return False

        dm1 = await _dm(int(s.p1_user_id))
        dm2 = await _dm(int(s.p2_user_id))

        if allow_public and embed is not None:
            try:
                await interaction.channel.send(embed=embed)
            except Exception:
                pass

        msg = f"✅ Ended Scene #{s.id}. "
        msg += ("Final summary DM’d to participants. " if summary else "Participants notified via DM. ")
        if not (dm1 and dm2):
            msg += "(I couldn’t DM one or both participants.) "
        if ok and forget_memory:
            msg += "Memory cleared. "
        if ignored_public_summary:
            msg += "(Note: public_summary ignored because summary=false.) "
        if ignored_public_channel:
            msg += "(Note: public_summary ignored because public=false.) "
        if summary and public_summary and not allow_public:
            msg += "(Public summary was not posted — Pro required.) "

        try:
            await interaction.followup.send(msg.strip(), ephemeral=True)
        except Exception:
            pass

    # ---------------------------
    # /scene list
    # ---------------------------
    @scene.command(name="list", description="List active scenes in this channel")
    async def list(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id

        await _maintenance_expire_stale(guild_id, channel_id)

        rows = await list_active_scenes_in_channel(
            guild_id=guild_id,
            channel_id=channel_id,
            limit=10,
        )

        if not rows:
            await interaction.response.send_message("No active scenes in this channel.", ephemeral=True)
            return

        lines = []
        for s in rows:
            turn = f"<@{int(s.turn_user_id)}>" if s.is_active else "—"
            lines.append(f"• **Scene #{s.id}** — <@{int(s.p1_user_id)}> vs <@{int(s.p2_user_id)}> — Turn: {turn}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---------------------------
    # /scene narrate
    # ---------------------------
    @scene.command(name="narrate", description="Add narration to a scene (describe what happens; no AI, does not change turn)")
    @app_commands.describe(
        scene="Scene ID",
        text="What happens (e.g. 'The dragon roars and the ground shakes').",
    )
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def narrate(
        self,
        interaction: discord.Interaction,
        scene: int,
        text: str,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if not s.is_active:
            await interaction.response.send_message("That scene has ended.", ephemeral=True)
            return
        if int(s.channel_id) != int(channel_id):
            await interaction.response.send_message("Use this in the original scene channel.", ephemeral=True)
            return
        if int(user_id) not in (int(s.p1_user_id), int(s.p2_user_id)):
            await interaction.response.send_message("Only participants can add narration to this scene.", ephemeral=True)
            return

        narration = (text or "").strip()
        if not narration:
            await interaction.response.send_message("Please provide the narration text.", ephemeral=True)
            return

        narration_s = _clamp(narration, SCENE_MAX_MESSAGE_CHARS)
        deny = await safety_gate(guild_id, narration_s)
        if deny:
            await interaction.response.send_message(f"🚫 {deny}", ephemeral=True)
            return

        if int(user_id) == int(s.p1_user_id):
            speaker_style_id = s.p1_style_id
        else:
            speaker_style_id = s.p2_style_id

        try:
            await add_scene_line(
                scene_id=int(scene),
                guild_id=guild_id,
                channel_id=channel_id,
                speaker_user_id=user_id,
                speaker_style_id=speaker_style_id,
                content=f"[Narration] {narration_s}",
            )
        except Exception:
            logger.exception("Failed to save scene narration")
            await interaction.response.send_message("⚠️ Failed to save narration.", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ Narration added to **Scene #{scene}**.", ephemeral=True)

    # ---------------------------
    # /scene say
    # ---------------------------
    @scene.command(name="say", description="Take your turn in a scene (AI generates your character response)")
    @app_commands.describe(
        scene="Scene ID",
        direction="How your character should respond (tone/intent). Optional but recommended.",
        message="What you do/say (your action / line).",
    )
    @app_commands.autocomplete(scene=ac_scene_active_for_user_in_channel)
    async def say(
        self,
        interaction: discord.Interaction,
        scene: int,
        direction: str | None = None,
        message: str = "",
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server, not DMs.", ephemeral=True)
            return

        if not await _require_redis_for_scenes(interaction):
            return

        guild_id = interaction.guild.id
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        await _maintenance_expire_stale(guild_id, channel_id)

        s = await get_scene(scene_id=int(scene))
        if not s or int(s.guild_id) != guild_id:
            await interaction.response.send_message("Scene not found.", ephemeral=True)
            return

        if s.is_active:
            expired = await expire_scene_if_stale(scene_id=int(scene), ttl_seconds=SCENE_TTL_SECONDS)
            if expired:
                await interaction.response.send_message("⏳ This scene expired due to inactivity (48h).", ephemeral=True)
                return
            s = await get_scene(scene_id=int(scene)) or s

        if not s.is_active:
            await interaction.response.send_message("That scene has ended.", ephemeral=True)
            return
        if int(s.channel_id) != int(channel_id):
            await interaction.response.send_message("Use this in the original scene channel.", ephemeral=True)
            return
        if int(user_id) not in (int(s.p1_user_id), int(s.p2_user_id)):
            await interaction.response.send_message("Only participants can speak in this scene.", ephemeral=True)
            return
        if int(s.turn_user_id) != int(user_id):
            await interaction.response.send_message("It’s not your turn yet.", ephemeral=True)
            return

        decision = await decide_ai_access(
            interaction,
            command_key="scene",
            user_text=f"{direction or ''}\n{message or ''}",
        )
        if not decision.allowed:
            await send_error(interaction, decision.message or "Not allowed.")
            return

        # Daily + weekly scene budgets are enforced centrally in core.ai_gateway

        locked, remaining = await is_user_penalized(guild_id, user_id)
        if locked:
            await interaction.response.send_message(
                f"⛔ You’re temporarily restricted for spam. Try again in **{remaining}s**.",
                ephemeral=True,
            )
            return

        msg_clean = (message or "").strip()
        if not msg_clean:
            await interaction.response.send_message("Please include what you do/say.", ephemeral=True)
            return

        direction_s = _clamp(direction or "", SCENE_MAX_DIRECTION_CHARS)
        msg_s = _clamp(msg_clean, SCENE_MAX_MESSAGE_CHARS)

        gate_text = (direction_s + "\n" + msg_s).strip()
        deny = await safety_gate(guild_id, gate_text)
        if deny:
            await interaction.response.send_message(f"🚫 {deny}", ephemeral=True)
            return

        user_limiter = await get_user_limiter(guild_id)
        guild_limiter = await get_guild_limiter(guild_id)

        u = await user_limiter.check(f"user:{user_id}:scene")
        if not u.allowed:
            ps = await record_cooldown_strike(guild_id, user_id)
            now = int(time.time())
            penalty_applied = bool(ps.is_penalized) or (int(ps.penalty_until or 0) > now)
            penalty_s = max(0, int(ps.penalty_until or 0) - now)

            msg = f"⏳ You’re on cooldown. Try again in **{u.retry_after_seconds}s**."
            if penalty_applied:
                msg += f"\n⛔ Repeated spam detected — you’re restricted for **{penalty_s}s**."
            await interaction.response.send_message(msg, ephemeral=True)
            return

        g = await guild_limiter.check(f"guild:{guild_id}:scene")
        if not g.allowed:
            await interaction.response.send_message(
                f"⏳ This server is on cooldown. Try again in **{g.retry_after_seconds}s**.",
                ephemeral=True,
            )
            return

        scene_limiter = await get_scene_limiter(guild_id)
        scheck = await scene_limiter.check(f"scene:{int(scene)}")
        if not scheck.allowed:
            await interaction.response.send_message(
                f"⏳ Scene is on cooldown. Try again in **{scheck.retry_after_seconds}s**.",
                ephemeral=True,
            )
            return

        if int(user_id) == int(s.p1_user_id):
            speaker_style_id = s.p1_style_id
            opponent_style_id = s.p2_style_id
        else:
            speaker_style_id = s.p2_style_id
            opponent_style_id = s.p1_style_id

        speaker_style = get_style(speaker_style_id) or get_style("fun")
        speaker_name = speaker_style.display_name if speaker_style else speaker_style_id
        opp_name = _safe_style_name(opponent_style_id)

        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except Exception:
            pass

        ai_text = ""
        try:
            p1_name = _safe_style_name(s.p1_style_id)
            p2_name = _safe_style_name(s.p2_style_id)

            recent_lines = await get_recent_scene_lines(scene_id=int(scene), limit=SCENE_CONTEXT_LINES)
            transcript_lines: list[str] = []
            for ln in recent_lines:
                nm = p1_name if int(ln.speaker_user_id) == int(s.p1_user_id) else p2_name
                transcript_lines.append(_format_scene_line(nm, ln.content))

            transcript_lines.append(_format_scene_line(speaker_name, msg_s))

            server_settings = await get_guild_settings(guild_id)
            language = (server_settings.get("language", "english") or "english")

            system = build_scene_system_prompt(
                language=language,
                style_prompt=(speaker_style.prompt if speaker_style else ""),
                style_obj=speaker_style,
            )
            user_prompt = build_scene_user_prompt(
                setting=s.setting,
                transcript_lines=transcript_lines[-SCENE_CONTEXT_LINES:],
                direction=direction_s,
                user_message=msg_s,
            )

            tier = await get_premium_tier(user_id)

            mt = MetricsTimer(
                "scene",
                guild_id,
                user_id,
                input_chars=len(user_prompt),
                model=getattr(config, "OPENAI_MODEL", None),
            )

            resp = await request_text(
                guild_id=guild_id,
                user_id=user_id,
                tier=tier,
                mode="scene",
                system=system,
                user_prompt=user_prompt,
                max_output_tokens=350,
                timeout_s=float(getattr(config, "OPENAI_TIMEOUT_S", 20.0) or 20.0),
            )
            if not resp.ok:
                emit(mt.finish(ok=False, error_type=resp.error_type or "AIGatewayError"))
                await interaction.followup.send(resp.user_message or "⚠️ Something went wrong.", ephemeral=True)
                return

            ai_text = (resp.text or "").strip()

            # AI usage metrics are recorded centrally in core.ai_gateway (real tokens when available).

            emit(mt.finish(ok=True, output_chars=len(ai_text or "")))

        except Exception:
            logger.exception("SCENE SAY failed (post-defer)")
            try:
                await interaction.followup.send("⚠️ Something went wrong. Please try again in a minute.", ephemeral=True)
            except Exception:
                pass
            return

        ai_text = (ai_text or "").strip()
        if not ai_text:
            await interaction.followup.send("⚠️ The AI returned an empty response. Please try again.", ephemeral=True)
            return
        if len(ai_text) > 1900:
            ai_text = ai_text[:1899].rstrip() + "…"

        # Save lines (best-effort)
        try:
            await add_scene_line(
                scene_id=int(scene),
                guild_id=guild_id,
                channel_id=channel_id,
                speaker_user_id=user_id,
                speaker_style_id=speaker_style_id,
                content=f"[Action] {msg_s}",
            )
            await add_scene_line(
                scene_id=int(scene),
                guild_id=guild_id,
                channel_id=channel_id,
                speaker_user_id=user_id,
                speaker_style_id=speaker_style_id,
                content=f"[Reply] {ai_text}",
            )
        except Exception:
            logger.exception("Failed to save scene lines")

        # Flip turn
        try:
            new_turn = await flip_turn(scene_id=int(scene))
        except Exception:
            new_turn = None

        next_turn_mention = f"<@{new_turn}>" if new_turn else "—"

        embed = discord.Embed(
            title=f"🎭 Scene #{s.id}",
            description=f"**{speaker_name}** vs **{opp_name}**\n\n**{speaker_name}:** {ai_text}",
        )
        embed.set_footer(text=f"Next turn: {next_turn_mention} • Use /scene say scene:{s.id} • /scene summary scene:{s.id}")

        try:
            view = ReportView(
                bot=self.bot,
                guild_id=guild_id,
                channel_id=channel_id,
                reporter_id=user_id,
                command="scene",
                prompt_excerpt=f"[Direction] {direction_s}\n[Action] {msg_s}",
                response_excerpt=ai_text,
                style_id=speaker_style_id,
            )
        except Exception:
            view = None

        dm1 = dm2 = True
        try:
            dm1, dm2 = await self._deliver_scene_embed(
                s=s,
                channel=interaction.channel,
                embed=embed,
                view=view,
                guild_id=guild_id,
                channel_id=channel_id,
            )
        except Exception:
            logger.exception("Failed to deliver scene message")

        if self._scene_is_public(s):
            await interaction.followup.send("✅ Turn played. Scene updated.", ephemeral=True)
        else:
            note = "✅ Turn played. Sent via DM."
            if not (dm1 and dm2):
                note += " (⚠️ I couldn’t DM one or both players — they may have DMs closed.)"
            await interaction.followup.send(note, ephemeral=True)

        if new_turn:
            try:
                await interaction.followup.send(
                    f"🔔 It’s now <@{new_turn}>'s turn in **Scene #{s.id}**.",
                    ephemeral=True,
                )
            except Exception:
                pass


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashScene") is None:
        await bot.add_cog(SlashScene(bot))
