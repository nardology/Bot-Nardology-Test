# commands/slash/feedback.py
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.audit import audit_log
from utils.feedback_store import count_feedback_since, insert_feedback
from utils.premium import get_feedback_caps

logger = logging.getLogger("bot.feedback")

KIND_CHOICES = [
    app_commands.Choice(name="Bug", value="bug"),
    app_commands.Choice(name="Recommendation", value="recommendation"),
    app_commands.Choice(name="Other", value="other"),
]

ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


class SlashFeedback(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _send_private(self, interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            pass

    async def _dm_owners(self, embed: discord.Embed, file: Optional[discord.File]) -> bool:
        owners = sorted(getattr(config, "BOT_OWNER_IDS", set()) or [])
        if not owners:
            logger.warning("No BOT_OWNER_IDS configured; cannot DM feedback.")
            return False

        for owner_id in owners:
            try:
                user = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                if user is None:
                    continue
                if file is not None:
                    await user.send(embed=embed, file=file)
                else:
                    await user.send(embed=embed)
                return True
            except Exception:
                logger.exception("Failed to DM owner_id=%s", owner_id)
                continue
        return False

    @app_commands.command(name="feedback", description="Send feedback to the bot owner (private)")
    @app_commands.describe(
        kind="What type of feedback is this?",
        message="What should the owner know?",
        attachment="Optional image (png/jpg/webp/gif)",
    )
    @app_commands.choices(kind=KIND_CHOICES)
    async def feedback(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        message: str,
        attachment: Optional[discord.Attachment] = None,
    ):
        if interaction.guild is None:
            await self._send_private(interaction, "Use this command in a server, not DMs.")
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        caps = await get_feedback_caps(user_id)

        msg = (message or "").strip()
        if not msg:
            await self._send_private(interaction, "Please type a message.")
            return

        if len(msg) > caps.max_chars:
            await self._send_private(
                interaction,
                f"That message is too long (**{len(msg)}** chars). Max is **{caps.max_chars}** for your tier.",
            )
            return

        # Daily limit: UTC midnight -> midnight
        now_utc = datetime.now(timezone.utc)
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        used_today = await count_feedback_since(guild_id=guild_id, user_id=user_id, since_utc=day_start)
        if used_today >= caps.daily_max:
            await self._send_private(
                interaction,
                f"You've hit the daily /feedback limit (**{caps.daily_max}/day**). Try again tomorrow (UTC).",
            )
            return

        # Attachment limits (check BEFORE reading bytes)
        discord_file: Optional[discord.File] = None
        attachment_name: Optional[str] = None
        attachment_size: Optional[int] = None
        attachment_ct: Optional[str] = None

        if attachment is not None:
            attachment_name = attachment.filename or "attachment"
            attachment_size = int(getattr(attachment, "size", 0) or 0)
            attachment_ct = getattr(attachment, "content_type", None)

            if attachment_size <= 0:
                await self._send_private(interaction, "That attachment looks invalid (size unknown).")
                return

            if attachment_size > caps.max_file_bytes:
                await self._send_private(
                    interaction,
                    f"That file is too large (**{attachment_size:,} bytes**). "
                    f"Max is **{caps.max_file_bytes:,} bytes** for your tier.",
                )
                return

            if attachment_ct not in ALLOWED_CONTENT_TYPES:
                await self._send_private(
                    interaction,
                    "Only images are allowed for /feedback attachments (png/jpg/webp/gif).",
                )
                return

        # Ack quickly to avoid the 3s timeout
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except Exception:
            pass

        # Build embed for DM/logs
        kind_value = kind.value
        ts = now_utc.isoformat()

        embed = discord.Embed(
            title="New /feedback",
            description=msg,
            timestamp=now_utc,
        )
        embed.add_field(name="Type", value=kind_value, inline=True)
        embed.add_field(name="User", value=f"{interaction.user} (id={user_id})", inline=False)
        embed.add_field(
            name="Guild / Channel",
            value=(
                f"{interaction.guild.name} (id={guild_id})\n"
                f"#{getattr(interaction.channel, 'name', 'unknown')} (id={interaction.channel_id})"
            ),
            inline=False,
        )
        if attachment is not None:
            embed.add_field(
                name="Attachment",
                value=f"{attachment_name} • {attachment_size:,} bytes • {attachment_ct}",
                inline=False,
            )
        embed.set_footer(text=f"utc={ts}")

        # Read attachment only after passing checks
        if attachment is not None:
            try:
                data = await attachment.read()
                discord_file = discord.File(fp=io.BytesIO(data), filename=attachment_name or "attachment")
            except Exception:
                logger.exception("Failed to read feedback attachment")
                discord_file = None

        # Store in DB (for daily limit + history)
        # Build attachments list compatible with utils.feedback_store.insert_feedback
        # which accepts an optional list of dicts.  Each dict can contain
        # metadata about the attachment.  We include name, size and
        # content_type so that downstream consumers have context.  The
        # feedback store does not support arbitrary keyword arguments such
        # as kind, attachment_name or attachment_bytes; therefore we pass
        # only supported parameters.
        try:
            attachments_list = []
            if attachment is not None:
                attachments_list.append({
                    "name": attachment_name,
                    "size": attachment_size,
                    "content_type": attachment_ct,
                })
            await insert_feedback(
                guild_id=guild_id,
                channel_id=int(interaction.channel_id),
                user_id=user_id,
                message=msg,
                attachments=attachments_list,
            )
        except Exception:
            logger.exception("Failed to insert feedback submission")

        # Always write an audit log entry
        audit_log(
            "FEEDBACK_SUBMITTED",
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            user_id=user_id,
            username=getattr(interaction.user, "name", "unknown"),
            command="feedback",
            result="success",
            fields={
                "kind": kind_value,
                "message_len": len(msg),
                "has_attachment": bool(attachment),
                "attachment_name": attachment_name,
                "attachment_bytes": attachment_size,
                "attachment_content_type": attachment_ct,
                "daily_used_before_submit": used_today,
                "daily_limit": caps.daily_max,
            },
        )

        logger.info(
            "Feedback submitted guild=%s user=%s kind=%s len=%s",
            guild_id,
            user_id,
            kind_value,
            len(msg),
        )

        dm_sent = await self._dm_owners(embed, discord_file)

        if dm_sent:
            await self._send_private(interaction, "✅ Sent! Thanks — I’ll take a look.")
        else:
            await self._send_private(
                interaction,
                "✅ Logged! I couldn’t DM the owner (DMs might be closed), but it’s in the bot logs.",
            )


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashFeedback") is None:
        await bot.add_cog(SlashFeedback(bot))


