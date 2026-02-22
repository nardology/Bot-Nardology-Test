from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.owner import is_bot_owner
from utils.ai_kill import disable as ai_disable_runtime, enable as ai_enable_runtime
from utils.premium import get_premium_tier
from utils.storage import get_guild_setting, set_guild_setting
from utils.backpressure import get_redis_or_none
from utils.mod_actions import (
    ban_user,
    unban_user,
    is_bot_disabled,
    disable_bot,
    enable_bot,
    is_user_banned,
    get_user_ban_reason,
    get_bot_disabled_meta,
    set_nuke_warning,
    mark_guild_nuked,
)

from utils.db import get_sessionmaker
from utils.packs_store import list_custom_packs, delete_custom_pack, normalize_pack_id, get_custom_pack, upsert_custom_pack, add_character_to_pack
from utils.character_registry import merge_pack_payload
from utils.models import (
    AnalyticsDailyMetric,
    GuildSetting,
    PremiumEntitlement,
    BondState,
    VoiceSound,
    PointsLedger,
    PointsWallet,
    QuestClaim,
    QuestProgress,
)
from utils.verification import (
    get_ticket,
    update_ticket_status,
    increment_trust_approval,
    increment_trust_denial,
    list_pending_tickets,
    is_auto_verify_enabled,
    set_auto_verify_enabled,
    check_auto_approve_expired,
)

try:
    from sqlalchemy import delete  # type: ignore
except Exception:  # pragma: no cover
    delete = None  # type: ignore


logger = logging.getLogger("bot.z_server")


async def _ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        logger.exception("Failed sending z_server response")


def _owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_bot_owner(getattr(interaction.user, "id", 0))

    return app_commands.check(predicate)


def _fmt_guild_line(g: discord.Guild, *, prefix: str = "- ") -> str:
    name = str(getattr(g, "name", "") or "Unknown")[:64]
    gid = int(getattr(g, "id", 0) or 0)
    members = int(getattr(g, "member_count", 0) or 0)
    owner_id = int(getattr(g, "owner_id", 0) or 0)
    return f"{prefix}**{name}** (`{gid}`) members={members} owner_id=`{owner_id}`"


def _fmt_utc_from_epoch(epoch_s: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(epoch_s)))
    except Exception:
        return "(unknown time)"


async def _purge_guild_redis(*, guild_id: int, max_keys: int = 5000) -> int:
    """
    Best-effort purge of Redis keys that are clearly guild-scoped in this codebase.
    Returns number of keys deleted (approx).
    """
    gid = int(guild_id)
    r = await get_redis_or_none()
    if r is None:
        return 0
    deleted = 0
    try:
        # Guild scalar settings hash
        await r.delete(f"guild:{gid}:settings")
        deleted += 1
    except Exception:
        pass

    # Guild list settings sets
    try:
        pattern = f"guild:{gid}:list:*"
        # Use SCAN to avoid blocking Redis. Cap deletions.
        async for key in r.scan_iter(match=pattern, count=500):
            try:
                await r.delete(key)
                deleted += 1
                if deleted >= max_keys:
                    break
            except Exception:
                continue
    except Exception:
        pass

    return deleted


async def _delete_guild_db_data(*, guild_id: int) -> tuple[bool, str]:
    if delete is None:
        return False, "SQL delete is unavailable in this build."
    gid = int(guild_id)
    try:
        Session = get_sessionmaker()
        async with Session() as session:
            # analytics
            await session.execute(delete(AnalyticsDailyMetric).where(AnalyticsDailyMetric.guild_id == gid))

            # per-guild tables
            await session.execute(delete(GuildSetting).where(GuildSetting.guild_id == gid))
            await session.execute(delete(PremiumEntitlement).where(PremiumEntitlement.guild_id == gid))
            await session.execute(delete(BondState).where(BondState.guild_id == gid))
            await session.execute(delete(VoiceSound).where(VoiceSound.guild_id == gid))

            # economy + quests
            await session.execute(delete(PointsLedger).where(PointsLedger.guild_id == gid))
            await session.execute(delete(PointsWallet).where(PointsWallet.guild_id == gid))
            await session.execute(delete(QuestClaim).where(QuestClaim.guild_id == gid))
            await session.execute(delete(QuestProgress).where(QuestProgress.guild_id == gid))

            await session.commit()
        return True, "ok"
    except Exception as e:
        logger.exception("DB delete failed for guild_id=%s", gid)
        return False, f"DB delete failed: {type(e).__name__}"


