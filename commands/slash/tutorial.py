# commands/slash/tutorial.py
"""Interactive multi-page setup tutorial for server administrators."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger("bot.tutorial")

# Each page is (title, description, color).  Descriptions use Discord markdown.
_PAGES: list[tuple[str, str, int]] = [
    (
        "Welcome to Bot-Nardology!",
        (
            "Thanks for adding the bot! This tutorial walks you through "
            "setting up your server in **6 quick steps**.\n\n"
            "**What this bot does:**\n"
            "Roll and collect characters with gacha mechanics, then talk to "
            "them using AI. Earn points through daily rewards, quests, and "
            "streaks. Climb leaderboards and unlock bonds.\n\n"
            "Use the **Next** button below to continue, or **Done** to close.\n\n"
            f"Need help? Join the [support server]({config.SUPPORT_SERVER_URL})."
        ),
        0x5865F2,
    ),
    (
        "Step 1 — AI Setup",
        (
            "Control where and how AI conversations happen.\n\n"
            "**Restrict AI to specific channels:**\n"
            "`/settings ai allow-channel` — Only allow `/talk` in chosen channels\n"
            "`/settings ai unallow-channel` — Remove a channel restriction\n"
            "`/settings ai list-channels` — See current channel rules\n\n"
            "**Safety controls:**\n"
            "`/settings ai safety-mode` — Toggle the safety filter\n"
            "`/settings ai block-topic` — Block specific topics from AI\n"
            "`/settings ai list-topics` — View blocked topics\n\n"
            "**Role-based access:**\n"
            "`/settings ai allow-role` — Restrict AI to certain roles\n"
            "`/settings ai block-role` — Block a role from using AI"
        ),
        0x57F287,
    ),
    (
        "Step 2 — Characters",
        (
            "Set up the character experience for your server.\n\n"
            "**Set a default character:**\n"
            "`/settings character` — Choose which character responds to `/talk` by default\n\n"
            "**Enable community packs:**\n"
            "`/packs marketplace` — Browse community-created character packs\n"
            "`/packs enable` — Activate a pack on your server\n"
            "`/packs enabled` — See which packs are active\n\n"
            "**Your members can:**\n"
            "- `/character roll` to collect characters\n"
            "- `/character select` to choose their active character\n"
            "- `/bond view` to check their relationship with a character"
        ),
        0xE67E22,
    ),
    (
        "Step 3 — Points & Economy",
        (
            "Your server has a built-in points economy.\n\n"
            "**How members earn points:**\n"
            "- `/points daily` — Daily claim with streak bonuses\n"
            "- `/points quests` — Complete quests for rewards\n"
            "- Streaks multiply rewards over time\n\n"
            "**What they can spend on:**\n"
            "- `/points shop` — Extra rolls, lucky boosters, inventory upgrades\n"
            "- `/points cosmetic-shop` — Profile cosmetics\n\n"
            "**Leaderboards:**\n"
            "`/leaderboard view` — Rankings for points, rolls, talk, bond XP, and more\n\n"
            "Points are global (shared across all servers), so members keep "
            "their progress everywhere."
        ),
        0xF1C40F,
    ),
    (
        "Step 4 — Moderation",
        (
            "Keep your server safe with these tools.\n\n"
            "**User management:**\n"
            "`/z_server ban_user` — Ban a user from the bot (server-wide)\n"
            "`/z_server unban_user` — Unban a user\n"
            "`/z_server check_user` — Check a user's status\n\n"
            "**Reporting:**\n"
            "`/report channel-set` — Set where reports go\n"
            "Members can use `/report send` to flag issues\n\n"
            "**Announcements:**\n"
            "`/settings announce channel` — Set an announcement channel\n"
            "`/z_server announce` — Broadcast a message to the channel\n\n"
            "**Emergency:**\n"
            "`/z_server disable_ai` — Instantly disable AI on your server\n"
            "`/z_server bot_disable` — Disable the bot entirely on your server"
        ),
        0xED4245,
    ),
    (
        "Step 5 — Premium",
        (
            "Pro subscriptions are per-user, not per-server. Members who "
            "have Pro get perks everywhere.\n\n"
            "**What Pro gives your members:**\n"
            "- 2x daily rolls (vs 1 free)\n"
            "- 20 character slots (vs 6 free)\n"
            "- AI conversation memory\n"
            "- Character streak DMs (characters message them!)\n"
            "- Custom character pack creation\n"
            "- Higher AI budgets and longer responses\n\n"
            + (
                "**Commands:**\n"
                "`/premium subscribe` — $4.99/month\n"
                "`/premium gift` — Gift Pro to a friend\n"
                "`/premium buy_points` — Buy points with real money\n\n"
                if config.PAYMENTS_ENABLED else
                "Subscriptions are coming soon — stay tuned!\n\n"
            )
            + "You're all set! Hit **Done** to close this tutorial. "
            "Use `/help` anytime for command reference."
        ),
        0xA855F7,
    ),
]


class _TutorialView(discord.ui.View):
    """Paginated embed view with Previous / Next / Done buttons."""

    def __init__(self, *, is_admin: bool, author_id: int):
        super().__init__(timeout=300)
        self.page = 0
        self.is_admin = is_admin
        self.author_id = author_id
        self._update_buttons()

    def _build_embed(self) -> discord.Embed:
        title, desc, color = _PAGES[self.page]
        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text=f"Page {self.page + 1}/{len(_PAGES)}")
        return embed

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(_PAGES) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This tutorial is for someone else. Run `/tutorial` yourself!",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, emoji="▶️")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(_PAGES) - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, emoji="✅")
    async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Tutorial complete!",
                description=(
                    "You're all set. Use `/help` anytime for a command reference.\n\n"
                    f"Questions? Join the [support server]({config.SUPPORT_SERVER_URL})."
                ),
                color=0x57F287,
            ),
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


class SlashTutorial(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="tutorial", description="Interactive setup guide for server admins")
    async def tutorial(self, interaction: discord.Interaction):
        is_admin = False
        if interaction.guild and interaction.guild_permissions.manage_guild:
            is_admin = True

        if not is_admin:
            embed = discord.Embed(
                title="Bot-Nardology Quick Start",
                description=(
                    "Welcome! Here's how to get started:\n\n"
                    "🐱 `/start` — Meet KAI, the bot mascot\n"
                    "🎲 `/character roll` — Roll for your first character\n"
                    "🎭 `/talk` — Talk to a character\n"
                    "🪙 `/points daily` — Claim your daily reward\n"
                    "❓ `/help` — Full command reference\n\n"
                    "Server admins can run `/tutorial` for the full setup guide."
                ),
                color=0x5865F2,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = _TutorialView(is_admin=True, author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashTutorial(bot))
