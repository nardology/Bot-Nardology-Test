# utils/reporting.py
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import discord

from utils.storage import get_guild_setting
from utils.redis_rate_limiter import RedisSlidingWindowLimiter, LimitResult
from utils.backpressure import get_redis_or_none

log = logging.getLogger("reporting")


# ============================================================
# LIMITERS (Redis)
# ============================================================

_REPORT_BURST: dict[int, RedisSlidingWindowLimiter] = {}
_REPORT_DAILY_USER: dict[int, RedisSlidingWindowLimiter] = {}
_REPORT_DAILY_GUILD: dict[int, RedisSlidingWindowLimiter] = {}

# ---------------------------------------------------------------------------
# Rate‑limiting helper
#
# The /scene and other commands reference an `is_report_rate_limited` helper
# but it was never defined.  Implement a simple rate‑limit check that
# consults the configured burst, per‑user and per‑guild daily limiters.  If
# any limiter denies the request, this helper returns True (indicating that
# a report should be suppressed).  If the limiters cannot be checked, it
# falls back to allowing the report (returns False) to avoid blocking
# legitimate submissions due to a transient Redis error.
# ---------------------------------------------------------------------------
async def is_report_rate_limited(guild_id: int, user_id: int) -> bool:
    """Return True if the user/guild should be rate‑limited for report sending."""
    try:
        # Burst limiter: quick spam protection
        burst = await get_report_limiter(guild_id)
        burst_result = await burst.check(f"report:{user_id}")

        # Daily per-user limiter
        daily_user = await get_report_daily_user_limiter(guild_id)
        daily_user_result = await daily_user.check(f"report:day:user:{user_id}")

        # Daily per-guild limiter
        daily_guild = await get_report_daily_guild_limiter(guild_id)
        daily_guild_result = await daily_guild.check("report:day:guild")

        # If any limiter denies the request, we consider it rate‑limited.
        # The LimitResult object has an `allowed` attribute; if it's False
        # then the request exceeded the limit.
        return not (
            getattr(burst_result, "allowed", True)
            and getattr(daily_user_result, "allowed", True)
            and getattr(daily_guild_result, "allowed", True)
        )
    except Exception:
        # On any error (e.g. Redis unreachable) do not rate‑limit to avoid
        # blocking legitimate reports; logging is intentionally omitted here
        # because report submission should never raise.
        return False


async def get_report_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    # Burst limiter (quick spam protection)
    max_events = int(await get_guild_setting(guild_id, "report_burst_max", 2) or 2)
    window = int(await get_guild_setting(guild_id, "report_burst_window", 30) or 30)

    limiter = _REPORT_BURST.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:report:burst:{int(guild_id)}",
        )
        _REPORT_BURST[guild_id] = limiter
    return limiter


async def get_report_daily_user_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    max_events = int(await get_guild_setting(guild_id, "report_daily_user_max", 10) or 10)
    window = int(await get_guild_setting(guild_id, "report_daily_user_window", 86400) or 86400)

    limiter = _REPORT_DAILY_USER.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:report:daily:user:{int(guild_id)}",
        )
        _REPORT_DAILY_USER[guild_id] = limiter
    return limiter


async def get_report_daily_guild_limiter(guild_id: int) -> RedisSlidingWindowLimiter:
    max_events = int(await get_guild_setting(guild_id, "report_daily_guild_max", 30) or 30)
    window = int(await get_guild_setting(guild_id, "report_daily_guild_window", 86400) or 86400)

    limiter = _REPORT_DAILY_GUILD.get(guild_id)
    if limiter is None or limiter.max_events != max_events or limiter.window_seconds != window:
        limiter = RedisSlidingWindowLimiter(
            max_events=max_events,
            window_seconds=window,
            key_prefix=f"rl:report:daily:guild:{int(guild_id)}",
        )
        _REPORT_DAILY_GUILD[guild_id] = limiter
    return limiter


# ============================================================
# REPORT CATEGORIES AND SEVERITY
# ============================================================

REPORT_CATEGORIES = {
    "harassment": "Harassment/Bullying",
    "nsfw": "NSFW Content",
    "spam": "Spam/Scam",
    "impersonation": "Impersonation",
    "violence": "Violence/Threats",
    "content": "Inappropriate Content (Pack/Character)",
    "other": "Other",
}

