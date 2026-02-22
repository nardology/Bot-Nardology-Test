# commands/slash/leaderboard.py
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.ui import safe_ephemeral_send, safe_send_embed
from utils.leaderboard import (
    get_leaderboard,
    get_user_rank,
    is_opted_out,
    CATEGORY_POINTS,
    CATEGORY_ROLLS,
    CATEGORY_TALK,
    CATEGORY_BOND,
    CATEGORY_CHARACTERS,
    CATEGORY_STREAK,
    CATEGORY_ACTIVITY,
    CATEGORY_CHARACTER_STREAK,
    PERIOD_ALLTIME,
    PERIOD_DAILY,
    PERIOD_WEEKLY,
    PERIOD_MONTHLY,
    GLOBAL_GUILD_ID,
)

logger = logging.getLogger("bot.leaderboard")

# Category display names
CATEGORY_NAMES = {
    CATEGORY_POINTS: "Points",
    CATEGORY_ROLLS: "Rolls",
    CATEGORY_TALK: "Talk Calls",
    CATEGORY_BOND: "Bond XP",
    CATEGORY_CHARACTERS: "Characters Owned",
    CATEGORY_STREAK: "Daily Streak",
    CATEGORY_ACTIVITY: "Days Active",
    CATEGORY_CHARACTER_STREAK: "Character Streak",
}

# Period display names
PERIOD_NAMES = {
    PERIOD_ALLTIME: "All Time",
    PERIOD_DAILY: "Daily",
    PERIOD_WEEKLY: "Weekly",
    PERIOD_MONTHLY: "Monthly",
}

def _format_score(category: str, score: float) -> str:
    """Format score based on category."""
    if category == CATEGORY_POINTS:
        return f"{int(score):,}"
    elif category in (CATEGORY_ROLLS, CATEGORY_TALK, CATEGORY_CHARACTERS, CATEGORY_ACTIVITY):
        return f"{int(score):,}"
    elif category == CATEGORY_BOND:
        return f"{int(score):,} XP"
    elif category == CATEGORY_STREAK:
        return f"{int(score)} days"
    elif category == CATEGORY_CHARACTER_STREAK:
        return f"{int(score)} days"
    else:
        return f"{int(score):,}"


def _get_medal_emoji(rank: int) -> str:
    """Get medal emoji for top 3."""
    if rank == 0:
        return "🥇"
    elif rank == 1:
        return "🥈"
    elif rank == 2:
        return "🥉"
    return ""


class LeaderboardView(discord.ui.View):
    """Pagination view for leaderboards."""

    def __init__(
        self,
        *,
        category: str,
        guild_id: int,
        period: str,
        limit: int = 10,
        current_page: int = 0,
        bot: commands.Bot,
    ):
        super().__init__(timeout=300.0)
        self.category = category
        self.guild_id = guild_id
        self.period = period
        self.limit = limit
        self.current_page = current_page
        self.bot = bot

    async def _get_page(self, page: int) -> discord.Embed:
        """Generate embed for a specific page."""
        offset = page * self.limit
        rankings = await get_leaderboard(
            category=self.category,
            guild_id=self.guild_id,
            period=self.period,
            limit=self.limit,
            offset=offset,
        )

        category_name = CATEGORY_NAMES.get(self.category, self.category.title())
        period_name = PERIOD_NAMES.get(self.period, self.period.title())
        scope = "Global" if self.guild_id == GLOBAL_GUILD_ID else "Server"

        embed = discord.Embed(
            title=f"🏆 {category_name} Leaderboard",
            description=f"**{scope}** • **{period_name}**",
            color=0x5865F2,
        )

        if not rankings:
            tip = "Try **Global** scope, or roll with /character and use /points daily to appear here."
            embed.description = f"{embed.description}\n\n*No rankings available yet.*\n\n{tip}"
            return embed

        lines = []
        for idx, (gid, uid, score) in enumerate(rankings):
            rank = offset + idx
            medal = _get_medal_emoji(rank)
            rank_display = f"{medal} **#{rank + 1}**" if medal else f"**#{rank + 1}**"

            try:
                user = await self.bot.fetch_user(uid)
                username = user.display_name or user.name
                user_mention = f"{username} (`{uid}`)"
            except Exception:
                user_mention = f"*Unknown User* (`{uid}`)"

            score_str = _format_score(self.category, score)
            lines.append(f"{rank_display} {user_mention} — **{score_str}**")

        embed.description = f"{embed.description}\n\n" + "\n".join(lines)

        # Add footer with pagination info
        total_shown = len(rankings)
        if total_shown == self.limit:
            embed.set_footer(text=f"Page {page + 1} • Showing {offset + 1}-{offset + total_shown}")
        else:
            embed.set_footer(text=f"Page {page + 1} • Showing {offset + 1}-{offset + total_shown} (end)")

        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            embed = await self._get_page(self.current_page)
            self.prev_page.disabled = self.current_page == 0
            # Check if there's a next page
            offset = (self.current_page + 1) * self.limit
            next_rankings = await get_leaderboard(
                category=self.category,
                guild_id=self.guild_id,
                period=self.period,
                limit=1,
                offset=offset,
            )
            self.next_page.disabled = len(next_rankings) == 0
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        offset = (self.current_page + 1) * self.limit
        next_rankings = await get_leaderboard(
            category=self.category,
            guild_id=self.guild_id,
            period=self.period,
            limit=self.limit,
            offset=offset,
        )
        if next_rankings:
            self.current_page += 1
            embed = await self._get_page(self.current_page)
            self.prev_page.disabled = False
            # Check if there's another page after this
            next_offset = (self.current_page + 1) * self.limit
            more_rankings = await get_leaderboard(
                category=self.category,
                guild_id=self.guild_id,
                period=self.period,
                limit=1,
                offset=next_offset,
            )
            self.next_page.disabled = len(more_rankings) == 0
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()


