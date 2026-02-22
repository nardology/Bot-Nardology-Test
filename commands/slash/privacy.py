"""Slash commands for GDPR self-service: data export and account deletion."""
from __future__ import annotations

import io
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.audit import audit_log

log = logging.getLogger("privacy")

_EXPORT_COOLDOWN_SECONDS = 86_400  # 24 h


class _DeleteConfirmModal(discord.ui.Modal):
    """Requires the user to type DELETE to confirm account deletion."""

    def __init__(self, *, has_stripe_sub: bool, billing_url: str | None):
        super().__init__(title="Confirm Account Deletion")
        self.has_stripe_sub = has_stripe_sub
        self.billing_url = billing_url

        self.confirmation = discord.ui.TextInput(
            label='Type "DELETE" to confirm',
            style=discord.TextStyle.short,
            required=True,
            min_length=6,
            max_length=6,
            placeholder="DELETE",
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirmation.value.strip().upper() != "DELETE":
            await interaction.response.send_message(
                "Deletion cancelled — you did not type DELETE.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            from utils.privacy import delete_user_data
            summary = await delete_user_data(interaction.user.id)
        except Exception:
            log.exception("delete_user_data failed for user %s", interaction.user.id)
            await interaction.followup.send(
                "Something went wrong while deleting your data. Please contact support.",
                ephemeral=True,
            )
            return

        await audit_log(
            "privacy_delete",
            user_id=interaction.user.id,
            username=str(interaction.user),
            result="ok",
            fields=summary,
        )

        lines = [f"**{k}:** {v}" for k, v in summary.items() if v]
        body = "\n".join(lines) or "No data found."

        msg = f"Your data has been deleted.\n\n{body}"
        if self.has_stripe_sub and self.billing_url:
            msg += (
                "\n\n**Note:** Your Stripe subscription was not cancelled automatically. "
                f"Please cancel it here: {self.billing_url}"
            )
        elif self.has_stripe_sub:
            msg += (
                "\n\n**Note:** Your Stripe subscription was not cancelled automatically. "
                "Use `/premium manage` to open the billing portal and cancel it."
            )

        try:
            await interaction.user.send(msg[:2000])
        except Exception:
            pass

        await interaction.followup.send(
            "Your account data has been deleted. A summary was sent to your DMs.",
            ephemeral=True,
        )


class SlashPrivacy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    privacy_group = app_commands.Group(name="privacy", description="Manage your personal data (export / delete)")

    # ------------------------------------------------------------------
    # /privacy export
    # ------------------------------------------------------------------

    @privacy_group.command(name="export", description="Download all data we store about you (JSON file)")
    async def privacy_export(self, interaction: discord.Interaction):
        uid = interaction.user.id

        if await self._is_export_rate_limited(uid):
            await interaction.response.send_message(
                "You can only export your data once every 24 hours. Please try again later.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            from utils.privacy import export_user_data
            data = await export_user_data(uid)
        except Exception:
            log.exception("export_user_data failed for user %s", uid)
            await interaction.followup.send(
                "Something went wrong while exporting your data. Please contact support.",
                ephemeral=True,
            )
            return

        blob = json.dumps(data, indent=2, default=str).encode("utf-8")
        file = discord.File(io.BytesIO(blob), filename=f"your_data_{uid}.json")

        try:
            await interaction.user.send(
                "Here is all the data we store about you:",
                file=file,
            )
            dm_ok = True
        except Exception:
            dm_ok = False

        await self._mark_export_used(uid)

        await audit_log(
            "privacy_export",
            user_id=uid,
            username=str(interaction.user),
            result="ok" if dm_ok else "dm_failed",
        )

        if dm_ok:
            await interaction.followup.send(
                "Your data export has been sent to your DMs.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I couldn't DM you the file. Please enable DMs from server members and try again.",
                ephemeral=True,
                file=discord.File(io.BytesIO(blob), filename=f"your_data_{uid}.json"),
            )

    # ------------------------------------------------------------------
    # /privacy delete
    # ------------------------------------------------------------------

    @privacy_group.command(name="delete", description="Permanently delete all your data")
    async def privacy_delete(self, interaction: discord.Interaction):
        uid = interaction.user.id

        has_stripe_sub = False
        billing_url: str | None = None

        try:
            from utils.premium import get_premium_details
            details = await get_premium_details(uid)
            stripe_sub = details.get("stripe_subscription_id")
            stripe_cust = details.get("stripe_customer_id")
            if stripe_sub and details.get("tier") == "pro" and details.get("source") == "stripe":
                has_stripe_sub = True
                if stripe_cust:
                    try:
                        from core.stripe_checkout import get_billing_portal_url
                        billing_url = await get_billing_portal_url(stripe_customer_id=str(stripe_cust))
                    except Exception:
                        pass
        except Exception:
            pass

        modal = _DeleteConfirmModal(has_stripe_sub=has_stripe_sub, billing_url=billing_url)
        await interaction.response.send_modal(modal)

    # ------------------------------------------------------------------
    # Rate-limit helpers (Redis-backed, best-effort)
    # ------------------------------------------------------------------

    @staticmethod
    async def _is_export_rate_limited(user_id: int) -> bool:
        try:
            from utils.backpressure import get_redis_or_none
            r = await get_redis_or_none()
            if r is None:
                return False
            val = await r.get(f"privacy:export:{int(user_id)}")
            return bool(val)
        except Exception:
            return False

    @staticmethod
    async def _mark_export_used(user_id: int) -> None:
        try:
            from utils.backpressure import get_redis_or_none
            r = await get_redis_or_none()
            if r is None:
                return
            await r.set(f"privacy:export:{int(user_id)}", "1", ex=_EXPORT_COOLDOWN_SECONDS)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashPrivacy(bot))
