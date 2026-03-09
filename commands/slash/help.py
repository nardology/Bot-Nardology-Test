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
    "scene",
    "voice",
    "bond",
    "limits",
    "usage",
    "settings",
    "settings.ai",
    "settings.announce",
    "points",
    "packs",
    "premium",
    "leaderboard",
    "cosmetic",
    "inspect",
    "start",
    "tutorial",
    "recommend",
    "feedback",
    "report",
    "appeal",
    "penalty",
    "privacy",
    "legal",
]


def get_help_text(topic: str | None = None) -> str:
    if topic is None:
        premium_section = (
            "**Premium (Pro — $4.99/mo)**\n"
            "⭐ `/premium subscribe` — Subscribe to Pro\n"
            "🎁 `/premium gift` — Gift Pro to a friend\n"
            "💎 `/premium buy_points` — Purchase points\n"
            "📊 `/premium status` — Check your tier\n\n"
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
            "🎬 `/scene start` — Start a turn-based RP scene with another user\n"
            "🎲 `/character roll` — Roll for a new character\n"
            "📜 `/character collection` — View your characters\n"
            "✅ `/character select` — Select your active character\n"
            "💕 `/bond view` — Check your bond with a character\n\n"
            "**Voice**\n"
            "🔊 `/voice play` — Play a sound in voice chat\n"
            "📋 `/voice list` — List available sounds\n\n"
            "**Points & Economy**\n"
            "🪙 `/points daily` — Claim daily reward\n"
            "🛒 `/points shop` — Browse the points shop\n"
            "🎨 `/points cosmetic-shop` — Buy profile cosmetics\n"
            "🎯 `/points quests` — View and claim quest rewards\n"
            "💰 `/points balance` — Check your balance\n"
            "🍀 `/points luck` — Check your luck modifier\n\n"
            "**Leaderboard**\n"
            "🏆 `/leaderboard view` — Server and global rankings\n\n"
            "**Custom Packs**\n"
            "📦 `/packs marketplace` — Discover community packs\n"
            "🔎 `/packs browse` — Preview a pack's characters\n"
            "✅ `/packs enable` / `disable` — Toggle packs on your server\n\n"
            "**Community**\n"
            "💡 `/recommend` — Suggest a new official character\n"
            "📝 `/feedback` — Send feedback to the developers\n"
            "🚨 `/report global` — Report a critical issue to bot owners\n"
            "📣 `/appeal` — Appeal a server ban/nuke\n\n"
            + premium_section +
            "**Settings (Admin)**\n"
            "🛠️ `/settings show` — View current server settings\n"
            "🛠️ `/settings character` — Set server default character\n"
            "🛠️ `/settings ai ...` — AI access controls\n"
            "🛠️ `/penalty view` / `reset` — View or reset user penalties\n\n"
            "**Privacy & Legal**\n"
            "🔒 `/privacy export` — Download your data\n"
            "🗑️ `/privacy delete` — Delete your account\n"
            "📜 `/legal` — Terms of Service & Privacy Policy\n\n"
            "ℹ️ Use `/help topic:<name>` for details on any section — "
            "e.g. `scene`, `voice`, `packs`, `premium`, `settings.ai`\n\n"
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
    if t in {"scene", "scenes"}:
        return (
            "🎬 **Scenes** — Turn-based roleplay with another user\n"
            "• `/scene start` — Start a scene with another user\n"
            "• `/scene say` — Take your turn (AI generates your character's response)\n"
            "• `/scene narrate` — Add narration (no AI, doesn't change turn)\n"
            "• `/scene view` — View recent lines of a scene\n"
            "• `/scene summary` — Get a short summary so far\n"
            "• `/scene list` — List active scenes in this channel\n"
            "• `/scene end` — End a scene\n"
            "• `/scene forget` — Delete scene memory"
        )
    if t in {"voice", "sounds"}:
        return (
            "🔊 **Voice**\n"
            "• `/voice play` — Join your voice channel and play a sound\n"
            "• `/voice list` — List available sounds\n"
            "• `/voice stop` — Stop playback and leave\n"
            "• `/voice add` — (Pro) Upload a .wav sound for this server\n"
            "• `/voice remove` — (Pro) Remove an uploaded server sound"
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
            "• `/settings ai limits` — Set /talk AI rate limits (admin)"
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
            "• `/points convert` — Convert between shards and points (50:1)\n"
            "• `/points luck` — Check your current luck modifier\n"
            "• `/points reminders` — Toggle daily streak reminders on/off"
        )
    if t == "premium":
        if config.PAYMENTS_ENABLED:
            return (
                "⭐ **Premium (Pro) — $4.99/month**\n\n"
                "**Commands:**\n"
                "• `/premium subscribe` — Subscribe to Pro\n"
                "• `/premium status` — Check your subscription status\n"
                "• `/premium cancel` — Cancel your subscription\n"
                "• `/premium gift` — Gift Pro to another user\n"
                "• `/premium buy_points` — Purchase points with real money\n\n"
                "**Pro Perks:**\n"
                "🎲 2x character rolls per day\n"
                "📦 20 inventory slots (vs 10 free)\n"
                "🧠 AI conversation memory across messages\n"
                "📝 Longer AI responses\n"
                "🗣️ Public `/talk` replies\n"
                "📦 Create and publish custom character packs\n"
                "🔊 Upload custom voice sounds\n"
                "💌 Character streak DM reminders\n"
                "🎨 Access to exclusive cosmetics\n"
                "⚡ Higher rate limits across all commands"
            )
        return (
            "⭐ **Premium (Pro)**\n"
            "• `/premium status` — Check your current tier\n\n"
            "**Pro Perks:**\n"
            "🎲 2x character rolls per day\n"
            "📦 20 inventory slots (vs 10 free)\n"
            "🧠 AI conversation memory across messages\n"
            "📝 Longer AI responses\n"
            "🗣️ Public `/talk` replies\n"
            "📦 Create and publish custom character packs\n"
            "🔊 Upload custom voice sounds\n"
            "💌 Character streak DM reminders\n"
            "🎨 Access to exclusive cosmetics\n"
            "⚡ Higher rate limits across all commands\n\n"
            "Subscriptions are coming soon — stay tuned!"
        )
    if t == "privacy":
        return (
            "🔒 **Privacy**\n"
            "• `/privacy export` — Download all your data as a JSON file (once per 24h)\n"
            "• `/privacy delete` — Permanently delete your account (requires confirmation)"
        )
    if t == "legal":
        return (
            "📜 **Legal**\n"
            f"• `/legal` — View Terms of Service, Privacy Policy, and contact info\n"
            f"• [Terms of Service]({config.TERMS_OF_SERVICE_URL})\n"
            f"• [Privacy Policy]({config.PRIVACY_POLICY_URL})\n"
            f"• [Support Server]({config.SUPPORT_SERVER_URL})"
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
            "• `/packs private_enable` — Enable a private pack (requires password)\n"
            "• `/packs enabled` — List packs active on this server\n"
            "• `/packs upvote` — Upvote a pack\n"
            "• `/packs leaderboard` — Top-rated packs\n\n"
            "**Pack Creators (Pro):**\n"
            "• `/packs create` — Create a new character pack\n"
            "• `/packs character_add` — Add a character to your pack\n"
            "• `/packs character_edit` — Edit a character in your pack\n"
            "• `/packs character_remove` — Remove a character from your pack\n"
            "• `/packs edit` / `delete` — Manage your packs\n\n"
            "**Server-Only Characters (Admin):**\n"
            "• `/packs server_characters` — List server-only characters\n"
            "• `/packs server_character_edit` — Edit a server character\n"
            "• `/packs server_character_remove` — Remove a server character"
        )
    if t == "recommend":
        return (
            "💡 **Recommend a Character**\n"
            "• `/recommend` — Opens a form to suggest a new official character\n\n"
            "Fill out the character's name, rarity, backstory, personality, and more. "
            "You'll be notified via DM when your recommendation is reviewed by the developers. "
            "You can edit your pending recommendation by running `/recommend` again."
        )
    if t == "feedback":
        return (
            "📝 **Feedback**\n"
            "• `/feedback message:<text>` — Send feedback directly to the developers\n"
            "• Optional: attach a screenshot with `attachment:<file>`\n\n"
            "Free users: 3 per day · Pro users: 15 per day"
        )
    if t in {"appeal", "appeals"}:
        return (
            "📣 **Appeals**\n"
            "• `/appeal` — Appeal a server ban or nuke (guild owners only, 1 per day)\n"
            "• `/verification_appeal` — Appeal a denied pack/character verification (1 per day)\n\n"
            "Appeals are sent directly to bot owners for review."
        )
    if t == "report":
        return (
            "🚨 **Reporting**\n"
            "• `/report global` — Report a critical issue to bot owners (3/day)\n"
            "• `/report content` — Report inappropriate pack/character content (5/day)\n"
            "• `/report send` — Report a user or content to server admins\n"
            "• `/report status` — Quick bot status check\n\n"
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
            "Buy cosmetics from `/points cosmetic-shop` (500 points each). "
            "Your equipped cosmetic is shown on your `/inspect` profile."
        )
    if t in {"penalty", "penalties"}:
        return (
            "⚠️ **Penalties** (Admin)\n"
            "• `/penalty view member:<user>` — View a user's active penalty status\n"
            "• `/penalty reset member:<user>` — Reset a user's spam penalties"
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