REPORT_SEVERITY = {
    "low": {"label": "Low", "color": 0x3498DB, "emoji": "🟦"},
    "medium": {"label": "Medium", "color": 0xF39C12, "emoji": "🟧"},
    "high": {"label": "High", "color": 0xE74C3C, "emoji": "🟥"},
    "critical": {"label": "Critical", "color": 0x8B0000, "emoji": "🔴"},
}

# Critical categories that always escalate to bot owners
CRITICAL_CATEGORIES = {"violence", "harassment", "impersonation"}

# Report statuses
REPORT_STATUSES = {
    "open": "Open",
    "investigating": "Investigating",
    "resolved": "Resolved",
    "dismissed": "Dismissed",
}

# Redis keys for report storage
KEY_REPORT_PREFIX = "report:ticket:"
KEY_REPORTS_BY_USER = "report:by_user:"  # report:by_user:<user_id> -> SET of report_ids
KEY_REPORTS_BY_CONTENT = "report:by_content:"  # report:by_content:<content_id> -> SET of report_ids
KEY_REPORTS_BY_GUILD = "report:by_guild:"  # report:by_guild:<guild_id> -> SET of report_ids
KEY_REPORTS_OPEN = "report:open"  # SET of open report_ids


# ============================================================
# REPORT STORAGE AND STATUS TRACKING
# ============================================================

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


async def store_report(
    *,
    report_id: str,
    guild_id: int,
    reporter_id: int,
    category: str,
    severity: str,
    description: str,
    reported_user_id: int | None = None,
    reported_content_id: str | None = None,
    evidence: Dict[str, Any] | None = None,
    anonymous: bool = False,
) -> bool:
    """Store a report in Redis with status tracking."""
    r = await get_redis_or_none()
    if r is None:
        return False

    report_data = {
        "report_id": report_id,
        "guild_id": int(guild_id),
        "reporter_id": int(reporter_id) if not anonymous else 0,  # Store 0 if anonymous, but keep real ID for internal tracking
        "reporter_id_real": int(reporter_id),  # Always store real ID for feedback loop
        "anonymous": bool(anonymous),
        "category": str(category),
        "severity": str(severity),
        "description": str(description),
        "reported_user_id": int(reported_user_id) if reported_user_id else None,
        "reported_content_id": str(reported_content_id) if reported_content_id else None,
        "evidence": evidence or {},
        "status": "open",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }

    try:
        # Store report
        await r.set(f"{KEY_REPORT_PREFIX}{report_id}", _json_dumps(report_data), ex=86400 * 90)  # 90 day TTL

        # Add to open reports set
        await r.sadd(KEY_REPORTS_OPEN, report_id)

        # Index by reported user
        if reported_user_id:
            await r.sadd(f"{KEY_REPORTS_BY_USER}{reported_user_id}", report_id)

        # Index by content
        if reported_content_id:
            await r.sadd(f"{KEY_REPORTS_BY_CONTENT}{reported_content_id}", report_id)

        # Index by guild
        await r.sadd(f"{KEY_REPORTS_BY_GUILD}{guild_id}", report_id)

        return True
    except Exception:
        log.exception("Failed to store report %s", report_id)
        return False


async def get_report(report_id: str) -> Dict[str, Any]:
    """Get a report by ID."""
    r = await get_redis_or_none()
    if r is None:
        return {}
    try:
        raw = await r.get(f"{KEY_REPORT_PREFIX}{report_id}")
        if not raw:
            return {}
        return _json_loads(raw)
    except Exception:
        return {}


async def update_report_status(
    *,
    report_id: str,
    status: str,
    updated_by: int,
    notes: str = "",
) -> bool:
    """Update report status."""
    r = await get_redis_or_none()
    if r is None:
        return False

    report = await get_report(report_id)
    if not report:
        return False

    report["status"] = str(status)
    report["updated_at"] = int(time.time())
    report["updated_by"] = int(updated_by)
    if notes:
        report["notes"] = str(notes)[:500]

    try:
        await r.set(f"{KEY_REPORT_PREFIX}{report_id}", _json_dumps(report), ex=86400 * 90)

        # Update open reports set
        if status == "open":
            await r.sadd(KEY_REPORTS_OPEN, report_id)
        else:
            await r.srem(KEY_REPORTS_OPEN, report_id)

        return True
    except Exception:
        log.exception("Failed to update report status %s", report_id)
        return False