class SlashLeaderboard(commands.Cog):
    """Leaderboard commands for server and global rankings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    leaderboard = app_commands.Group(name="leaderboard", description="View server and global leaderboards")

    @leaderboard.command(name="view", description="View leaderboard rankings")
    @app_commands.describe(
        category="Leaderboard category",
        period="Time period (default: alltime)",
        scope="Server or global (default: global)",
        limit="Results per page (default: 10, max: 25)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Points", value=CATEGORY_POINTS),
            app_commands.Choice(name="Rolls", value=CATEGORY_ROLLS),
            app_commands.Choice(name="Talk Calls", value=CATEGORY_TALK),
            app_commands.Choice(name="Bond XP", value=CATEGORY_BOND),
            app_commands.Choice(name="Characters Owned", value=CATEGORY_CHARACTERS),
            app_commands.Choice(name="Daily Streak", value=CATEGORY_STREAK),
            app_commands.Choice(name="Days Active", value=CATEGORY_ACTIVITY),
            app_commands.Choice(name="Character Streak", value=CATEGORY_CHARACTER_STREAK),
        ],
        period=[
            app_commands.Choice(name="All Time", value=PERIOD_ALLTIME),
            app_commands.Choice(name="Daily", value=PERIOD_DAILY),
            app_commands.Choice(name="Weekly", value=PERIOD_WEEKLY),
            app_commands.Choice(name="Monthly", value=PERIOD_MONTHLY),
        ],
        scope=[
            app_commands.Choice(name="Server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ],
    )
    async def leaderboard_view(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        period: app_commands.Choice[str] | None = None,
        scope: app_commands.Choice[str] | None = None,
        limit: int = 10,
    ):
        if not interaction.guild:
            await safe_ephemeral_send(interaction, "Use this command in a server.")
            return

        # Determine guild_id from scope (default: global).
        scope_value = scope.value if scope else "global"
        guild_id = GLOBAL_GUILD_ID if scope_value == "global" else int(interaction.guild.id)

        # Validate limit
        limit = max(1, min(25, int(limit or 10)))

        # Get period
        period_value = period.value if period else PERIOD_ALLTIME

        # Get first page
        view = LeaderboardView(
            category=category.value,
            guild_id=guild_id,
            period=period_value,
            limit=limit,
            current_page=0,
            bot=self.bot,
        )

        # Check if there's a next page
        next_rankings = await get_leaderboard(
            category=category.value,
            guild_id=guild_id,
            period=period_value,
            limit=1,
            offset=limit,
        )
        view.next_page.disabled = len(next_rankings) == 0

        embed = await view._get_page(0)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @leaderboard.command(name="rank", description="Check your rank on a leaderboard")
    @app_commands.describe(
        category="Leaderboard category",
        period="Time period (default: alltime)",
        scope="Server or global (default: global)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Points", value=CATEGORY_POINTS),
            app_commands.Choice(name="Rolls", value=CATEGORY_ROLLS),
            app_commands.Choice(name="Talk Calls", value=CATEGORY_TALK),
            app_commands.Choice(name="Bond XP", value=CATEGORY_BOND),
            app_commands.Choice(name="Characters Owned", value=CATEGORY_CHARACTERS),
            app_commands.Choice(name="Daily Streak", value=CATEGORY_STREAK),
            app_commands.Choice(name="Days Active", value=CATEGORY_ACTIVITY),
            app_commands.Choice(name="Character Streak", value=CATEGORY_CHARACTER_STREAK),
        ],
        period=[
            app_commands.Choice(name="All Time", value=PERIOD_ALLTIME),
            app_commands.Choice(name="Daily", value=PERIOD_DAILY),
            app_commands.Choice(name="Weekly", value=PERIOD_WEEKLY),
            app_commands.Choice(name="Monthly", value=PERIOD_MONTHLY),
        ],
        scope=[
            app_commands.Choice(name="Server", value="server"),
            app_commands.Choice(name="Global", value="global"),
        ],
    )
    async def leaderboard_rank(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        period: app_commands.Choice[str] | None = None,
        scope: app_commands.Choice[str] | None = None,
    ):
        if not interaction.guild:
            await safe_ephemeral_send(interaction, "Use this command in a server.")
            return

        # Determine guild_id from scope (default: global).
        scope_value = scope.value if scope else "global"
        guild_id = GLOBAL_GUILD_ID if scope_value == "global" else int(interaction.guild.id)

        # Get period
        period_value = period.value if period else PERIOD_ALLTIME

        # Get user's rank
        rank_data = await get_user_rank(
            category=category.value,
            guild_id=guild_id,
            user_id=int(interaction.user.id),
            period=period_value,
        )

        category_name = CATEGORY_NAMES.get(category.value, category.value.title())
        period_name = PERIOD_NAMES.get(period_value, period_value.title())
        scope_name = "Global" if guild_id == GLOBAL_GUILD_ID else "Server"

        if rank_data is None:
            opted_out = await is_opted_out(int(interaction.user.id))
            if opted_out:
                msg = (
                    f"**{category_name}** • **{scope_name}** • **{period_name}**\n\n"
                    "You've **opted out** of leaderboards, so your data is hidden and new activity isn't counted. "
                    "Use **/leaderboard opt_in** to appear on leaderboards again."
                )
            else:
                msg = (
                    f"**{category_name}** • **{scope_name}** • **{period_name}**\n\n"
                    "You're not ranked yet. Start using the bot (e.g. **/points daily**, rolls, **/talk**) to appear on the leaderboard!"
                )
            embed = discord.Embed(title="📊 Your Rank", description=msg, color=0x5865F2)
            await safe_send_embed(interaction, embed, ephemeral=True)
            return

        rank, score = rank_data
        medal = _get_medal_emoji(rank)
        rank_display = f"{medal} **#{rank + 1}**" if medal else f"**#{rank + 1}**"
        score_str = _format_score(category.value, score)

        # Get top 3 for context
        top_3 = await get_leaderboard(
            category=category.value,
            guild_id=guild_id,
            period=period_value,
            limit=3,
            offset=0,
        )

        embed = discord.Embed(
            title="📊 Your Rank",
            description=f"**{category_name}** • **{scope_name}** • **{period_name}**\n\n"
            f"Your rank: {rank_display}\n"
            f"Your score: **{score_str}**",
            color=0x5865F2,
        )

        # Add top 3 context
        if top_3:
            top_lines = []
            for idx, (gid, uid, top_score) in enumerate(top_3):
                top_medal = _get_medal_emoji(idx)
                try:
                    user = await self.bot.fetch_user(uid)
                    username = user.display_name or user.name
                except Exception:
                    username = f"User {uid}"
                top_score_str = _format_score(category.value, top_score)
                top_lines.append(f"{top_medal} **#{idx + 1}** {username} — {top_score_str}")
            embed.add_field(name="Top 3", value="\n".join(top_lines), inline=False)

        await safe_send_embed(interaction, embed, ephemeral=True)

    @leaderboard.command(name="opt_out", description="Opt out of leaderboards (your data will be hidden)")
    async def leaderboard_opt_out(self, interaction: discord.Interaction):
        from utils.leaderboard import set_opt_out

        success = await set_opt_out(int(interaction.user.id), opt_out=True)
        if success:
            await safe_ephemeral_send(
                interaction,
                "✅ You've opted out of leaderboards. Your data will no longer appear on public leaderboards.",
            )
        else:
            await safe_ephemeral_send(interaction, "⚠️ Failed to update your preference. Please try again later.")

    @leaderboard.command(name="opt_in", description="Opt back into leaderboards")
    async def leaderboard_opt_in(self, interaction: discord.Interaction):
        from utils.leaderboard import set_opt_out

        success = await set_opt_out(int(interaction.user.id), opt_out=False)
        if success:
            await safe_ephemeral_send(
                interaction,
                "✅ You've opted back into leaderboards. Your data will now appear on public leaderboards.",
            )
        else:
            await safe_ephemeral_send(interaction, "⚠️ Failed to update your preference. Please try again later.")


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashLeaderboard(bot))
