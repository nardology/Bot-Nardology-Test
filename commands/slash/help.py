import discord
from discord.ext import commands
from discord import app_commands

import config
from utils.text import format_help_header, unknown_command

TOPICS = [
    "ping",
    "hello",
    "say",
    "add",
    "talk",
    "character",
    "limits",
    "usage",
    "settings",
    "settings.ai",
    "settings.announce",
    "points",
    "premium",
    "privacy",
    "leaderboard",
    "bond",
    "packs",
    "report",
    "cosmetic",
    "inspect",
    "start",
    "tutorial",
]


def get_help_text(topic: str | None = None) -> str:
    if topic is None:
        premium_section = (
            "**Premium**\n"
            "⭐ `/premium subscribe` — Get Pro for $4.99/mo\n"
            "🎁 `/premium gift` — Gift Pro to a friend\n\n"
        ) if config.PAYMENTS_ENABLED else (
            "**Premium**\n"
            "⭐ `/premium status` — Check your tier\n\n"
        )

        return (
            format_help_header()
            + "\n"
            "Type `/` and choose a command, or pick a topic below for details.\n\n"
            "**Getting Started**\n"
            "🐱 `/start` — Meet KAI and get introduced to the bot\n"
            "📖 `/tutorial` — Interactive setup guide for server admins\n"
            "🔍 `/inspect` — View your profile or another member's\n\n"
            "**Basic**\n"
            "🏓 `/ping` — Check if I'm alive\n"
            "👋 `/hello` — Greet the bot\n"
            "🗣️ `/say` — Repeat a message\n"
            "➕ `/add` — Add two numbers\n\n"
            "**Roleplay / AI**\n"
            "🎭 `/talk` — Talk to a character\n"
            "🎲 `/character roll` — Roll for a new character\n"
            "📜 `/character collection` — View your characters\n"
            "✅ `/character select` — Select your active character\n"
            "💕 `/bond view` — Check your bond with a character\n\n"
            "**Points & Economy**\n"
            "🪙 `/points daily` — Claim daily reward\n"
            "🛒 `/points shop` — Browse the points shop\n"
            "🎯 `/points quests` — View and claim quest rewards\n"
            "💰 `/points balance` — Check your balance\n\n"
            "**Leaderboard**\n"
            "🏆 `/leaderboard view` — Server and global rankings\n\n"
            + premium_section +
            "**Settings (Admin)**\n"
            "🛠️ `/settings show` — View current server settings\n"
            "🛠️ `/settings language` — Set bot language\n"
            "🛠️ `/settings character` — Set server default character\n"
            "🛠️ `/settings ai ...` — AI access controls\n"
            "🛠️ `/settings announce ...` — Announcement channel\n\n"
            "**Privacy**\n"
            "🔒 `/privacy export` — Download your data\n"
            "🗑️ `/privacy delete` — Delete your account\n\n"
            "ℹ️ Try `/help topic:points`, `/help topic:settings.ai`, `/help topic:premium`, and more.\n\n"
            f"📜 [Terms of Service]({config.TERMS_OF_SERVICE_URL}) · "
            f"[Privacy Policy]({config.PRIVACY_POLICY_URL}) · "
            f"[Support Server]({config.SUPPORT_SERVER_URL})"
        )

    t = topic.lower().strip()

    if t == "ping":
        return "🏓 `/ping` — Checks if I'm alive."
    if t == "hello":
        return "👋 `/hello` — Greets you."
    if t == "say":
        return "🗣️ `/say message:<text>` — Repeats your message."
    if t == "add":
        return "➕ `/add a:<int> b:<int>` — Adds two numbers."
    if t == "start":
        return "🐱 `/start` — Meet KAI, the bot mascot, and get a quick introduction."
    if t == "tutorial":
        return (
            "📖 **/tutorial**\n"
            "Interactive setup guide for server admins. Walks you through AI settings, "
            "characters, points, moderation, and more with step-by-step pages."
        )

    if t == "talk":
        return (
            "🎭 **/talk**\n"
            "• `/talk prompt:<text>` — Talk to a character (subject to server rules + rate limits)\n"
            "• Optional: `public:true` (Pro-only) posts the reply publicly\n"
            "• Optional: `character:<id>` lets you pick one you own (otherwise uses server default)\n"
            "• Pro users get conversation memory across messages"
        )
    if t in {"character", "characters"}:
        return (
            "🎲 **Characters**\n"
            "• `/character roll` — Roll for a random character\n"
            "• `/character collection` — View your collection + selected character\n"
            "• `/character select` — Select your active character\n"
            "• `/character unselect` — Clear selected character\n"
            "• `/character remove` — Remove a custom character you own"
        )
    if t == "limits":
        return "📉 `/limits view` — Shows current rate limits and remaining daily usage."
    if t == "usage":
        return "📈 `/usage view days:<1-30>` — (Owner/Admin) shows server usage stats and outcomes."
    if t == "inspect":
        return (
            "🔍 **/inspect**\n"
            "• `/inspect` — View your own profile (characters, bond, stats, cosmetics)\n"
            "• `/inspect member:<user>` — View another member's profile"
        )

    if t in {"settings", "config"}:
        return (
            "🛠️ **Settings** (Admin)\n"
            "• `/settings show` — View current server settings\n"
            "• `/settings language` — Set bot language\n"
            "• `/settings character` — Set default server character\n"
            "• `/settings announce channel` — Set announcement channel\n"
            "• `/settings announce clear_channel` — Remove announcement channel\n"
            "• `/settings announce show` — Show current announcement settings\n"
            "• `/settings say limits` — View say command limits\n\n"
            "See also: `/help topic:settings.ai`"
        )
    if t in {"settings.ai", "ai"}:
        return (
            "🤖 **AI Settings** (Admin)\n"
            "• `/settings ai allow-role` / `block-role` — Allow or block roles from using AI\n"
            "• `/settings ai unallow-role` / `unblock-role` — Undo role permissions\n"
            "• `/settings ai allow-channel` / `unallow-channel` — Restrict AI to specific channels\n"
            "• `/settings ai list-channels` — View allowed/blocked channels\n"
            "• `/settings ai safety-mode` — Toggle safety filter\n"
            "• `/settings ai block-topic` / `unblock-topic` — Block or unblock topics\n"
            "• `/settings ai list-topics` — View blocked topics\n"
            "• `/settings ai limits` — View current AI rate limits"
        )
    if t in {"settings.announce", "announce"}:
        return (
            "📢 **Announcement Settings** (Admin)\n"
            "• `/settings announce channel` — Set the announcement channel\n"
            "• `/settings announce clear_channel` — Remove the announcement channel\n"
            "• `/settings announce show` — View current announcement settings"
        )

    if t == "points":
        return (
            "🪙 **Points & Economy**\n"
            "• `/points daily` — Claim your daily reward (streaks give bonuses!)\n"
            "• `/points balance` — Check your point balance\n"
            "• `/points shop` — Browse and buy items with points\n"
            "• `/points cosmetic-shop` — Browse cosmetic items\n"
            "• `/points quests` — View and claim quest rewards (daily/weekly/monthly)\n"
            "• `/points buy` — Quick-buy a shop item\n"
            "• `/points convert` — Convert between currencies\n"
            "• `/points luck` — Check your current luck modifier\n"
            "• `/points reminders` — Toggle daily streak reminders on/off"
        )
    if t == "premium":
        if config.PAYMENTS_ENABLED:
            return (
                "⭐ **Premium (Pro)**\n"
                "• `/premium subscribe` — Subscribe to Pro ($4.99/month)\n"
                "• `/premium status` — Check your subscription status\n"
                "• `/premium cancel` — Cancel your subscription\n"
                "• `/premium gift` — Gift Pro to another user\n"
                "• `/premium buy_points` — Purchase points with real money\n\n"
                "**Pro perks:** 2x rolls, 20 inventory slots, AI memory, longer responses, "
                "custom packs, character streak DMs, and more!"
            )
        return (
            "⭐ **Premium (Pro)**\n"
            "• `/premium status` — Check your current tier\n\n"
            "**Pro perks:** 2x rolls, 20 inventory slots, AI memory, longer responses, "
            "custom packs, character streak DMs, and more!\n\n"
            "Subscriptions are coming soon — stay tuned!"
        )
    if t == "privacy":
        return (
            "🔒 **Privacy**\n"
            "• `/privacy export` — Download all your data as a JSON file (once per 24h)\n"
            "• `/privacy delete` — Permanently delete your account (requires confirmation)"
        )
    if t == "leaderboard":
        return (
            "🏆 **Leaderboard**\n"
            "• `/leaderboard view` — View rankings (points, rolls, talk, bond XP, etc.)\n"
            "• `/leaderboard rank` — Check your rank in a category\n"
            "• `/leaderboard opt_out` — Hide yourself from leaderboards\n"
            "• `/leaderboard opt_in` — Re-appear on leaderboards"
        )
    if t == "bond":
        return (
            "💕 **Bond**\n"
            "• `/bond view` — Check your bond level and XP with a character\n"
            "• `/bond nickname` — Set a nickname for a character\n\n"
            "Bond XP is earned by talking to characters. Higher bond levels unlock "
            "special images and secret lore!"
        )
    if t == "packs":
        return (
            "📦 **Character Packs**\n"
            "• `/packs marketplace` — Browse community packs\n"
            "• `/packs browse` — Preview a pack's characters\n"
            "• `/packs enable` / `disable` — Enable or disable a pack on your server\n"
            "• `/packs enabled` — List packs active on this server\n"
            "• `/packs upvote` — Upvote a pack\n"
            "• `/packs leaderboard` — Top-rated packs\n\n"
            "**Pack Creators (Pro):**\n"
            "• `/packs create` — Create a new character pack\n"
            "• `/packs character_add` — Add a character to your pack\n"
            "• `/packs edit` / `delete` — Manage your packs"
        )
    if t == "report":
        return (
            "🚨 **Reporting**\n"
            "• `/report send` — Report a user or content\n"
            "• `/report status` — Check report status\n"
            "• `/report global` — Report a critical issue to bot owners\n\n"
            "**Admin:**\n"
            "• `/report channel-set` — Set the report channel\n"
            "• `/report list` / `view` — Review reports\n"
            "• `/report status_update` — Update report status\n"
            "• `/report analytics` — Report statistics"
        )
    if t == "cosmetic":
        return (
            "🎨 **Cosmetics**\n"
            "• `/cosmetic select` — Equip a cosmetic item\n"
            "• `/cosmetic clear` — Unequip your cosmetic\n\n"
            "Buy cosmetics from `/points cosmetic-shop` (500 points each)."
        )

    return unknown_command()


class SlashHelp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show bot commands and usage")
    @app_commands.describe(topic="Optional: a command/topic like points, talk, character, settings.ai...")
    async def help(self, interaction: discord.Interaction, topic: str | None = None):
        await interaction.response.send_message(get_help_text(topic), ephemeral=True)

    @help.autocomplete("topic")
    async def help_topic_autocomplete(self, interaction: discord.Interaction, current: str):
        current = (current or "").lower()
        matches = [o for o in TOPICS if current in o]
        return [app_commands.Choice(name=o, value=o) for o in matches[:25]]


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashHelp(bot))