async def get_reports_by_user(user_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Get all reports for a specific user."""
    r = await get_redis_or_none()
    if r is None:
        return []

    try:
        report_ids = await r.smembers(f"{KEY_REPORTS_BY_USER}{user_id}")
        reports = []
        for rid_raw in report_ids or []:
            rid = rid_raw.decode("utf-8", errors="ignore") if isinstance(rid_raw, (bytes, bytearray)) else str(rid_raw)
            report = await get_report(rid)
            if report:
                reports.append(report)
            if len(reports) >= limit:
                break
        return reports
    except Exception:
        return []


async def get_reports_by_content(content_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Get all reports for specific content."""
    r = await get_redis_or_none()
    if r is None:
        return []

    try:
        report_ids = await r.smembers(f"{KEY_REPORTS_BY_CONTENT}{content_id}")
        reports = []
        for rid_raw in report_ids or []:
            rid = rid_raw.decode("utf-8", errors="ignore") if isinstance(rid_raw, (bytes, bytearray)) else str(rid_raw)
            report = await get_report(rid)
            if report:
                reports.append(report)
            if len(reports) >= limit:
                break
        return reports
    except Exception:
        return []


async def get_open_reports(*, guild_id: int | None = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Get open reports, optionally filtered by guild."""
    r = await get_redis_or_none()
    if r is None:
        return []

    try:
        report_ids = await r.smembers(KEY_REPORTS_OPEN)
        reports = []
        for rid_raw in report_ids or []:
            rid = rid_raw.decode("utf-8", errors="ignore") if isinstance(rid_raw, (bytes, bytearray)) else str(rid_raw)
            report = await get_report(rid)
            if report and report.get("status") == "open":
                if guild_id is None or int(report.get("guild_id", 0)) == int(guild_id):
                    reports.append(report)
            if len(reports) >= limit:
                break
        # Sort by created_at (oldest first)
        reports.sort(key=lambda x: int(x.get("created_at", 0) or 0))
        return reports
    except Exception:
        return []


async def detect_repeat_offender(*, user_id: int, threshold: int = 3) -> tuple[bool, int, List[Dict[str, Any]]]:
    """
    Detect if a user is a repeat offender.
    Returns (is_repeat_offender, report_count, recent_reports).
    """
    reports = await get_reports_by_user(user_id, limit=100)
    open_reports = [r for r in reports if r.get("status") == "open"]
    high_severity = [r for r in reports if r.get("severity") in {"high", "critical"}]

    count = len(open_reports)
    is_repeat = count >= threshold or len(high_severity) >= 2

    return is_repeat, count, reports[:10]  # Return last 10 reports


# ============================================================
# SENDING
# ============================================================

async def send_report(
    *,
    bot: discord.Client,
    guild_id: int,
    title: str,
    description: str,
    fields: list[tuple[str, str]] | None = None,
    category: str | None = None,
    severity: str | None = None,
    reporter_id: int | None = None,
    reported_user_id: int | None = None,
    reported_content_id: str | None = None,
    evidence: dict | None = None,
) -> tuple[bool, str | None]:
    """Send a report embed to the configured report channel with quick action buttons.

    Returns (success, report_id).
    """
    channel_id = await get_guild_setting(guild_id, "report_channel_id", 0)
    if not channel_id:
        return False, None

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return False, None

    # Generate report ID
    report_id = uuid.uuid4().hex[:16]

    # Store report (anonymous flag will be set by modal if user requests it)
    await store_report(
        report_id=report_id,
        guild_id=guild_id,
        reporter_id=reporter_id or 0,
        category=category or "other",
        severity=severity or "medium",
        description=description,
        reported_user_id=reported_user_id,
        reported_content_id=reported_content_id,
        evidence=evidence,
        anonymous=False,  # Will be set by modal
    )

    # Check for repeat offender
    is_repeat, report_count, recent_reports = await detect_repeat_offender(
        user_id=reported_user_id or 0, threshold=3
    ) if reported_user_id else (False, 0, [])

    # Determine color based on severity
    color = 0xED4245  # Default red
    if severity and severity in REPORT_SEVERITY:
        color = REPORT_SEVERITY[severity]["color"]

    embed = discord.Embed(
        title=title,
        description=description,
        timestamp=datetime.now(timezone.utc),
        color=color,
    )

    # Add report ID
    embed.set_footer(text=f"Report ID: {report_id}")

    # Add category and severity
    if category and category in REPORT_CATEGORIES:
        embed.add_field(name="Category", value=REPORT_CATEGORIES[category], inline=True)
    if severity and severity in REPORT_SEVERITY:
        sev_info = REPORT_SEVERITY[severity]
        embed.add_field(name="Severity", value=f"{sev_info['emoji']} {sev_info['label']}", inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)

    # Add evidence fields
    # Check if report is anonymous
    is_anonymous = False
    if report_id:
        report_data = await get_report(report_id)
        is_anonymous = bool(report_data.get("anonymous", False))
    
    if reporter_id and not is_anonymous:
        embed.add_field(name="Reporter", value=f"<@{reporter_id}> (`{reporter_id}`)", inline=True)
    elif is_anonymous:
        embed.add_field(name="Reporter", value="🔒 Anonymous", inline=True)
    if reported_user_id:
        user_mention = f"<@{reported_user_id}> (`{reported_user_id}`)"
        if is_repeat:
            user_mention += f" ⚠️ **REPEAT OFFENDER** ({report_count} reports)"
        embed.add_field(name="Reported User", value=user_mention, inline=True)
    if reported_content_id:
        embed.add_field(name="Content ID", value=f"`{reported_content_id}`", inline=True)

    # Add evidence (message links, etc.)
    if evidence:
        evidence_lines = []
        if evidence.get("message_link"):
            evidence_lines.append(f"Message: {evidence['message_link']}")
        if evidence.get("message_id"):
            evidence_lines.append(f"Message ID: `{evidence['message_id']}`")
        if evidence.get("channel_id"):
            evidence_lines.append(f"Channel: <#{evidence['channel_id']}>")
        if evidence.get("guild_id"):
            evidence_lines.append(f"Guild ID: `{evidence['guild_id']}`")
        if evidence_lines:
            embed.add_field(name="Evidence", value="\n".join(evidence_lines), inline=False)

    if fields:
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)

    # Create quick action view (admin-only)
    view = ReportActionView(
        report_id=report_id,
        reported_user_id=reported_user_id,
        reported_content_id=reported_content_id,
        bot=bot,
    )

    try:
        await channel.send(embed=embed, view=view)
        return True, report_id
    except Exception:
        log.exception("Failed to send report")
        return False, None


async def send_global_report(
    *,
    bot: discord.Client,
    guild_id: int,
    title: str,
    description: str,
    category: str | None = None,
    severity: str | None = None,
    reporter_id: int | None = None,
    reported_user_id: int | None = None,
    reported_content_id: str | None = None,
    evidence: dict | None = None,
) -> int:
    """Send a report to bot owners via DM (for critical/global issues).

    Returns number of owners successfully notified.
    """
    import config

    # Determine color based on severity
    color = 0xED4245  # Default red
    if severity and severity in REPORT_SEVERITY:
        color = REPORT_SEVERITY[severity]["color"]

    embed = discord.Embed(
        title=f"🚨 Global Report: {title}",
        description=description,
        timestamp=datetime.now(timezone.utc),
        color=color,
    )

    # Add category and severity
    if category and category in REPORT_CATEGORIES:
        embed.add_field(name="Category", value=REPORT_CATEGORIES[category], inline=True)
    if severity and severity in REPORT_SEVERITY:
        sev_info = REPORT_SEVERITY[severity]
        embed.add_field(name="Severity", value=f"{sev_info['emoji']} {sev_info['label']}", inline=True)

    embed.add_field(name="Guild ID", value=f"`{guild_id}`", inline=True)

    # Add evidence fields
    if reporter_id:
        embed.add_field(name="Reporter", value=f"<@{reporter_id}> (`{reporter_id}`)", inline=True)
    if reported_user_id:
        embed.add_field(name="Reported User", value=f"<@{reported_user_id}> (`{reported_user_id}`)", inline=True)
    if reported_content_id:
        embed.add_field(name="Content ID", value=f"`{reported_content_id}`", inline=True)

    # Add evidence
    if evidence:
        evidence_lines = []
        if evidence.get("message_link"):
            evidence_lines.append(f"Message: {evidence['message_link']}")
        if evidence.get("message_id"):
            evidence_lines.append(f"Message ID: `{evidence['message_id']}`")
        if evidence.get("channel_id"):
            evidence_lines.append(f"Channel: <#{evidence['channel_id']}>")
        if evidence_lines:
            embed.add_field(name="Evidence", value="\n".join(evidence_lines), inline=False)

    sent = 0
    for oid in sorted(getattr(config, "BOT_OWNER_IDS", set()) or set()):
        try:
            owner = await bot.fetch_user(int(oid))
            await owner.send(embed=embed)
            sent += 1
        except Exception:
            log.exception("Failed to DM owner %s for global report", oid)
            continue

    return sent


# ---------------------------------------------------------------------------
# Quick Action Buttons for Admins
# ---------------------------------------------------------------------------

class ReportActionView(discord.ui.View):
    """Quick action buttons for admins on report embeds."""

    def __init__(
        self,
        *,
        report_id: str,
        reported_user_id: int | None = None,
        reported_content_id: str | None = None,
        bot: discord.Client,
    ):
        super().__init__(timeout=None)  # Persistent view
        self.report_id = report_id
        self.reported_user_id = reported_user_id
        self.reported_content_id = reported_content_id
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow admins/managers
        if not interaction.guild:
            return False
        if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
            try:
                await interaction.response.send_message("Only administrators can use these buttons.", ephemeral=True)
            except Exception:
                pass
            return False
        return True

    @discord.ui.button(label="Ban User", style=discord.ButtonStyle.danger, emoji="🔨")
    async def ban_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.reported_user_id:
            await interaction.response.send_message("No user ID in this report.", ephemeral=True)
            return

        from utils.mod_actions import ban_user

        try:
            await ban_user(
                user_id=self.reported_user_id,
                reason=f"Banned via report {self.report_id}",
                by_user_id=int(interaction.user.id),
            )
            await update_report_status(
                report_id=self.report_id,
                status="resolved",
                updated_by=int(interaction.user.id),
                notes=f"User banned by {interaction.user}",
            )
            # Notify reporter (if not anonymous)
            await _notify_reporter_feedback(
                bot=self.bot,
                report_id=self.report_id,
                action="User banned",
                action_by=interaction.user,
            )
            await interaction.response.send_message(
                f"✅ User <@{self.reported_user_id}> has been banned. Report marked as resolved.",
                ephemeral=True,
            )
            # Update embed
            await self._update_embed(interaction, f"Resolved - User banned by {interaction.user}")
        except Exception:
            log.exception("Failed to ban user from report")
            await interaction.response.send_message("⚠️ Failed to ban user. Check logs.", ephemeral=True)

    @discord.ui.button(label="Disable Content", style=discord.ButtonStyle.danger, emoji="🚫")
    async def disable_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.reported_content_id:
            await interaction.response.send_message("No content ID in this report.", ephemeral=True)
            return

        try:
            from utils.packs_store import get_custom_pack, upsert_custom_pack
            from utils.character_registry import merge_pack_payload

            pack = await get_custom_pack(self.reported_content_id)
            if pack:
                pack["disabled_by_report"] = True
                pack["disabled_reason"] = f"Disabled via report {self.report_id} by {interaction.user}"
                await upsert_custom_pack(pack)
                try:
                    merge_pack_payload(pack)
                except Exception:
                    pass
                await update_report_status(
                    report_id=self.report_id,
                    status="resolved",
                    updated_by=int(interaction.user.id),
                    notes=f"Content disabled by {interaction.user}",
                )
                # Notify reporter (if not anonymous)
                await _notify_reporter_feedback(
                    bot=self.bot,
                    report_id=self.report_id,
                    action="Content disabled",
                    action_by=interaction.user,
                )
                await interaction.response.send_message(
                    f"✅ Content `{self.reported_content_id}` has been disabled. Report marked as resolved.",
                    ephemeral=True,
                )
                await self._update_embed(interaction, f"Resolved - Content disabled by {interaction.user}")
            else:
                await interaction.response.send_message("⚠️ Content not found.", ephemeral=True)
        except Exception:
            log.exception("Failed to disable content from report")
            await interaction.response.send_message("⚠️ Failed to disable content. Check logs.", ephemeral=True)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="❌")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_report_status(
            report_id=self.report_id,
            status="dismissed",
            updated_by=int(interaction.user.id),
            notes=f"Dismissed by {interaction.user}",
        )
        # Notify reporter (if not anonymous)
        await _notify_reporter_feedback(
            bot=self.bot,
            report_id=self.report_id,
            action="Report dismissed",
            action_by=interaction.user,
        )
        await interaction.response.send_message("✅ Report dismissed.", ephemeral=True)
        await self._update_embed(interaction, f"Dismissed by {interaction.user}")

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.primary, emoji="⬆️")
    async def escalate(self, interaction: discord.Interaction, button: discord.ui.Button):
        report = await get_report(self.report_id)
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return

        # Send to bot owners
        import config

        embed = discord.Embed(
            title=f"🚨 Escalated Report: {report.get('category', 'unknown')}",
            description=report.get("description", ""),
            color=0x8B0000,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Report ID", value=f"`{self.report_id}`", inline=True)
        embed.add_field(name="Severity", value=report.get("severity", "unknown"), inline=True)
        embed.add_field(name="Escalated by", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)

        sent = 0
        for oid in sorted(getattr(config, "BOT_OWNER_IDS", set()) or set()):
            try:
                owner = await self.bot.fetch_user(int(oid))
                await owner.send(embed=embed)
                sent += 1
            except Exception:
                continue

        await update_report_status(
            report_id=self.report_id,
            status="investigating",
            updated_by=int(interaction.user.id),
            notes=f"Escalated to bot owners by {interaction.user}",
        )
        # Notify reporter (if not anonymous)
        await _notify_reporter_feedback(
            bot=self.bot,
            report_id=self.report_id,
            action="Escalated to bot owners",
            action_by=interaction.user,
        )
        await interaction.response.send_message(
            f"✅ Report escalated to bot owners ({sent} notified). Status: Investigating.",
            ephemeral=True,
        )
        await self._update_embed(interaction, f"Investigating - Escalated by {interaction.user}")

    async def _update_embed(self, interaction: discord.Interaction, status_text: str):
        """Update the report embed with new status."""
        try:
            if interaction.message:
                embed = interaction.message.embeds[0] if interaction.message.embeds else None
                if embed:
                    # Update status field
                    for i, field in enumerate(embed.fields):
                        if field.name == "Status":
                            embed.set_field_at(i, name="Status", value=f"🟡 {status_text}", inline=True)
                            break
                    await interaction.message.edit(embed=embed, view=None)  # Remove buttons after action
        except Exception:
            log.exception("Failed to update report embed")


# ---------------------------------------------------------------------------
# User Feedback Loop
# ---------------------------------------------------------------------------

async def _notify_reporter_feedback(
    *,
    bot: discord.Client,
    report_id: str,
    action: str,
    action_by: discord.User,
) -> None:
    """Notify the reporter when action is taken on their report."""
    report = await get_report(report_id)
    if not report:
        return

    # Don't notify if anonymous
    if bool(report.get("anonymous", False)):
        return

    # Get real reporter ID (stored even for anonymous reports)
    reporter_id = int(report.get("reporter_id_real") or report.get("reporter_id") or 0)
    if not reporter_id:
        return

    try:
        user = await bot.fetch_user(reporter_id)
        embed = discord.Embed(
            title="📬 Report Update",
            description=f"Action taken on your report `{report_id}`",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Action", value=action, inline=False)
        embed.add_field(name="Action by", value=f"{action_by} (`{action_by.id}`)", inline=False)
        embed.add_field(name="Status", value=REPORT_STATUSES.get(report.get("status", "open"), "Unknown"), inline=False)
        if report.get("notes"):
            embed.add_field(name="Notes", value=str(report.get("notes"))[:500], inline=False)
        await user.send(embed=embed)
    except Exception:
        log.exception("Failed to notify reporter %s for report %s", reporter_id, report_id)


# ---------------------------------------------------------------------------
# Discord UI helpers
#
# Some commands (e.g. /scene) attach a "Report" button to their response.
# Earlier iterations referenced a ReportView but never implemented it, which
# causes an ImportError at startup. The classes below provide a safe,
# rate-limited reporting flow:
#   - user clicks a button
#   - a modal collects details
#   - the report is delivered to the configured report channel
#
# These are intentionally dependency-light: they rely only on send_report()
# and the limiter helpers in this module.
# ---------------------------------------------------------------------------


class _ReportModal(discord.ui.Modal, title="Send a report"):
    """Collects report text from the user and submits it."""

    details: discord.ui.TextInput = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=5,
        max_length=1500,
        placeholder="Describe the issue or feedback...",
    )

    def __init__(
        self,
        *,
        bot,
        guild_id: int,
        user_id: int,
        context: str,
        report_title: str,
        report_description: str,
    ):
        super().__init__()
        self._bot = bot
        self._guild_id = int(guild_id)
        self._user_id = int(user_id)
        self._context = str(context or "")
        self._report_title = str(report_title or "Report")
        self._report_description = str(report_description or "")

    async def on_submit(self, interaction: discord.Interaction):
        # Rate-limit per-user and per-guild.
        if await is_report_rate_limited(self._guild_id, interaction.user.id):
            await interaction.response.send_message(
                "⏳ You’re sending reports too quickly. Please wait a bit and try again.",
                ephemeral=True,
            )
            return

        # Build message.
        who = f"{interaction.user} ({interaction.user.id})"
        content = (
            f"**Title:** {self._report_title}\n"
            f"**Context:** {self._context}\n"
            f"**From:** {who}\n\n"
            f"{self._report_description}\n\n"
            f"**User details:**\n{self.details.value.strip()}"
        ).strip()

        ok = await send_report(
            bot=self._bot,
            guild_id=self._guild_id,
            title=self._report_title,
            description=content,
        )
        if ok:
            await interaction.response.send_message("✅ Report sent. Thank you!", ephemeral=True)
        else:
            await interaction.response.send_message(
                "⚠️ I couldn’t send that report (no report channel set or a permissions error).",
                ephemeral=True,
            )


class ReportView(discord.ui.View):
    """A reusable view that posts a report via a modal when the user clicks."""

    def __init__(
        self,
        *,
        bot,
        guild_id: int,
        user_id: int,
        context: str = "",
        label: str = "Report",
        report_title: str = "Report",
        report_description: str = "",
        timeout: Optional[float] = 300.0,
    ):
        super().__init__(timeout=timeout)
        self._bot = bot
        self._guild_id = int(guild_id)
        self._user_id = int(user_id)
        self._context = str(context or "")
        self._label = str(label or "Report")
        self._report_title = str(report_title or "Report")
        self._report_description = str(report_description or "")
        self._reported_user_id = int(reported_user_id) if reported_user_id else None
        self._reported_content_id = str(reported_content_id) if reported_content_id else None
        self._evidence = evidence or {}

        # Add button dynamically so the label is configurable.
        self.add_item(_ReportButton(self._label))

    async def _open_modal(self, interaction: discord.Interaction):
        # Only allow the user who triggered the original action to submit,
        # to avoid other members spamming reports from someone else’s message.
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this button.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            _ReportModal(
                bot=self._bot,
                guild_id=self._guild_id,
                user_id=self._user_id,
                context=self._context,
                report_title=self._report_title,
                report_description=self._report_description,
                reported_user_id=getattr(self, "_reported_user_id", None),
                reported_content_id=getattr(self, "_reported_content_id", None),
                evidence=getattr(self, "_evidence", None),
            )
        )


class _ReportButton(discord.ui.Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, ReportView):
            await view._open_modal(interaction)
        else:
            await interaction.response.send_message("This button is misconfigured.", ephemeral=True)
