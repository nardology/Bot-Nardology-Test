# commands/slash/voice.py
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from utils.premium import get_premium_tier
from utils.object_store import storage_mode, upload_bytes, download_bytes, delete_object
from utils.voice_store import list_voice_sounds, get_voice_sound, upsert_voice_sound, delete_voice_sound


# -------------------------
# Storage layout (IMPORTANT)
# -------------------------
# Built-in sounds ship with the repo at ./sounds (persist in your code)
# Custom uploaded sounds go to /data/sounds/<guild_id>/ (persist only if Railway Volume mounted to /data)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_SOUNDS_DIR = (PROJECT_ROOT / "sounds").resolve()

# Optional override, but default matches Railway Volume convention:
PERSIST_SOUNDS_ROOT = Path(os.getenv("VOICE_UPLOADS_DIR", "/data/sounds")).resolve()
PERSIST_SOUNDS_ROOT.mkdir(parents=True, exist_ok=True)

# Temp cache for object-storage-backed sounds (safe to wipe on restart)
VOICE_CACHE_ROOT = Path(os.getenv("VOICE_CACHE_DIR", "/tmp/voice_cache")).resolve()
VOICE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def guild_upload_dir(guild_id: int) -> Path:
    return PERSIST_SOUNDS_ROOT / str(guild_id)


# -------------------------
# Limits / validation
# -------------------------
ALLOWED_EXTS = {".wav"}  # keep simple/safe
MAX_FILE_BYTES = 3 * 1024 * 1024  # 3 MB
MAX_SOUNDS_PER_GUILD = 25

_NAME_RE = re.compile(r"[^a-z0-9_]+")

AUTOCOMPLETE_MAX = 25  # Discord choice limit


def _safe_stem(name: str) -> str:
    n = (name or "").strip().lower().replace(" ", "_")
    n = _NAME_RE.sub("", n)
    return n[:32]


def _iter_sound_stems(folder: Path) -> Iterable[str]:
    if not folder.exists():
        return []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
            yield p.stem.lower()


async def list_custom_sounds(guild_id: int) -> list[str]:
    """List custom sounds for this guild.

    - local mode: scan /data/sounds/<guild>/
    - s3 mode: query DB registry
    """
    mode = storage_mode()
    if mode == "s3":
        try:
            rows = await list_voice_sounds(guild_id=guild_id)
            return sorted({r.name.lower() for r in rows})
        except Exception:
            # Fallback to disk if DB is unavailable (avoid breaking UX)
            return sorted(set(_iter_sound_stems(guild_upload_dir(guild_id))))
    return sorted(set(_iter_sound_stems(guild_upload_dir(guild_id))))


def list_builtin_sounds() -> list[str]:
    return sorted(set(_iter_sound_stems(BUILTIN_SOUNDS_DIR)))


async def list_all_sounds(guild_id: int) -> list[str]:
    """
    Combine:
    - Guild uploads in /data/sounds/<guild_id>/
    - Built-in sounds in ./sounds/
    """
    names = set(await list_custom_sounds(guild_id))
    names.update(list_builtin_sounds())
    return sorted(names)


async def resolve_sound(guild_id: int, name: str) -> Path | None:
    """
    Prefer guild uploads first, then built-ins.
    """
    n = (name or "").strip().lower()
    if not n:
        return None

    # 1) Guild uploads (local)
    gd = guild_upload_dir(guild_id)
    for ext in ALLOWED_EXTS:
        p = gd / f"{n}{ext}"
        if p.exists():
            return p

    # 1b) Object storage (download to local cache on demand)
    if storage_mode() == "s3":
        rec = None
        try:
            rec = await get_voice_sound(guild_id=guild_id, name=n)
        except Exception:
            rec = None

        if rec and rec.storage_mode == "s3" and rec.object_key:
            cache_dir = VOICE_CACHE_ROOT / str(guild_id)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"{n}.wav"
            if cache_path.exists() and cache_path.stat().st_size > 0:
                return cache_path
            try:
                data = await download_bytes(key=rec.object_key)
                if data:
                    cache_path.write_bytes(data)
                    return cache_path
            except Exception:
                return None

    # 2) Built-in global sounds
    for ext in ALLOWED_EXTS:
        p = BUILTIN_SOUNDS_DIR / f"{n}{ext}"
        if p.exists():
            return p

    # 3) last-resort: match stem ignoring extension
    for folder in (gd, BUILTIN_SOUNDS_DIR):
        if folder.exists():
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in ALLOWED_EXTS and p.stem.lower() == n:
                    return p

    return None


async def send_ephemeral(interaction: discord.Interaction, content: str):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


