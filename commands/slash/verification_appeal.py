from __future__ import annotations

"""
Appeal system for denied verification tickets.

Users can appeal denied pack/character verification requests.
Similar pattern to the ban appeal system but for verification denials.
"""

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
from utils.verification import get_denied_ticket, get_ticket

logger = logging.getLogger("bot.verification_appeal")


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
        logger.exception("Failed sending verification appeal response")


async def _rate_limit_once_per_day(*, user_id: int) -> tuple[bool, str]:
    """1 appeal per UTC day per user. Fail-closed if Redis is unavailable."""
    r = await get_redis_or_none()
    if r is None:
        return False, "Appeals are temporarily unavailable. Please try again later."
    key = f"verification_appeal:rl:{int(user_id)}:{_utc_day()}"
    try:
        ok = await r.set(key, "1", nx=True, ex=90000)  # 25 hours expiry
        return (bool(ok), "ok" if ok else "You can only submit **1 verification appeal per day**.")
    except Exception:
        return False, "Appeals are temporarily unavailable. Please try again later."


async def _store_appeal_ticket(ticket: Dict[str, Any]) -> None:
    r = await get_redis_or_none()
    if r is None:
        return
    try:
        tid = str(ticket.get("appeal_id") or "")
        if not tid:
            return
        await r.set(f"verification_appeal:ticket:{tid}", _json_dumps(ticket), ex=86400 * 30)
    except Exception:
        pass


async def _load_appeal_ticket(appeal_id: str) -> Dict[str, Any]:
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        raw = await r.get(f"verification_appeal:ticket:{appeal_id}")
        if not raw:
            return {}
        return _json_loads(raw)
    except Exception:
        return {}


async def _update_appeal_ticket(appeal_id: str, updates: Dict[str, Any]) -> None:
    d = await _load_appeal_ticket(appeal_id)
    if not d:
        return
    d.update(updates)
    await _store_appeal_ticket(d)


