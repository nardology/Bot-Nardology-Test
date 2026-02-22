# bot.py
import os
import sys
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

print("[boot] bot.py loading…", flush=True)

import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import config

print(f"[boot] config OK  env={getattr(config, 'ENVIRONMENT', '?')}", flush=True)
from utils.audit import audit_log
from utils.backpressure import get_redis_or_none
from utils.db import init_db
from utils.analytics_flush import start_analytics_flush_loop
from core.incident_monitor import start_incident_monitor
from utils.verification_auto_approve import start_verification_auto_approve_loop
from utils.owner import is_bot_owner


async def ensure_redis_best_effort() -> bool:
    """Best-effort Redis readiness.

    Phase 1 requirement: the bot should *degrade* if Redis is down/misconfigured,
    not crash-loop.

    Returns True if Redis appears reachable, else False.
    """
    r = await get_redis_or_none()
    if r is None:
        return False
    # A few retries for Railway cold starts (service boot order)
    for _ in range(1, 6):
        try:
            await r.ping()
            return True
        except Exception:
            await asyncio.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv()

# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

intents = discord.Intents.default()

# (You can ignore the "message content intent missing" warning since you're slash-only.)
# If you ever need it later:
# intents.message_content = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)


def _make_json_formatter() -> logging.Formatter:
    """Build a JSON formatter for structured file logs. Falls back to plain text."""
    try:
        from pythonjsonlogger.json import JsonFormatter
        return JsonFormatter(
            "{asctime}{levelname}{name}{message}",
            style="{",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    except ImportError:
        return logging.Formatter(LOG_FORMAT)


def setup_logging() -> None:
    """Configure logging once (safe for reloads)."""
    for handler in root_logger.handlers:
        if getattr(handler, "_botnardology_handler", False):
            return

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console._botnardology_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(console)

    json_fmt = _make_json_formatter()

    file = RotatingFileHandler(
        LOG_DIR / "bot.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file.setFormatter(json_fmt)
    file._botnardology_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(file)

    errors = RotatingFileHandler(
        LOG_DIR / "errors.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    errors.setLevel(logging.ERROR)
    errors.setFormatter(json_fmt)
    errors._botnardology_handler = True  # type: ignore[attr-defined]
    root_logger.addHandler(errors)


setup_logging()
logger = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


# NOTE: For large-scale deployment, sharding is required.
# AutoShardedBot handles shard management automatically.
class SlashOnlyBot(commands.AutoShardedBot):
    async def setup_hook(self) -> None:
        # Redis preflight (best-effort): do NOT crash-loop if Redis is down.
        redis_ok = await ensure_redis_best_effort()
        if not redis_ok:
            logger.warning("Redis unavailable at startup; running in degraded mode.")

        # DB preflight for durable data.
        await init_db()

        # Health-check + Stripe webhook server (must succeed so Railway sees the container as alive).
        try:
            print("[boot] starting health/webhook server…", flush=True)
            from core.stripe_webhook import start_webhook_server
            await start_webhook_server(self)
            print("[boot] health/webhook server UP", flush=True)
        except Exception:
            logger.exception("Failed to start webhook/health server — Railway may stay stuck on 'Creating containers'")

        await load_extensions()

        # Global interaction gate (bans / bot-disable / owner-banned guilds).
        # Must never crash: on any error, allow the command rather than breaking the bot.
        async def _interaction_gate(interaction: discord.Interaction) -> bool:
            try:
                # Only gate slash commands (skip component/autocomplete interactions).
                if getattr(interaction, "type", None) != discord.InteractionType.application_command:
                    return True

                # Allow appeals even when banned/disabled (so users can reach you).
                try:
                    data = getattr(interaction, "data", None) or {}
                    cmd_name = str(data.get("name") or "")
                    if cmd_name == "appeal":
                        return True
                except Exception:
                    pass

                uid = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
                if uid and is_bot_owner(uid):
                    return True  # owner bypass

                from utils.mod_actions import (
                    is_bot_disabled,
                    get_bot_disabled_meta,
                    is_user_banned,
                    get_user_ban_reason,
                )

                # 1) User ban
                if uid and await is_user_banned(uid):
                    reason = await get_user_ban_reason(uid)
                    msg = "⛔ You are banned from using this bot."
                    if reason:
                        msg += f" Reason: `{reason}`"
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(msg[:1900], ephemeral=True)
                    except Exception:
                        pass
                    return False

                # 2) Global bot disable
                if await is_bot_disabled():
                    t, reason, by = await get_bot_disabled_meta()
                    msg = "⛔ The bot is temporarily disabled by the administrator."
                    if reason:
                        msg += f"\nReason: `{reason}`"
                    if t:
                        msg += f"\nAt: `<t:{int(t)}:R>`"
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(msg[:1900], ephemeral=True)
                    except Exception:
                        pass
                    return False

                # 3) If the *guild owner* is banned, disable the bot in that guild.
                g = getattr(interaction, "guild", None)
                owner_id = int(getattr(g, "owner_id", 0) or 0) if g is not None else 0
                if owner_id and await is_user_banned(owner_id):
                    msg = "⛔ This server is disabled because the server owner is banned."
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(msg[:1900], ephemeral=True)
                    except Exception:
                        pass
                    return False

                return True
            except Exception:
                return True

        try:
            self.tree.interaction_check = _interaction_gate  # type: ignore[assignment]
        except Exception:
            logger.exception("Failed installing global interaction gate")

        # Background: periodically flush product analytics (Redis -> Postgres)
        # Best-effort: if Redis is down, the loop should safely no-op.
        try:
            start_analytics_flush_loop()
        except Exception:
            logger.exception("Failed to start analytics flush loop")

        # Background: incident monitor (DM owners on anomalies / AI shutdown)
        try:
            await start_incident_monitor(self)
        except Exception:
            logger.exception("Failed to start incident monitor")

        # Background: verification auto-approve (5-day auto-approve for pending tickets)
        try:
            start_verification_auto_approve_loop()
        except Exception:
            logger.exception("Failed to start verification auto-approve loop")

        # Background: streak reminder DMs (daily reminder + 90-min-before-midnight warning)
        try:
            from utils.streak_reminder_loop import start_streak_reminder_loop
            start_streak_reminder_loop(self)
        except Exception:
            logger.exception("Failed to start streak reminder loop")

        # Background: weekly analytics DM to bot owners (Monday 10:00 UTC)
        try:
            from utils.weekly_analytics_loop import start_weekly_analytics_loop
            start_weekly_analytics_loop(self)
        except Exception:
            logger.exception("Failed to start weekly analytics loop")

        # Background: leaderboard period resets (daily/weekly/monthly)
        try:
            from utils.leaderboard_reset_loop import start_leaderboard_reset_loop
            start_leaderboard_reset_loop()
        except Exception:
            logger.exception("Failed to start leaderboard reset loop")

        # Sync JSON-defined shop items into Redis (JSON is the source of truth).
        try:
            from utils.shop_store import sync_shop_items_from_registry
            synced = await sync_shop_items_from_registry()
            if synced:
                logger.info("Shop sync: %d item(s) pushed to Redis from JSON.", synced)
        except Exception:
            logger.exception("Shop sync failed (non-fatal)")

        # Sync slash commands (dev guild or global) after extensions are loaded.
        try:
            await sync_commands()
        except Exception:
            logger.exception("sync_commands() failed")

    async def on_message(self, message: discord.Message):
        return  # ignore message-based commands entirely


def _get_env_int(name: str) -> int | None:
    try:
        v = int(str(os.getenv(name, "")).strip())
        return v if v > 0 else None
    except Exception:
        return None


_shard_count = _get_env_int("SHARD_COUNT")

bot = SlashOnlyBot(
    command_prefix="__NO_PREFIX__",
    intents=intents,
    shard_count=_shard_count,
)

# Used for /owner status uptime.
bot.start_time = time.time()  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

EXTENSIONS = [
    "commands.slash.analytics",
    "commands.slash.basic",
    "commands.slash.start",
    "commands.slash.settings",
    "commands.slash.help",
    "commands.slash.talk",
    "commands.slash.voice",
    "commands.slash.feedback",
    "commands.slash.limits",
    "commands.slash.usage",
    "commands.slash.bond",
    "commands.slash.penalty",
    "commands.slash.character",
    "commands.slash.points",
    "commands.slash.packs",
    # "commands.slash.scene",  # Hidden: uncomment to show /scene commands
    "commands.slash.owner",
    "commands.slash.report",
    "commands.slash.z_server",
    "commands.slash.appeal",
    "commands.slash.verification_appeal",
    "commands.slash.leaderboard",
    "commands.slash.inspect",
    "commands.slash.cosmetic",
    "commands.slash.legal",
    "commands.slash.premium",
    "commands.slash.privacy",
    "commands.slash.tutorial",
]

logger.info("EXTENSIONS tuple: %r", EXTENSIONS)


async def load_extensions() -> None:
    """Load all extensions.  Log failures but keep going so one broken cog
    doesn't kill the health-check server and block Railway deploys."""
    failed: list[str] = []
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            logger.info("Loaded extension: %s", ext)
        except Exception:
            logger.exception("FAILED loading extension: %s", ext)
            failed.append(ext)
    if failed:
        logger.error("Extensions that failed to load: %s", failed)


async def sync_commands() -> None:
    env = str(getattr(config, "ENVIRONMENT", "prod")).lower().strip()
    
    # One-time maintenance: clear GLOBAL commands (these cause duplicates in the UI)
    if bool(getattr(config, "CLEAR_GLOBAL_COMMANDS_ONCE", False)):
        logger.info("🧨 CLEAR_GLOBAL_COMMANDS_ONCE=True — clearing GLOBAL commands...")
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()  # sync empty global set to Discord
        logger.info("🧨 Cleared GLOBAL commands. Now set CLEAR_GLOBAL_COMMANDS_ONCE=False and restart.")

    if env == "dev":
        # Support comma-separated IDs in SYNC_GUILD_ID / DEV_GUILD_ID via config.SYNC_GUILD_IDS / DEV_GUILD_IDS.
        guild_ids = (
            list(getattr(config, "SYNC_GUILD_IDS", []) or [])
            or list(getattr(config, "DEV_GUILD_IDS", []) or [])
        )
        if not guild_ids:
            single = getattr(config, "SYNC_GUILD_ID", None) or getattr(config, "DEV_GUILD_ID", None)
            if single:
                guild_ids = [int(single)]
        if not guild_ids:
            logger.warning("No SYNC_GUILD_ID/DEV_GUILD_ID set; skipping dev guild slash-command sync")
            return

        local_names = [c.name for c in bot.tree.get_commands()]
        logger.info("Tree command names (local/global): %s", local_names)
        logger.info("SYNC TARGET guild_ids=%s", guild_ids)

        cleanup_once = bool(getattr(config, "CLEANUP_DEV_COMMANDS_ONCE", False))

        for guild_id in guild_ids:
            guild = discord.Object(id=int(guild_id))
            if cleanup_once:
                logger.info("🧹 CLEANUP_DEV_COMMANDS_ONCE=True — clearing guild commands then re-adding (guild=%s)...", guild_id)

                # 1) Clear guild commands on Discord by syncing an empty guild set
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                logger.info("🧹 Cleared guild commands for guild=%s", guild_id)

                # 2) IMPORTANT: repopulate guild commands from your global tree
                bot.tree.copy_global_to(guild=guild)

                # 3) Sync again to actually register them
                await bot.tree.sync(guild=guild)
                logger.info("✅ Synced slash commands to guild=%s (after cleanup)", guild_id)
            else:
                # In dev, always copy globals -> guild so guild sync is instant
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                logger.info("✅ Synced slash commands to guild=%s", guild_id)

            # Small delay avoids occasional “sync finished but fetch is empty” timing weirdness
            await asyncio.sleep(1)
            try:
                cmds = await bot.tree.fetch_commands(guild=guild)
                logger.info("Discord guild commands after sync (guild=%s): %s", guild_id, [c.name for c in cmds])
            except Exception:
                logger.exception("Failed fetching commands after sync (guild=%s)", guild_id)

    else:
        await bot.tree.sync()
        logger.info("✅ Synced slash commands globally (prod)")

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------



@bot.event
async def on_ready():
    audit_log(
        "BOT_READY",
        fields={
            "bot_user": str(bot.user),
            "env": getattr(config, "ENVIRONMENT", None),
        },
    )
    logger.info("%s is ready. Logged in as %s", config.BOT_NAME, bot.user)

    try:
        from utils.prom import active_guilds
        active_guilds.set(len(bot.guilds))
    except Exception:
        pass


@bot.event
async def on_guild_join(guild):
    try:
        from utils.prom import active_guilds
        active_guilds.set(len(bot.guilds))
    except Exception:
        pass


@bot.event
async def on_guild_remove(guild):
    try:
        from utils.prom import active_guilds
        active_guilds.set(len(bot.guilds))
    except Exception:
        pass




# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def main():
    try:
        print("[boot] connecting to Discord…", flush=True)
        await bot.start(config.DISCORD_TOKEN)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Received shutdown signal")
    finally:
        # Graceful cleanup: close the Discord gateway and dispose DB engine.
        if not bot.is_closed():
            logger.info("Closing bot connection...")
            await bot.close()
        try:
            from utils.db import get_engine
            engine = get_engine()
            await engine.dispose()
            logger.info("Database engine disposed")
        except Exception:
            pass
        logger.info("Shutdown complete")


if __name__ == "__main__":
    import signal

    def _handle_signal(sig, _frame):
        logger.info("Signal %s received, initiating graceful shutdown...", signal.Signals(sig).name)
        raise KeyboardInterrupt

    # Windows uses SIGINT (Ctrl+C); Unix also supports SIGTERM (Docker/Railway stop)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on Windows

    asyncio.run(main())
