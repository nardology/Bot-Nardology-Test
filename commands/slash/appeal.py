from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.backpressure import get_redis_or_none
from utils.owner import is_bot_owner
from utils.mod_actions import unban_user, is_user_banned, get_user_ban_reason, get_nuke_warning


logger = logging.getLogger("bot.appeal")


def _utc_day() -> str:
    return time.strftime("%Y%m%d", time.gmtime(int(time.time())))


def _json_dumps(d: Dict[str, Any]) -> str:
    return json.dumps(d, separators=(",", ":"))


def _json_loads(raw: Any) -> Dict[str, Any]:
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="ignore")
        d = json.loads(str(raw))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def _ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        logger.exception("Failed sending appeal response")


def _is_guild_owner(interaction: discord.Interaction) -> bool:
    try:
        g = interaction.guild
        if not g:
            return False
        return int(getattr(g, "owner_id", 0) or 0) == int(getattr(interaction.user, "id", 0) or 0)
    except Exception:
        return False


async def _rate_limit_once_per_day(*, user_id: int) -> tuple[bool, str]:
    """
    1 appeal per UTC day per user. Fail-closed if Redis is unavailable (prevents spam).
    """
    r = await get_redis_or_none()
    if r is None:
        return False, "Appeals are temporarily unavailable. Please try again later."
    key = f"appeal:rl:{int(user_id)}:{_utc_day()}"
    try:
        # SET NX with expiry = 24h + buffer
        ok = await r.set(key, "1", nx=True, ex=90000)
        return (bool(ok), "ok" if ok else "You can only submit **1 appeal per day**.")
    except Exception:
        return False, "Appeals are temporarily unavailable. Please try again later."


async def _store_ticket(ticket: Dict[str, Any]) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        tid = str(ticket.get("ticket_id") or "")
        if not tid:
            return
        await r.set(f"appeal:ticket:{tid}", _json_dumps(ticket), ex=86400 * 30)
    except Exception:
        pass


async def _load_ticket(ticket_id: str) -> Dict[str, Any]:
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        raw = await r.get(f"appeal:ticket:{ticket_id}")
        if not raw:
            return {}
        return _json_loads(raw)
    except Exception:
        return {}


async def _update_ticket(ticket_id: str, updates: Dict[str, Any]) -> None:
    d = await _load_ticket(ticket_id)
    if not d:
        return
    d.update(updates)
    await _store_ticket(d)


class _DecisionModal(discord.ui.Modal):
    def __init__(self, *, ticket_id: str, action: str):
        super().__init__(title=f"Appeal {action.title()}")
        self.ticket_id = ticket_id
        self.action = action
        self.reason = discord.ui.TextInput(
            label="Optional reason / notes",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            placeholder="Why approve/deny? (optional)",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Only bot owners can decide.
        if not is_bot_owner(getattr(interaction.user, "id", 0)):
            await _ephemeral(interaction, "Owner only.")
            return

        ticket = await _load_ticket(self.ticket_id)
        if not ticket:
            await _ephemeral(interaction, "Ticket not found (may have expired).")
            return

        status = str(ticket.get("status") or "open")
        if status != "open":
            await _ephemeral(interaction, f"Ticket already `{status}`.")
            return

        decision_reason = str(self.reason.value or "").strip()

        # Apply decision
        appellant_id = int(ticket.get("user_id") or 0)
        guild_id = int(ticket.get("guild_id") or 0)

        if self.action == "approve":
            # Approve = unban the appellant user id (this reverses /ban_user and /nuke owner bans).
            try:
                await unban_user(user_id=appellant_id, by_user_id=int(getattr(interaction.user, "id", 0) or 0), reason=decision_reason)
            except Exception:
                pass
            await _update_ticket(
                self.ticket_id,
                {
                    "status": "approved",
                    "decided_at": int(time.time()),
                    "decided_by": int(getattr(interaction.user, "id", 0) or 0),
                    "decision_reason": decision_reason,
                },
            )
            msg = f"✅ Appeal approved for user `{appellant_id}` (guild `{guild_id or 0}`)."
        else:
            await _update_ticket(
                self.ticket_id,
                {
                    "status": "denied",
                    "decided_at": int(time.time()),
                    "decided_by": int(getattr(interaction.user, "id", 0) or 0),
                    "decision_reason": decision_reason,
                },
            )
            msg = f"❌ Appeal denied for user `{appellant_id}` (guild `{guild_id or 0}`)."

        if decision_reason:
            msg += f"\nReason: `{decision_reason}`"

        # Notify appellant (best-effort DM)
        try:
            user = await interaction.client.fetch_user(appellant_id)  # type: ignore[attr-defined]
            await user.send((msg + f"\nTicket: `{self.ticket_id}`")[:1900])
        except Exception:
            pass

        await _ephemeral(interaction, msg[:1900])


class _AppealDecisionView(discord.ui.View):
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
        await interaction.response.send_modal(_DecisionModal(ticket_id=self.ticket_id, action="approve"))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_DecisionModal(ticket_id=self.ticket_id, action="deny"))