async def _delete_guild_packs(*, guild_id: int) -> tuple[int, int]:
    """
    Deletes any custom packs created by this guild (created_by_guild),
    plus best-effort deletion of the server_<guild_id> pack if present.
    Returns (deleted_ok, deleted_fail).
    """
    gid = int(guild_id)
    ok = 0
    fail = 0
    try:
        packs = await list_custom_packs(limit=5000, include_internal=True)
    except Exception:
        packs = []

    for p in packs or []:
        try:
            if not isinstance(p, dict):
                continue
            created_by = int(p.get("created_by_guild", 0) or 0)
            if created_by != gid:
                continue
            pid = normalize_pack_id(str(p.get("pack_id") or ""))
            if not pid:
                continue
            if await delete_custom_pack(pid):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1

    # Also attempt to delete the per-guild server pack (if it exists)
    try:
        server_pid = normalize_pack_id(f"server_{gid}")
        if server_pid and await get_custom_pack(server_pid):
            if await delete_custom_pack(server_pid):
                ok += 1
            else:
                fail += 1
    except Exception:
        pass

    return ok, fail


class _NukeConfirmView(discord.ui.View):
    def __init__(self, *, bot: commands.Bot, actor_id: int, guild_id: int, reason: str, notes: str):
        super().__init__(timeout=90)
        self.bot = bot
        self.actor_id = int(actor_id)
        self.guild_id = int(guild_id)
        self.reason = (reason or "").strip()
        self.notes = (notes or "").strip()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.actor_id:
            try:
                await interaction.response.send_message("This button isn’t for you.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="CANCEL", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.response.edit_message(content="Cancelled.", view=self)
        except Exception:
            pass

    @discord.ui.button(label="NUKE NOW", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.response.edit_message(content="Nuking…", view=self)
        except Exception:
            pass

        gid = int(self.guild_id)
        actor = int(self.actor_id)

        # Try resolve guild + owner id
        g = self.bot.get_guild(gid)
        owner_id = int(getattr(g, "owner_id", 0) or 0) if g is not None else 0

        # 1) Delete DB data
        db_ok, db_msg = await _delete_guild_db_data(guild_id=gid)

        # 2) Delete packs created by the guild (Redis)
        packs_ok, packs_fail = await _delete_guild_packs(guild_id=gid)

        # 3) Purge guild-scoped Redis settings/lists (best-effort)
        redis_deleted = await _purge_guild_redis(guild_id=gid)

        # 4) Ban guild owner (if known)
        if owner_id:
            try:
                await ban_user(user_id=owner_id, reason=f"nuke:{gid} {self.reason}"[:400], by_user_id=actor)
            except Exception:
                pass

        # 5) Mark guild nuked
        try:
            await mark_guild_nuked(guild_id=gid, reason=self.reason, by_user_id=actor)
        except Exception:
            pass

        # 6) Leave guild (if we're in it)
        left = False
        if g is not None:
            try:
                await g.leave()
                left = True
            except Exception:
                left = False

        summary = (
            f"✅ NUKE complete for guild `{gid}`\n"
            f"- DB delete: **{db_ok}** ({db_msg})\n"
            f"- Packs deleted: ok=`{packs_ok}` fail=`{packs_fail}`\n"
            f"- Redis keys purged (approx): `{redis_deleted}`\n"
            f"- Owner banned: `{owner_id or 0}`\n"
            f"- Left guild: **{left}**"
        )
        try:
            await interaction.followup.send(summary[:1900], ephemeral=True)
        except Exception:
            pass


class _VerificationDecisionModal(discord.ui.Modal):
    def __init__(self, *, ticket_id: str, action: str):
        super().__init__(title=f"Verification {action.title()}")
        self.ticket_id = ticket_id
        self.action = action
        self.reason = discord.ui.TextInput(
            label="Optional comment / reason",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            placeholder="Why approve/deny? (optional)",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_bot_owner(getattr(interaction.user, "id", 0)):
            await _ephemeral(interaction, "Owner only.")
            return

        ticket = await get_ticket(self.ticket_id)
        if not ticket:
            await _ephemeral(interaction, "Ticket not found (may have expired).")
            return

        status = str(ticket.get("status") or "pending")
        if status != "pending":
            await _ephemeral(interaction, f"Ticket already `{status}`.")
            return

        decision_reason = str(self.reason.value or "").strip()
        gid = int(ticket.get("guild_id") or 0)
        uid = int(ticket.get("user_id") or 0)
        ticket_type = str(ticket.get("type") or "")
        payload = ticket.get("payload") or {}

        if self.action == "approve":
            # Apply the creation/edit
            try:
                if ticket_type == "pack_create":
                    ok = await upsert_custom_pack(payload)
                    if ok:
                        await increment_trust_approval(guild_id=gid, user_id=uid)
                        await update_ticket_status(
                            ticket_id=self.ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=decision_reason,
                        )
                        # Merge into registry
                        try:
                            merge_pack_payload(payload)
                        except Exception:
                            pass
                        msg = f"✅ Pack `{payload.get('pack_id', '')}` approved and created."
                    else:
                        msg = "⚠️ Failed to create pack (storage error)."
                elif ticket_type == "pack_edit":
                    ok = await upsert_custom_pack(payload)
                    if ok:
                        await increment_trust_approval(guild_id=gid, user_id=uid)
                        await update_ticket_status(
                            ticket_id=self.ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=decision_reason,
                        )
                        # Merge into registry
                        try:
                            merge_pack_payload(payload)
                        except Exception:
                            pass
                        msg = f"✅ Pack `{payload.get('pack_id', '')}` approved and updated."
                    else:
                        msg = "⚠️ Failed to update pack (storage error)."
                elif ticket_type == "character_add":
                    pack_id = str(payload.get("pack_id") or "")
                    ok, msg_save = await add_character_to_pack(pack_id, payload)
                    if ok:
                        await increment_trust_approval(guild_id=gid, user_id=uid)
                        await update_ticket_status(
                            ticket_id=self.ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=decision_reason,
                        )
                        # Merge into registry
                        try:
                            p = await get_custom_pack(pack_id)
                            if p:
                                merge_pack_payload(p)
                        except Exception:
                            pass
                        msg = f"✅ Character `{payload.get('id', '')}` approved and added to pack `{pack_id}`."
                    else:
                        msg = f"⚠️ Failed to add character: {msg_save}"
                elif ticket_type == "character_edit":
                    pack_id = str(payload.get("pack_id") or "")
                    ok, msg_save = await add_character_to_pack(pack_id, payload)
                    if ok:
                        await increment_trust_approval(guild_id=gid, user_id=uid)
                        await update_ticket_status(
                            ticket_id=self.ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=decision_reason,
                        )
                        # Merge into registry
                        try:
                            p = await get_custom_pack(pack_id)
                            if p:
                                merge_pack_payload(p)
                        except Exception:
                            pass
                        msg = f"✅ Character `{payload.get('id', '')}` approved and updated in pack `{pack_id}`."
                    else:
                        msg = f"⚠️ Failed to update character: {msg_save}"
                else:
                    msg = f"⚠️ Unknown ticket type: {ticket_type}"
            except Exception:
                logger.exception("Failed applying approved verification ticket")
                msg = "⚠️ Failed to apply approval (check logs)."
        else:
            # Deny
            await increment_trust_denial(guild_id=gid, user_id=uid)
            await update_ticket_status(
                ticket_id=self.ticket_id,
                status="denied",
                decided_by=int(getattr(interaction.user, "id", 0) or 0),
                decision_reason=decision_reason,
            )
            msg = f"❌ Verification denied for {ticket_type} (guild `{gid}`, user `{uid}`)."

        if decision_reason:
            msg += f"\nComment: `{decision_reason}`"

        # Notify submitter (best-effort DM)
        try:
            user = await interaction.client.fetch_user(uid)  # type: ignore[attr-defined]
            await user.send((msg + f"\nTicket: `{self.ticket_id}`")[:1900])
        except Exception:
            pass

        await _ephemeral(interaction, msg[:1900])


class VerificationDecisionView(discord.ui.View):
    def __init__(self, *, ticket_id: str):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_owner(getattr(interaction.user, "id", 0)):
            try:
                await interaction.response.send_message("Owner only.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_VerificationDecisionModal(ticket_id=self.ticket_id, action="approve"))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_VerificationDecisionModal(ticket_id=self.ticket_id, action="deny"))


class SlashServer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    z_server = app_commands.Group(name="z_server", description="Owner-only server moderation dashboard")
    verification = app_commands.Group(name="verification", description="Content verification", parent=z_server)
    announce_channel = app_commands.Group(
        name="announce_channel",
        description="Configure announcement channel per server",
        parent=z_server,
    )

    # ----------------------------
    # Global bot enable/disable
    # ----------------------------

    @z_server.command(name="bot_disable", description="Disable ALL bot commands globally (except owner)")
    @_owner_only()
    @app_commands.describe(reason="Why you're disabling the bot")
    async def bot_disable(self, interaction: discord.Interaction, reason: str = "manual"):
        await disable_bot(reason=reason, by_user_id=int(getattr(interaction.user, "id", 0) or 0))
        await _ephemeral(interaction, f"⛔ Bot disabled globally. Reason: `{reason}`")

    @z_server.command(name="bot_enable", description="Re-enable ALL bot commands globally")
    @_owner_only()
    async def bot_enable(self, interaction: discord.Interaction):
        await enable_bot()
        await _ephemeral(interaction, "✅ Bot re-enabled globally.")

    @z_server.command(name="bot_status", description="Show global bot disable + ban status")
    @_owner_only()
    async def bot_status(self, interaction: discord.Interaction):
        disabled = bool(await is_bot_disabled())
        t, reason, by = await get_bot_disabled_meta()
        extra = ""
        if disabled:
            extra = f"\nReason: `{reason or 'unknown'}`"
            if by:
                extra += f"\nBy: `{by}`"
            if t:
                extra += f"\nAt: `<t:{int(t)}:R>`"
        await _ephemeral(interaction, f"Bot disabled: **{disabled}**{extra}")

    # ----------------------------
    # AI enable/disable (alias of existing runtime kill switch)
    # ----------------------------

    @z_server.command(name="disable_ai", description="Disable AI globally (runtime) for a duration")
    @_owner_only()
    @app_commands.describe(minutes="How long to disable (minutes)", reason="Why you're disabling")
    async def disable_ai(self, interaction: discord.Interaction, minutes: int = 60, reason: str = "manual"):
        ttl_s = max(300, int(minutes) * 60)
        await ai_disable_runtime(reason=f"z_server disable: {reason}", ttl_s=ttl_s)
        await _ephemeral(interaction, f"⛔ AI disabled (runtime) for ~{int(ttl_s/60)} minutes. Reason: `{reason}`")

    @z_server.command(name="enable_ai", description="Re-enable AI globally (runtime)")
    @_owner_only()
    async def enable_ai(self, interaction: discord.Interaction):
        await ai_enable_runtime()
        await _ephemeral(interaction, "✅ AI re-enabled (runtime).")

    # ----------------------------
    # User bans (global)
    # ----------------------------

    @z_server.command(name="ban_user", description="Ban a user from using the bot (global)")
    @_owner_only()
    @app_commands.describe(user_id="User ID to ban", reason="Optional reason")
    async def ban_user_cmd(self, interaction: discord.Interaction, user_id: str, reason: str = ""):
        if not (user_id or "").strip().isdigit():
            await _ephemeral(interaction, "Provide a numeric user_id.")
            return
        uid = int(user_id.strip())
        await ban_user(user_id=uid, reason=reason, by_user_id=int(getattr(interaction.user, "id", 0) or 0))
        await _ephemeral(interaction, f"✅ Banned user `{uid}`. Reason: `{(reason or 'n/a')}`")

    @z_server.command(name="unban_user", description="Unban a user (global)")
    @_owner_only()
    @app_commands.describe(user_id="User ID to unban", reason="Optional reason")
    async def unban_user_cmd(self, interaction: discord.Interaction, user_id: str, reason: str = ""):
        if not (user_id or "").strip().isdigit():
            await _ephemeral(interaction, "Provide a numeric user_id.")
            return
        uid = int(user_id.strip())
        await unban_user(user_id=uid, by_user_id=int(getattr(interaction.user, "id", 0) or 0), reason=reason)
        await _ephemeral(interaction, f"✅ Unbanned user `{uid}`. Reason: `{(reason or 'n/a')}`")

    @z_server.command(name="check_user", description="Check whether a user is banned")
    @_owner_only()
    @app_commands.describe(user_id="User ID to check")
    async def check_user(self, interaction: discord.Interaction, user_id: str):
        if not (user_id or "").strip().isdigit():
            await _ephemeral(interaction, "Provide a numeric user_id.")
            return
        uid = int(user_id.strip())
        banned = bool(await is_user_banned(uid))
        reason = await get_user_ban_reason(uid) if banned else ""
        msg = f"User `{uid}` banned: **{banned}**"
        if banned and reason:
            msg += f"\nReason: `{reason}`"
        await _ephemeral(interaction, msg)

    # ----------------------------
    # Server summary (guild list)
    # ----------------------------

    @z_server.command(name="bot_server_summary", description="List servers the bot is in (paged)")
    @_owner_only()
    @app_commands.describe(
        start="Start index (0-based)",
        limit="How many guilds to show (max 100)",
        premium_only="Only show premium servers (includes /start trial + admin premium)",
    )
    async def bot_server_summary(self, interaction: discord.Interaction, start: int = 0, limit: int = 50, premium_only: bool = False):
        start_i = max(0, int(start or 0))
        limit_i = max(1, min(100, int(limit or 50)))

        guilds = list(getattr(self.bot, "guilds", []) or [])
        total = len(guilds)
        if total <= 0:
            await _ephemeral(interaction, "Bot is not in any servers.")
            return

        # Defer for expensive premium checks.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        page = guilds[start_i : start_i + limit_i]

        lines: list[str] = []
        shown = 0
        for g in page:
            try:
                if premium_only:
                    owner_id = int(getattr(g, "owner_id", 0) or 0)
                    tier = await get_premium_tier(owner_id) if owner_id else "free"
                    if tier != "pro":
                        continue
                lines.append(_fmt_guild_line(g))
                shown += 1
            except Exception:
                continue

        header = f"**Guilds** total=`{total}` showing=`{shown}` range=`{start_i}`..`{start_i + limit_i - 1}`"
        if premium_only:
            header += " filter=`premium_only`"

        body = "\n".join(lines) if lines else "(none matched)"
        msg = (header + "\n" + body)[:1900]
        await _ephemeral(interaction, msg)

    # ----------------------------
    # Announcements
    # ----------------------------

    @announce_channel.command(name="set", description="Set this server's announcement channel")
    @_owner_only()
    @app_commands.describe(channel="Channel where announcements will be posted")
    async def announce_channel_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this inside a server.")
            return
        await set_guild_setting(int(interaction.guild.id), "announce_channel_id", int(channel.id))
        await _ephemeral(interaction, f"✅ Announcement channel set to {channel.mention}.")

    @announce_channel.command(name="clear", description="Clear this server's announcement channel")
    @_owner_only()
    async def announce_channel_clear(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral(interaction, "Run this inside a server.")
            return
        await set_guild_setting(int(interaction.guild.id), "announce_channel_id", 0)
        await _ephemeral(interaction, "✅ Announcement channel cleared.")

    @z_server.command(name="announce", description="Broadcast announcement to configured channels + DM server owners")
    @_owner_only()
    @app_commands.describe(
        message="Announcement text",
        start="Start index (0-based) for guild paging",
        limit="How many guilds to process (max 200; use paging for more)",
        dm_owners="Also DM each server owner",
    )
    async def announce(self, interaction: discord.Interaction, message: str, start: int = 0, limit: int = 50, dm_owners: bool = True):
        msg = (message or "").strip()
        if not msg:
            await _ephemeral(interaction, "Message cannot be empty.")
            return

        start_i = max(0, int(start or 0))
        limit_i = max(1, min(200, int(limit or 50)))

        guilds = list(getattr(self.bot, "guilds", []) or [])
        total = len(guilds)
        if total <= 0:
            await _ephemeral(interaction, "Bot is not in any servers.")
            return

        # Defer because this can take time and hit rate limits.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        batch = guilds[start_i : start_i + limit_i]
        ch_ok = 0
        ch_fail = 0
        dm_ok = 0
        dm_fail = 0

        for g in batch:
            gid = int(getattr(g, "id", 0) or 0)
            if not gid:
                continue

            # 1) Channel broadcast (option B): only if configured for that guild.
            try:
                ch_id = await get_guild_setting(gid, "announce_channel_id", 0)
                ch_id_i = int(ch_id or 0)
            except Exception:
                ch_id_i = 0

            if ch_id_i:
                try:
                    ch = g.get_channel(ch_id_i)
                    if ch is None:
                        ch = await self.bot.fetch_channel(ch_id_i)  # type: ignore[assignment]
                    if isinstance(ch, (discord.TextChannel, discord.Thread)):
                        await ch.send(msg[:1900])
                        ch_ok += 1
                    else:
                        ch_fail += 1
                except Exception:
                    ch_fail += 1

            # 2) Owner DMs (best effort)
            if dm_owners:
                try:
                    owner_id = int(getattr(g, "owner_id", 0) or 0)
                    if owner_id:
                        user = await self.bot.fetch_user(owner_id)
                        await user.send(msg[:1900])
                        dm_ok += 1
                except Exception:
                    dm_fail += 1

            # Gentle pacing (prevents burst rate-limit spikes)
            try:
                await asyncio.sleep(0.6)
            except Exception:
                pass

        summary = (
            f"✅ Announcement done.\n"
            f"Guild batch: `{start_i}`..`{start_i + len(batch) - 1}` of `{total}`\n"
            f"Channel posts: ok=`{ch_ok}` fail=`{ch_fail}` (only configured channels attempted)\n"
            f"Owner DMs: ok=`{dm_ok}` fail=`{dm_fail}`"
        )
        await _ephemeral(interaction, summary[:1900])

    # ----------------------------
    # Verification system (Phase 1)
    # ----------------------------

    @verification.command(name="toggle_auto", description="Enable/disable auto-approve after 5 days")
    @_owner_only()
    async def verification_toggle_auto(self, interaction: discord.Interaction):
        current = bool(await is_auto_verify_enabled())
        await set_auto_verify_enabled(not current)
        await _ephemeral(interaction, f"✅ Auto-verify {'enabled' if not current else 'disabled'}.")

    @verification.command(name="list", description="List pending verification tickets (dashboard)")
    @_owner_only()
    @app_commands.describe(limit="How many tickets to show (max 50)", status="Filter by status (pending/approved/denied/auto_approved)")
    async def verification_list(self, interaction: discord.Interaction, limit: int = 20, status: str | None = None):
        from utils.verification import list_tickets_by_status, get_trust_score
        
        limit_i = max(1, min(50, int(limit or 20)))
        status_filter = (status or "pending").lower().strip()
        
        if status_filter == "pending":
            tickets = await list_pending_tickets(limit=limit_i)
        else:
            tickets = await list_tickets_by_status(status=status_filter, limit=limit_i)
        
        if not tickets:
            await _ephemeral(interaction, f"No {status_filter} verification tickets.")
            return

        lines: list[str] = []
        for t in tickets:
            tid = str(t.get("ticket_id") or "")[:12]
            ttype = str(t.get("type") or "unknown")
            gid = int(t.get("guild_id") or 0)
            uid = int(t.get("user_id") or 0)
            created = int(t.get("created_at") or 0)
            age_days = (int(time.time()) - created) / 86400.0 if created > 0 else 0.0
            
            # Get trust score
            trust = await get_trust_score(guild_id=gid, user_id=uid)
            trust_score = float(trust.get("score", 0.0))
            trust_str = f"trust={trust_score:.0%}" if trust_score > 0 else "no trust"
            
            payload = t.get("payload") or {}
            flags = []
            if bool(payload.get("private", False)):
                flags.append("PRIVATE")
            flags_str = " " + " ".join(flags) if flags else ""
            
            # Show decision info if not pending
            decision_info = ""
            if status_filter != "pending":
                decided_by = t.get("decided_by", 0)
                decision_reason = str(t.get("decision_reason") or "")[:50]
                if decided_by:
                    decision_info = f" by={decided_by}"
                if decision_reason:
                    decision_info += f" reason=`{decision_reason}`"
            
            lines.append(f"• `{tid}` {ttype} g={gid} u={uid} age={age_days:.1f}d {trust_str}{flags_str}{decision_info}")

        msg = f"**{status_filter.title()} tickets** ({len(tickets)})\n" + "\n".join(lines)[:1900]
        await _ephemeral(interaction, msg)

    @verification.command(name="status", description="Show verification system status dashboard")
    @_owner_only()
    async def verification_status(self, interaction: discord.Interaction):
        from utils.verification import list_tickets_by_status
        
        auto_enabled = bool(await is_auto_verify_enabled())
        pending = await list_pending_tickets(limit=1000)
        pending_count = len(pending)
        approved = await list_tickets_by_status(status="approved", limit=1000)
        denied = await list_tickets_by_status(status="denied", limit=1000)
        auto_approved = await list_tickets_by_status(status="auto_approved", limit=1000)
        
        msg = (
            f"**Verification Dashboard**\n"
            f"Auto-approve (5 days): **{'✅ Enabled' if auto_enabled else '❌ Disabled'}**\n\n"
            f"**Ticket Counts:**\n"
            f"• Pending: **{len(pending)}**\n"
            f"• Approved (manual): **{len(approved)}**\n"
            f"• Auto-approved: **{len(auto_approved)}**\n"
            f"• Denied: **{len(denied)}**\n"
            f"• **Total: {len(pending) + len(approved) + len(auto_approved) + len(denied)}**"
        )
        await _ephemeral(interaction, msg)

    # ----------------------------
    # Pass 2: nuke warnings + manual nukes (no scheduler)
    # ----------------------------

    @z_server.command(name="nuke_warning", description="Warn a guild owner that their server will be nuked in X days")
    @_owner_only()
    @app_commands.describe(
        guild_id="Guild ID to warn",
        days="Days until nuke (for messaging only; no scheduler)",
        reason="Reason for the warning",
        notes="Optional extra notes to include",
    )
    async def nuke_warning(self, interaction: discord.Interaction, guild_id: str, days: int = 7, reason: str = "policy violation", notes: str = ""):
        if not (guild_id or "").strip().isdigit():
            await _ephemeral(interaction, "Provide a numeric guild_id.")
            return
        gid = int(guild_id.strip())
        days_i = max(0, int(days or 0))

        # Best-effort DM guild owner (only if we can resolve it)
        dm_ok = False
        owner_id = 0
        try:
            g = self.bot.get_guild(gid)
            owner_id = int(getattr(g, "owner_id", 0) or 0) if g is not None else 0
        except Exception:
            owner_id = 0

        deadline = await set_nuke_warning(
            guild_id=gid,
            days_until_nuke=days_i,
            reason=reason,
            notes=notes,
            by_user_id=int(getattr(interaction.user, "id", 0) or 0),
            owner_id=int(owner_id or 0),
        )

        if owner_id:
            try:
                user = await self.bot.fetch_user(owner_id)
                msg = (
                    f"⚠️ **Nuke warning** for server `{gid}`\n\n"
                    f"Reason: {reason}\n"
                    f"Deadline: `{_fmt_utc_from_epoch(deadline)}` ({days_i} day(s))\n"
                )
                if (notes or "").strip():
                    msg += f"\nNotes:\n{(notes or '').strip()[:1200]}\n"
                msg += "\nIf the issue is not resolved by the deadline, the bot may be disabled and server data may be lost."
                await user.send(msg[:1900])
                dm_ok = True
            except Exception:
                dm_ok = False

        await _ephemeral(
            interaction,
            f"✅ Stored nuke warning for guild `{gid}` deadline=`{_fmt_utc_from_epoch(deadline)}` owner_id=`{owner_id}` dm_ok=`{dm_ok}`",
        )

    @z_server.command(name="nuke", description="DANGEROUS: Delete a guild's data, ban its owner, and leave the guild")
    @_owner_only()
    @app_commands.describe(
        guild_id="Guild ID to nuke",
        confirm="Type: NUKE <guild_id> (example: NUKE 123)",
        reason="Why you're nuking",
        notes="Optional notes (for logs)",
    )
    async def nuke(self, interaction: discord.Interaction, guild_id: str, confirm: str, reason: str = "manual", notes: str = ""):
        if not (guild_id or "").strip().isdigit():
            await _ephemeral(interaction, "Provide a numeric guild_id.")
            return
        gid = int(guild_id.strip())
        actor = int(getattr(interaction.user, "id", 0) or 0)

        expected = f"NUKE {gid}"
        if (confirm or "").strip() != expected:
            await _ephemeral(interaction, f"Confirmation mismatch. Type exactly: `{expected}`")
            return

        warn = (
            f"⚠️ **NUKE CONFIRMATION**\n"
            f"Guild: `{gid}`\n"
            f"Action: delete guild data + delete packs created by guild + ban guild owner + leave guild\n"
            f"Reason: `{(reason or '').strip()[:200]}`\n\n"
            f"Press **NUKE NOW** within 90 seconds to proceed."
        )
        view = _NukeConfirmView(bot=self.bot, actor_id=actor, guild_id=gid, reason=reason, notes=notes)
        try:
            await interaction.response.send_message(warn[:1900], ephemeral=True, view=view)
        except Exception:
            await _ephemeral(interaction, "Failed to show confirmation dialog.")


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashServer") is None:
        await bot.add_cog(SlashServer(bot))