# -------------------------
# Autocomplete handlers
# -------------------------
async def ac_voice_play(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /voice play: uploads + built-ins, with UX labels.
    """
    if not interaction.guild:
        return []

    guild_id = interaction.guild.id
    cur = (current or "").strip().lower()

    custom = await list_custom_sounds(guild_id)
    builtin = list_builtin_sounds()

    # Build labeled choices. Value must be the actual sound key (stem).
    # Label shows source to reduce confusion.
    items: list[tuple[str, str]] = []
    for s in custom:
        items.append((f"{s}  [custom]", s))
    for s in builtin:
        items.append((f"{s}  [built-in]", s))

    if cur:
        items = [it for it in items if cur in it[1].lower() or cur in it[0].lower()]

    # If both custom and builtin share the same stem, show custom first (already is).
    seen_vals = set()
    choices: list[app_commands.Choice[str]] = []
    for label, val in items:
        if val in seen_vals:
            continue
        seen_vals.add(val)
        choices.append(app_commands.Choice(name=label, value=val))
        if len(choices) >= AUTOCOMPLETE_MAX:
            break

    return choices


async def ac_voice_remove(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete for /voice remove: uploads only (with UX label).
    """
    if not interaction.guild:
        return []

    guild_id = interaction.guild.id
    cur = (current or "").strip().lower()

    uploads = await list_custom_sounds(guild_id)
    items = [(f"{s}  [custom]", s) for s in uploads]

    if cur:
        items = [it for it in items if cur in it[1].lower() or cur in it[0].lower()]

    return [app_commands.Choice(name=label, value=val) for (label, val) in items[:AUTOCOMPLETE_MAX]]


class SlashVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    voice_group = app_commands.Group(name="voice", description="Play and manage voice sounds")

    @voice_group.command(name="list", description="List available sounds")
    async def voice_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return

        guild_id = interaction.guild.id
        custom = await list_custom_sounds(guild_id)
        builtin = list_builtin_sounds()

        if not custom and not builtin:
            await send_ephemeral(
                interaction,
                "No sounds found. Built-ins should be in `./sounds/`. Uploads save to `/data/sounds/`.",
            )
            return

        def fmt(names: list[str], tag: str) -> str:
            if not names:
                return f"**{tag}:** (none)"
            preview = ", ".join(names[:40])
            more = "" if len(names) <= 40 else f" (+{len(names) - 40} more)"
            return f"**{tag}:** {preview}{more}"

        msg = "\n".join(
            [
                fmt(custom, "Custom (persisted)"),
                fmt(builtin, "Built-in (repo)"),
                "",
                "Use `/voice play sound:<name>`",
            ]
        )
        await send_ephemeral(interaction, msg)

    @voice_group.command(name="play", description="Join your voice channel and play a sound")
    @app_commands.describe(sound="Name of the sound (see /voice list)")
    @app_commands.autocomplete(sound=ac_voice_play)
    async def voice_play(self, interaction: discord.Interaction, sound: str):
        if not interaction.guild:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return

        if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None:
            await send_ephemeral(interaction, "Join a voice channel first, then try again.")
            return

        guild_id = interaction.guild.id
        sound_path = await resolve_sound(guild_id, sound)
        if sound_path is None:
            available = await list_all_sounds(guild_id)
            if available:
                preview = ", ".join(available[:20])
                more = "" if len(available) <= 20 else f" (+{len(available) - 20} more)"
                await send_ephemeral(interaction, f"❌ I can’t find **{sound}**.\nAvailable: {preview}{more}")
            else:
                await send_ephemeral(interaction, "❌ No sounds found. Try `/voice list`.")
            return

        channel = interaction.user.voice.channel

        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id != channel.id:
                await send_ephemeral(
                    interaction,
                    f"⚠️ I’m already in **{vc.channel.name}**. Join that channel or run `/voice stop` first.",
                )
                return
            if vc.is_playing():
                await send_ephemeral(interaction, "⏳ I’m already playing audio. Try again in a moment.")
                return
        else:
            vc = await channel.connect()

        # UX: tell source
        sp = sound_path.resolve().as_posix()
        if sp.startswith(BUILTIN_SOUNDS_DIR.resolve().as_posix()):
            src_tag = "built-in"
        elif sp.startswith(guild_upload_dir(guild_id).resolve().as_posix()):
            src_tag = "custom"
        elif sp.startswith(VOICE_CACHE_ROOT.resolve().as_posix()):
            src_tag = "custom (object)"
        else:
            src_tag = "custom"
        await send_ephemeral(interaction, f"▶️ Playing **{sound_path.stem}**  [{src_tag}]")

        src = discord.FFmpegPCMAudio(str(sound_path))
        vc.play(src)

        # Wait until done, then disconnect
        while vc.is_playing():
            await asyncio.sleep(0.2)

        try:
            await vc.disconnect()
        except Exception:
            pass

    @voice_group.command(
        name="add",
        description="(Pro) Upload a .wav sound for this server (persists if /data is a volume)",
    )
    @app_commands.describe(file="Attach a .wav under 3MB", name="Optional name override")
    async def voice_add(self, interaction: discord.Interaction, file: discord.Attachment, name: str | None = None):
        if not interaction.guild:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return

        tier = await get_premium_tier(interaction.user.id)
        if tier != "pro":
            await send_ephemeral(interaction, "🔒 `/voice add` is Pro-only.")
            return

        if not file or not file.filename:
            await send_ephemeral(interaction, "Attach a `.wav` file.")
            return

        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            await send_ephemeral(interaction, "❌ Only `.wav` is allowed right now.")
            return

        if file.size and file.size > MAX_FILE_BYTES:
            await send_ephemeral(interaction, f"❌ File too large. Max size is {MAX_FILE_BYTES // (1024*1024)} MB.")
            return

        safe_name = _safe_stem(name or Path(file.filename).stem)
        if not safe_name:
            await send_ephemeral(interaction, "❌ Invalid sound name.")
            return

        guild_id = interaction.guild.id
        gdir = guild_upload_dir(guild_id)
        gdir.mkdir(parents=True, exist_ok=True)

        existing = set(await list_custom_sounds(guild_id))
        if safe_name not in existing and len(existing) >= MAX_SOUNDS_PER_GUILD:
            await send_ephemeral(interaction, f"❌ This server already has {MAX_SOUNDS_PER_GUILD} custom sounds.")
            return

        dest = gdir / f"{safe_name}{ext}"

        try:
            data = await file.read()
        except Exception:
            await send_ephemeral(interaction, "⚠️ Couldn’t download that attachment. Try again.")
            return

        if len(data) > MAX_FILE_BYTES:
            await send_ephemeral(interaction, f"❌ File too large. Max size is {MAX_FILE_BYTES // (1024*1024)} MB.")
            return

        mode = storage_mode()
        if mode == "s3":
            # Upload to object storage and store mapping in Postgres.
            key = f"voice/{guild_id}/{safe_name}{ext}"
            try:
                ref = await upload_bytes(key=key, data=data, content_type="audio/wav")
                await upsert_voice_sound(
                    guild_id=guild_id,
                    name=safe_name,
                    storage_mode="s3",
                    object_key=ref.key,
                    url=ref.url,
                )
            except Exception as e:
                await send_ephemeral(interaction, f"⚠️ Upload failed.\nError: `{type(e).__name__}`")
                return

            # Optional local cache for quick playback on this instance
            try:
                dest.write_bytes(data)
            except Exception:
                pass

            await send_ephemeral(
                interaction,
                f"✅ Added **{safe_name}** (stored in object storage).\nUse: `/voice play sound:{safe_name}`",
            )
            return

        # Default: local/volume storage
        try:
            dest.write_bytes(data)
        except Exception as e:
            await send_ephemeral(interaction, f"⚠️ Failed to save the file on disk.\nError: `{type(e).__name__}`")
            return

        try:
            await upsert_voice_sound(
                guild_id=guild_id,
                name=safe_name,
                storage_mode="local",
                object_key=None,
                url=None,
            )
        except Exception:
            pass

        await send_ephemeral(interaction, f"✅ Added **{safe_name}**.\nUse: `/voice play sound:{safe_name}`")

    @voice_group.command(name="remove", description="(Pro) Remove an uploaded server sound")
    @app_commands.describe(sound="Sound name to remove (server uploads only)")
    @app_commands.autocomplete(sound=ac_voice_remove)
    async def voice_remove(self, interaction: discord.Interaction, sound: str):
        if not interaction.guild:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return

        tier = await get_premium_tier(interaction.user.id)
        if tier != "pro":
            await send_ephemeral(interaction, "🔒 `/voice remove` is Pro-only.")
            return

        safe = _safe_stem(sound)
        if not safe:
            await send_ephemeral(interaction, "❌ Invalid sound name.")
            return

        guild_id = interaction.guild.id
        gdir = guild_upload_dir(guild_id)
        removed = False
        for ext in ALLOWED_EXTS:
            p = gdir / f"{safe}{ext}"
            if p.exists():
                try:
                    p.unlink()
                    removed = True
                except Exception:
                    pass

        # Also delete from registry / object storage if configured
        try:
            rec = await get_voice_sound(guild_id=guild_id, name=safe)
        except Exception:
            rec = None

        if rec and rec.storage_mode == "s3" and rec.object_key:
            try:
                await delete_object(key=rec.object_key)
            except Exception:
                pass

        try:
            await delete_voice_sound(guild_id=guild_id, name=safe)
            removed = True
        except Exception:
            pass

        if removed:
            await send_ephemeral(interaction, f"🗑️ Removed **{safe}**.")
        else:
            await send_ephemeral(interaction, f"❌ I couldn’t find an uploaded sound named **{safe}**.")

    @voice_group.command(name="stop", description="Stop playback and leave")
    async def voice_stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await send_ephemeral(interaction, "This command can only be used in a server.")
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await send_ephemeral(interaction, "I’m not in a voice channel.")
            return

        try:
            if vc.is_playing():
                vc.stop()
        finally:
            try:
                await vc.disconnect()
            except Exception:
                pass

        await send_ephemeral(interaction, "🛑 Stopped and left.")


async def setup(bot: commands.Bot):
    if bot.get_cog("SlashVoice") is None:
        await bot.add_cog(SlashVoice(bot))