class SlashAppeal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="appeal", description="Guild owner appeal for bans/nukes (1 per day)")
    @app_commands.describe(reason="Why should this be reversed?", guild_id="If in DMs, provide a guild id (optional)")
    async def appeal(self, interaction: discord.Interaction, reason: str, guild_id: str | None = None):
        uid = int(getattr(interaction.user, "id", 0) or 0)
        if not uid:
            await _ephemeral(interaction, "Could not determine your user id.")
            return

        # Only guild owners can appeal.
        target_gid = 0
        if interaction.guild:
            if not _is_guild_owner(interaction):
                await _ephemeral(interaction, "Only the **server owner** can use `/appeal`.")
                return
            target_gid = int(getattr(interaction.guild, "id", 0) or 0)
        else:
            # DM appeal: user must own at least one guild the bot is currently in,
            # OR provide a guild_id where a nuke warning exists pointing to them.
            gid_input = int(guild_id.strip()) if (guild_id and guild_id.strip().isdigit()) else 0
            owned = [int(getattr(g, "id", 0) or 0) for g in list(getattr(self.bot, "guilds", []) or []) if int(getattr(g, "owner_id", 0) or 0) == uid]
            if gid_input:
                # If bot is in that guild and user owns it, accept.
                if gid_input in owned:
                    target_gid = gid_input
                else:
                    # Otherwise, only accept if there's a stored nuke_warning referencing this owner_id.
                    warn = await get_nuke_warning(gid_input)
                    if int(warn.get("owner_id") or 0) == uid:
                        target_gid = gid_input
            else:
                if len(owned) == 1:
                    target_gid = owned[0]
                elif len(owned) > 1:
                    await _ephemeral(interaction, "You own multiple servers with the bot. Provide `guild_id=` to choose one.")
                    return
            if not target_gid:
                await _ephemeral(interaction, "You must run `/appeal` in your server, or provide a valid `guild_id=` in DMs.")
                return

        # Must be banned to appeal (keeps noise down)
        if not await is_user_banned(uid):
            await _ephemeral(interaction, "You are not currently banned. No appeal is needed.")
            return

        # Daily limit
        ok, msg = await _rate_limit_once_per_day(user_id=uid)
        if not ok:
            await _ephemeral(interaction, msg)
            return

        reason_txt = (reason or "").strip()
        if len(reason_txt) < 5:
            await _ephemeral(interaction, "Please provide a bit more detail (at least 5 characters).")
            return

        ban_reason = await get_user_ban_reason(uid)
        ticket_id = uuid.uuid4().hex[:12]

        ticket = {
            "ticket_id": ticket_id,
            "status": "open",
            "created_at": int(time.time()),
            "user_id": uid,
            "guild_id": int(target_gid),
            "appeal_reason": reason_txt[:1200],
            "current_ban_reason": (ban_reason or "")[:400],
        }
        await _store_ticket(ticket)

        # DM all bot owners
        view = _AppealDecisionView(ticket_id=ticket_id)
        sent = 0
        for oid in sorted(getattr(config, "BOT_OWNER_IDS", set()) or set()):
            try:
                owner = await self.bot.fetch_user(int(oid))
                content = (
                    f"📝 **New appeal** `{ticket_id}`\n"
                    f"User: `{uid}`\n"
                    f"Guild: `{int(target_gid)}`\n"
                    f"Ban reason: `{(ban_reason or 'n/a')}`\n"
                    f"Appeal: {reason_txt[:1200]}"
                )
                await owner.send(content[:1900], view=view)
                sent += 1
            except Exception:
                continue

        await _ephemeral(interaction, f"✅ Appeal submitted. Ticket `{ticket_id}`. Sent to `{sent}` owner inbox(es).")


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashAppeal") is None:
        await bot.add_cog(SlashAppeal(bot))

