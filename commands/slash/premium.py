# commands/slash/premium.py
"""Slash commands for Stripe-powered premium subscriptions, gifting, and points purchases."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.premium import get_premium_tier, get_premium_details

log = logging.getLogger("bot.premium")


# ---------------------------------------------------------------------------
# Points bundle definitions (label, env-var price ID, points amount, price display, savings desc)
# ---------------------------------------------------------------------------

_POINTS_BUNDLES: list[tuple[str, str | None, int, str, str]] = [
    ("500 Points",    config.STRIPE_PRICE_POINTS_500,   500,    "$4.99",  "500 points"),
    ("1,000 Points",  config.STRIPE_PRICE_POINTS_1000,  1_000,  "$8.99",  "Save ~10% vs base rate"),
    ("2,500 Points",  config.STRIPE_PRICE_POINTS_2500,  2_500,  "$21.99", "Save ~12% vs base rate"),
    ("5,000 Points",  config.STRIPE_PRICE_POINTS_5000,  5_000,  "$43.50", "Save ~13% vs base rate"),
    ("10,000 Points", config.STRIPE_PRICE_POINTS_10000, 10_000, "$84.99", "Save ~15% vs base rate"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send_error(interaction: discord.Interaction, msg: str) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        pass


async def _send_ephemeral(interaction: discord.Interaction, msg: str = "", *, embed: discord.Embed | None = None, view: discord.ui.View | None = None) -> None:
    try:
        kwargs: dict = {"ephemeral": True}
        if msg:
            kwargs["content"] = msg
        if embed:
            kwargs["embed"] = embed
        if view:
            kwargs["view"] = view
        if not interaction.response.is_done():
            await interaction.response.send_message(**kwargs)
        else:
            await interaction.followup.send(**kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Points bundle select menu
# ---------------------------------------------------------------------------

class PointsBundleSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int, username: str):
        self.guild_id = guild_id
        self.user_id_val = user_id
        self.username = username

        options = []
        for label, price_id, points, price_display, savings_desc in _POINTS_BUNDLES:
            if price_id:  # Only show bundles that have a configured price ID
                options.append(discord.SelectOption(
                    label=f"{label} \u2014 {price_display}",
                    value=f"{price_id}:{points}",
                    description=savings_desc,
                ))

        if not options:
            options = [discord.SelectOption(label="No bundles configured", value="none")]

        super().__init__(
            placeholder="Choose a points bundle...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id_val:
            await _send_error(interaction, "This menu is not for you.")
            return

        val = self.values[0]
        if val == "none":
            await _send_error(interaction, "No points bundles are configured yet.")
            return

        price_id, points_str = val.split(":", 1)
        points = int(points_str)

        await interaction.response.defer(ephemeral=True)

        try:
            from core.stripe_checkout import create_points_checkout
            url = await create_points_checkout(
                guild_id=self.guild_id,
                user_id=self.user_id_val,
                username=self.username,
                price_id=price_id,
                points_amount=points,
            )
        except Exception:
            log.exception("Failed to create points checkout")
            await interaction.followup.send(
                "Something went wrong creating the checkout. Please try again later.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Purchase Points",
            description=(
                f"Click the button below to purchase **{points:,} points**.\n\n"
                f"You'll be taken to a secure Stripe checkout page."
            ),
            color=discord.Color.green(),
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Complete Purchase",
            url=url,
            style=discord.ButtonStyle.link,
            emoji="\U0001f4b3",  # credit card emoji
        ))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PointsBundleView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, username: str):
        super().__init__(timeout=120)
        self.add_item(PointsBundleSelect(guild_id, user_id, username))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class PremiumCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    premium = app_commands.Group(name="premium", description="Manage your personal Bot-Nardology Pro subscription, gift premium, and buy points")

    # ---- /premium subscribe ----
    @premium.command(name="subscribe", description="Subscribe to Bot-Nardology Pro ($4.99/month) for yourself")
    async def premium_subscribe(self, interaction: discord.Interaction) -> None:
        if not config.PAYMENTS_ENABLED:
            await _send_error(interaction, "Payments are not available right now. Stay tuned!")
            return
        if not config.STRIPE_SECRET_KEY or not config.STRIPE_PRICE_PRO_MONTHLY:
            await _send_error(interaction, "Payments are not configured yet. Please try again later.")
            return

        user_id = int(interaction.user.id)

        # Check if already Pro.
        tier = await get_premium_tier(user_id)
        if tier == "pro":
            await _send_error(interaction, "You already have **Pro**! Use `/premium status` to see details.")
            return

        await interaction.response.defer(ephemeral=True)

        try:
            from core.stripe_checkout import create_pro_checkout
            url = await create_pro_checkout(
                user_id=user_id,
                username=str(interaction.user),
            )
        except Exception:
            log.exception("Failed to create pro checkout")
            await interaction.followup.send(
                "Something went wrong. Please try again later.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Bot-Nardology Pro",
            description=(
                "Upgrade to **Pro** for **$4.99/month**!\n\n"
                "**What you get:**\n"
                "\u2022 3x character rolls per window\n"
                "\u2022 10 inventory slots (instead of 3)\n"
                "\u2022 Longer AI responses (1,900 chars)\n"
                "\u2022 Conversation memory\n"
                "\u2022 Public AI replies\n"
                "\u2022 5x daily talk budget\n"
                "\u2022 Create custom character packs\n"
                "\u2022 Up to 250 custom characters\n"
                "\u2022 Exclusive packs access\n"
                "\u2022 Higher rate limits\n\n"
                "Your subscription is **personal** \u2014 Pro follows you across all servers.\n\n"
                "Click the button below to subscribe.\n\n"
                f"[Terms of Service]({config.TERMS_OF_SERVICE_URL}) · "
                f"[Refund Policy]({config.TERMS_OF_SERVICE_URL}#refunds)"
            ),
            color=discord.Color.gold(),
        )

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Subscribe to Pro",
            url=url,
            style=discord.ButtonStyle.link,
            emoji="\u2b50",  # star emoji
        ))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ---- /premium status ----
    @premium.command(name="status", description="View your personal premium status")
    async def premium_status(self, interaction: discord.Interaction) -> None:
        user_id = int(interaction.user.id)
        details = await get_premium_details(user_id)

        tier = details["tier"]
        source = details["source"]
        period_end = details["subscription_period_end"]
        gifted_by = details["gifted_by_user_id"]

        if tier == "pro":
            embed = discord.Embed(
                title="Bot-Nardology Pro",
                description="You have **Pro** active!",
                color=discord.Color.gold(),
            )
            if source == "stripe":
                embed.add_field(name="Source", value="Stripe Subscription", inline=True)
            elif source == "gift":
                embed.add_field(name="Source", value="Gift", inline=True)
            elif source and source.startswith("trial"):
                embed.add_field(name="Source", value="Free Trial", inline=True)
            else:
                embed.add_field(name="Source", value=source or "Manual", inline=True)

            if period_end:
                try:
                    if period_end.tzinfo is None:
                        period_end = period_end.replace(tzinfo=timezone.utc)
                    ts = int(period_end.timestamp())
                    embed.add_field(name="Renews / Expires", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=True)
                except Exception:
                    pass

            if gifted_by:
                embed.add_field(name="Gifted By", value=f"<@{gifted_by}>", inline=True)
        else:
            if config.PAYMENTS_ENABLED:
                free_desc = (
                    "You are on the **free** tier.\n\n"
                    "Use `/premium subscribe` to upgrade to Pro for $4.99/month!\n"
                    "Pro is personal \u2014 it follows you across all servers."
                )
            else:
                free_desc = (
                    "You are on the **free** tier.\n\n"
                    "Pro upgrades are coming soon \u2014 stay tuned!"
                )
            embed = discord.Embed(
                title="Bot-Nardology \u2014 Free Tier",
                description=free_desc,
                color=discord.Color.greyple(),
            )

        await _send_ephemeral(interaction, embed=embed)

    # ---- /premium cancel ----
    @premium.command(name="cancel", description="Manage or cancel your Pro subscription")
    async def premium_cancel(self, interaction: discord.Interaction) -> None:
        if not config.PAYMENTS_ENABLED:
            await _send_error(interaction, "Payments are not available right now. Stay tuned!")
            return
        user_id = int(interaction.user.id)
        details = await get_premium_details(user_id)

        stripe_cust_id = details.get("stripe_customer_id")
        if not stripe_cust_id or details.get("source") != "stripe":
            await _send_error(
                interaction,
                "You don't have an active Stripe subscription. "
                "If Pro was activated via trial, gift, or manually, contact the bot owner.",
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            from core.stripe_checkout import get_billing_portal_url
            url = await get_billing_portal_url(stripe_customer_id=stripe_cust_id)
        except Exception:
            log.exception("Failed to create billing portal session")
            await interaction.followup.send(
                "Something went wrong. Please try again later.",
                ephemeral=True,
            )
            return

        if not url:
            await interaction.followup.send(
                "Could not generate a billing portal link. Please try again later.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Manage Subscription",
            description=(
                "Click the button below to open the Stripe billing portal.\n\n"
                "From there you can:\n"
                "\u2022 Cancel your subscription\n"
                "\u2022 Update your payment method\n"
                "\u2022 View invoices\n\n"
                "**Note:** If you cancel, Pro stays active until the end of your current billing period."
            ),
            color=discord.Color.orange(),
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Open Billing Portal",
            url=url,
            style=discord.ButtonStyle.link,
            emoji="\U0001f4b3",  # credit card emoji
        ))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ---- /premium gift ----
    @premium.command(name="gift", description="Gift Bot-Nardology Pro to another user")
    @app_commands.describe(
        user="The user to gift premium to",
        months="Number of months to gift",
    )
    @app_commands.choices(months=[
        app_commands.Choice(name="1 month ($4.99)", value=1),
        app_commands.Choice(name="3 months ($14.97)", value=3),
        app_commands.Choice(name="6 months ($29.94)", value=6),
        app_commands.Choice(name="12 months ($59.88)", value=12),
    ])
    async def premium_gift(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        months: app_commands.Choice[int],
    ) -> None:
        if not config.PAYMENTS_ENABLED:
            await _send_error(interaction, "Payments are not available right now. Stay tuned!")
            return
        if not config.STRIPE_SECRET_KEY or not config.STRIPE_PRICE_PRO_MONTHLY:
            await _send_error(interaction, "Payments are not configured yet. Please try again later.")
            return

        gifter_id = int(interaction.user.id)
        recipient_id = int(user.id)
        month_count = months.value

        if recipient_id == gifter_id:
            await _send_error(interaction, "You can't gift premium to yourself! Use `/premium subscribe` instead.")
            return

        if user.bot:
            await _send_error(interaction, "You can't gift premium to a bot.")
            return

        await interaction.response.defer(ephemeral=True)

        try:
            from core.stripe_checkout import create_gift_checkout
            url = await create_gift_checkout(
                gifter_user_id=gifter_id,
                recipient_user_id=recipient_id,
                months=month_count,
                username=str(interaction.user),
            )
        except Exception:
            log.exception("Failed to create gift checkout")
            await interaction.followup.send(
                "Something went wrong. Please try again later.",
                ephemeral=True,
            )
            return

        total_price = f"${4.99 * month_count:.2f}"
        embed = discord.Embed(
            title="\U0001f381 Gift Bot-Nardology Pro",
            description=(
                f"Gift **{month_count} month{'s' if month_count != 1 else ''}** "
                f"of Pro to **{user.display_name}** for **{total_price}**.\n\n"
                f"Once payment completes, {user.mention} will receive Pro immediately "
                f"and get a DM notification.\n\n"
                f"Click the button below to complete the gift."
            ),
            color=discord.Color.purple(),
        )

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Complete Gift",
            url=url,
            style=discord.ButtonStyle.link,
            emoji="\U0001f381",  # gift emoji
        ))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ---- /premium buy_points ----
    @premium.command(name="buy_points", description="Purchase points with real money")
    async def premium_buy_points(self, interaction: discord.Interaction) -> None:
        if not config.PAYMENTS_ENABLED:
            await _send_error(interaction, "Payments are not available right now. Stay tuned!")
            return
        if not interaction.guild:
            await _send_error(interaction, "This command must be used in a server.")
            return

        if not config.STRIPE_SECRET_KEY:
            await _send_error(interaction, "Payments are not configured yet. Please try again later.")
            return

        # Check that at least one bundle is configured.
        available = [(l, p, pts, d, s) for l, p, pts, d, s in _POINTS_BUNDLES if p]
        if not available:
            await _send_error(interaction, "No points bundles are available right now.")
            return

        guild_id = int(interaction.guild.id)
        user_id = int(interaction.user.id)
        username = str(interaction.user)

        embed = discord.Embed(
            title="Buy Points",
            description=(
                "Select a points bundle from the dropdown below.\n\n"
                "**Bigger bundles = better value!**\n"
                "Points are added to your **global** wallet instantly after payment."
            ),
            color=discord.Color.green(),
        )

        view = PointsBundleView(guild_id, user_id, username)
        await _send_ephemeral(interaction, embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PremiumCog(bot))