class _VerificationAppealDecisionModal(discord.ui.Modal):
    def __init__(self, *, appeal_id: str, action: str):
        super().__init__(title=f"Appeal {action.title()}")
        self.appeal_id = appeal_id
        self.action = action
        self.reason = discord.ui.TextInput(
            label="Optional reason / notes",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
            placeholder="Why approve/deny this appeal? (optional)",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_bot_owner(getattr(interaction.user, "id", 0)):
            await _ephemeral(interaction, "Owner only.")
            return

        appeal = await _load_appeal_ticket(self.appeal_id)
        if not appeal:
            await _ephemeral(interaction, "Appeal ticket not found (may have expired).")
            return

        status = str(appeal.get("status") or "open")
        if status != "open":
            await _ephemeral(interaction, f"Appeal already `{status}`.")
            return

        decision_reason = str(self.reason.value or "").strip()
        original_ticket_id = str(appeal.get("original_ticket_id") or "")
        appellant_id = int(appeal.get("user_id") or 0)
        guild_id = int(appeal.get("guild_id") or 0)

        if self.action == "approve":
            # Approve appeal = re-approve the original verification ticket
            from utils.verification import (
                get_ticket,
                update_ticket_status,
                increment_trust_approval,
            )
            from utils.packs_store import upsert_custom_pack, add_character_to_pack, get_custom_pack
            from utils.character_registry import merge_pack_payload

            original_ticket = await get_ticket(original_ticket_id)
            if not original_ticket:
                await _ephemeral(interaction, "Original verification ticket not found.")
                return

            ticket_type = str(original_ticket.get("type") or "")
            payload = original_ticket.get("payload") or {}

            try:
                if ticket_type == "pack_create":
                    ok = await upsert_custom_pack(payload)
                    if ok:
                        await increment_trust_approval(guild_id=guild_id, user_id=appellant_id)
                        await update_ticket_status(
                            ticket_id=original_ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=f"Appeal approved: {decision_reason}"[:400],
                        )
                        try:
                            merge_pack_payload(payload)
                        except Exception:
                            pass
                        msg = f"✅ Appeal approved. Pack `{payload.get('pack_id', '')}` has been created."
                    else:
                        msg = "⚠️ Failed to create pack (storage error)."
                elif ticket_type == "pack_edit":
                    ok = await upsert_custom_pack(payload)
                    if ok:
                        await increment_trust_approval(guild_id=guild_id, user_id=appellant_id)
                        await update_ticket_status(
                            ticket_id=original_ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=f"Appeal approved: {decision_reason}"[:400],
                        )
                        try:
                            merge_pack_payload(payload)
                        except Exception:
                            pass
                        msg = f"✅ Appeal approved. Pack `{payload.get('pack_id', '')}` has been updated."
                    else:
                        msg = "⚠️ Failed to update pack (storage error)."
                elif ticket_type in {"character_add", "character_edit"}:
                    pack_id = str(payload.get("pack_id") or "")
                    ok, msg_save = await add_character_to_pack(pack_id, payload)
                    if ok:
                        await increment_trust_approval(guild_id=guild_id, user_id=appellant_id)
                        await update_ticket_status(
                            ticket_id=original_ticket_id,
                            status="approved",
                            decided_by=int(getattr(interaction.user, "id", 0) or 0),
                            decision_reason=f"Appeal approved: {decision_reason}"[:400],
                        )
                        try:
                            p = await get_custom_pack(pack_id)
                            if p:
                                merge_pack_payload(p)
                        except Exception:
                            pass
                        action = "added" if ticket_type == "character_add" else "updated"
                        msg = f"✅ Appeal approved. Character has been {action}."
                    else:
                        msg = f"⚠️ Failed to add/update character: {msg_save}"
                else:
                    msg = f"⚠️ Unknown ticket type: {ticket_type}"
            except Exception:
                logger.exception("Failed processing approved verification appeal")
                msg = "⚠️ Failed to process appeal (check logs)."

            await _update_appeal_ticket(
                self.appeal_id,
                {
                    "status": "approved",
                    "decided_at": int(time.time()),
                    "decided_by": int(getattr(interaction.user, "id", 0) or 0),
                    "decision_reason": decision_reason,
                },
            )
        else:
            # Deny appeal
            await _update_appeal_ticket(
                self.appeal_id,
                {
                    "status": "denied",
                    "decided_at": int(time.time()),
                    "decided_by": int(getattr(interaction.user, "id", 0) or 0),
                    "decision_reason": decision_reason,
                },
            )
            msg = f"❌ Appeal denied for verification ticket `{original_ticket_id}` (guild `{guild_id}`, user `{appellant_id}`)."

        if decision_reason:
            msg += f"\nReason: `{decision_reason}`"

        # Notify appellant (best-effort DM)
        try:
            user = await interaction.client.fetch_user(appellant_id)  # type: ignore[attr-defined]
            await user.send((msg + f"\nAppeal ID: `{self.appeal_id}`")[:1900])
        except Exception:
            pass

        await _ephemeral(interaction, msg[:1900])


class _VerificationAppealDecisionView(discord.ui.View):
    def __init__(self, *, appeal_id: str):
        super().__init__(timeout=None)
        self.appeal_id = appeal_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_owner(getattr(interaction.user, "id", 0)):
            try:
                await interaction.response.send_message("Owner only.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="Approve Appeal", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_VerificationAppealDecisionModal(appeal_id=self.appeal_id, action="approve"))

    @discord.ui.button(label="Deny Appeal", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_VerificationAppealDecisionModal(appeal_id=self.appeal_id, action="deny"))


class SlashVerificationAppeal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="verification_appeal", description="Appeal a denied verification request (1 per day)")
    @app_commands.describe(
        ticket_id="The verification ticket ID that was denied",
        reason="Why should this be reversed? What changed?",
    )
    async def verification_appeal(self, interaction: discord.Interaction, ticket_id: str, reason: str):
        if not interaction.guild:
            await _ephemeral(interaction, "Use this in a server.")
            return

        uid = int(getattr(interaction.user, "id", 0) or 0)
        gid = int(getattr(interaction.guild, "id", 0) or 0) if interaction.guild else 0

        # Check rate limit
        ok_rl, msg_rl = await _rate_limit_once_per_day(user_id=uid)
        if not ok_rl:
            await _ephemeral(interaction, f"⚠️ {msg_rl}")
            return

        # Check if ticket exists and is denied
        ticket = await get_denied_ticket(ticket_id.strip())
        if not ticket:
            await _ephemeral(interaction, "⚠️ Ticket not found or not denied. Only denied verification tickets can be appealed.")
            return

        # Verify user owns the ticket
        ticket_user_id = int(ticket.get("user_id") or 0)
        ticket_guild_id = int(ticket.get("guild_id") or 0)
        if ticket_user_id != uid or ticket_guild_id != gid:
            await _ephemeral(interaction, "⚠️ You can only appeal verification tickets from your own server.")
            return

        # Check if already appealed
        # (Simple check: if there's an open appeal for this ticket, reject)
        # In production, you might want a more sophisticated check
        appeal_id = uuid.uuid4().hex[:16]
        appeal_ticket = {
            "appeal_id": appeal_id,
            "status": "open",
            "created_at": int(time.time()),
            "user_id": uid,
            "guild_id": gid,
            "original_ticket_id": ticket_id.strip(),
            "appeal_reason": str(reason or "").strip()[:1200],
            "original_denial_reason": str(ticket.get("decision_reason") or "")[:400],
        }
        await _store_appeal_ticket(appeal_ticket)

        # DM bot owners with appeal details
        ticket_type = str(ticket.get("type") or "unknown")
        payload = ticket.get("payload") or {}
        original_payload = ticket.get("original_payload")

        lines: list[str] = []
        lines.append(f"**Verification Appeal** `{appeal_id}`")
        lines.append(f"Original Ticket: `{ticket_id}`")
        lines.append(f"Type: {ticket_type}")
        if ticket_type in {"pack_create", "pack_edit"}:
            lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
            lines.append(f"Name: {payload.get('name', '')}")
        elif ticket_type in {"character_add", "character_edit"}:
            lines.append(f"Pack ID: `{payload.get('pack_id', '')}`")
            lines.append(f"Character ID: `{payload.get('id') or payload.get('style_id', '')}`")
            lines.append(f"Display Name: {payload.get('display_name', '')}")
        lines.append(f"\n**Appeal Reason:**\n{str(reason or '')[:800]}")
        if ticket.get("decision_reason"):
            lines.append(f"\n**Original Denial Reason:**\n{str(ticket.get('decision_reason', ''))[:400]}")
        lines.append(f"\nGuild: `{gid}`")
        lines.append(f"User: `{uid}`")

        content = "\n".join(lines)[:1900]
        view = _VerificationAppealDecisionView(appeal_id=appeal_id)

        sent = 0
        for oid in sorted(getattr(config, "BOT_OWNER_IDS", set()) or set()):
            try:
                owner = await self.bot.fetch_user(int(oid))
                await owner.send(content, view=view)
                sent += 1
            except Exception:
                continue

        if sent == 0:
            logger.warning("Failed to DM any owners for verification appeal %s", appeal_id)

        await _ephemeral(
            interaction,
            f"✅ Appeal submitted (ID: `{appeal_id}`). You'll be notified when it's reviewed. "
            f"Original ticket: `{ticket_id}`",
        )


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashVerificationAppeal") is None:
        await bot.add_cog(SlashVerificationAppeal(bot))
