from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.storage import set_guild_setting, get_guild_setting
from utils.reporting import (
    send_report,
    send_global_report,
    REPORT_CATEGORIES,
    REPORT_SEVERITY,
    CRITICAL_CATEGORIES,
    get_report,
    get_open_reports,
    get_reports_by_user,
    get_reports_by_content,
    update_report_status,
    detect_repeat_offender,
    REPORT_STATUSES,
)


class SlashReport(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    report = app_commands.Group(name="report", description="Owner/admin reporting tools")

    @report.command(name="channel-set", description="Set the channel that receives bot reports")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_channel_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await set_guild_setting(interaction.guild.id, "report_channel_id", int(channel.id))
        await interaction.response.send_message(f"✅ Reports will be sent to {channel.mention}", ephemeral=True)

    @report.command(name="channel-view", description="View the current report channel")
    async def report_channel_view(self, interaction: discord.Interaction):
        cid = await get_guild_setting(interaction.guild.id, "report_channel_id", default=0)
        try:
            cid = int(cid or 0)
        except Exception:
            cid = 0
        if not cid:
            await interaction.response.send_message("No report channel set.", ephemeral=True)
            return
        await interaction.response.send_message(f"Current report channel: <#{cid}>", ephemeral=True)

    @report.command(name="send", description="Send a test report to the report channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_send(self, interaction: discord.Interaction, title: str, message: str):
        # Always DM the user a copy of the report.
        try:
            dm_embed = discord.Embed(title=f"Report: {title}", description=message, color=0xED4245)
            dm_embed.set_footer(text=f"Guild: {interaction.guild.name} • Reporter: {interaction.user}")
            await interaction.user.send(embed=dm_embed)
        except Exception:
            # DM can fail if the user has DMs disabled.
            pass

        # Also post to the configured report channel if one is set.
        ok, report_id = await send_report(
            bot=self.bot,
            guild_id=interaction.guild.id,
            title=title,
            description=message,
        )

        if ok:
            await interaction.response.send_message(
                f"✅ Sent (DM + channel). Report ID: `{report_id}`" if report_id else "✅ Sent (DM + channel).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "✅ Sent via DM. ⚠️ No report channel is set (use `/report channel-set`).",
                ephemeral=True,
            )

    @report.command(name="status", description="Show quick bot status")
    async def report_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ Online as **{self.bot.user}**\nGuilds: **{len(self.bot.guilds)}**",
            ephemeral=True,
        )

    @report.command(name="global", description="Report a critical issue directly to bot owners (cross-server)")
    @app_commands.describe(
        category="Category: harassment, nsfw, spam, impersonation, violence, content, other",
        severity="Severity: low, medium, high, critical",
        description="Describe the issue",
    )
    async def report_global(
        self,
        interaction: discord.Interaction,
        category: str,
        severity: str,
        description: str,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        category_lower = category.strip().lower()
        if category_lower not in REPORT_CATEGORIES:
            await interaction.response.send_message(
                f"⚠️ Invalid category. Use one of: {', '.join(REPORT_CATEGORIES.keys())}",
                ephemeral=True,
            )
            return

        severity_lower = severity.strip().lower()
        if severity_lower not in REPORT_SEVERITY:
            await interaction.response.send_message(
                f"⚠️ Invalid severity. Use one of: {', '.join(REPORT_SEVERITY.keys())}",
                ephemeral=True,
            )
            return

        # Build report content
        content = (
            f"**Reported by:** {interaction.user} (`{interaction.user.id}`)\n"
            f"**Guild:** {interaction.guild.name} (`{interaction.guild.id}`)\n\n"
            f"**Description:**\n{description.strip()}"
        )

        # Add evidence
        evidence = {}
        if interaction.channel:
            evidence["channel_id"] = int(interaction.channel.id)
        if interaction.guild:
            evidence["guild_id"] = int(interaction.guild.id)

        # Send global report
        sent = await send_global_report(
            bot=self.bot,
            guild_id=int(interaction.guild.id),
            title=f"Global Report: {REPORT_CATEGORIES[category_lower]}",
            description=content,
            category=category_lower,
            severity=severity_lower,
            reporter_id=int(interaction.user.id),
            evidence=evidence,
        )

        if sent > 0:
            await interaction.response.send_message(
                f"✅ Critical report sent to bot owners ({sent} notified). Thank you!",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ Failed to send global report. Please try again later.",
                ephemeral=True,
            )

    @report.command(name="content", description="Report inappropriate pack or character content")
    @app_commands.describe(
        content_id="Pack ID or Character ID to report",
        category="Category: harassment, nsfw, spam, impersonation, violence, content, other",
        severity="Severity: low, medium, high, critical",
        description="Why is this content inappropriate?",
        anonymous="Report anonymously? (yes/no, defaults to no)",
    )
    async def report_content(
        self,
        interaction: discord.Interaction,
        content_id: str,
        category: str,
        severity: str,
        description: str,
        anonymous: str = "no",
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        category_lower = category.strip().lower()
        if category_lower not in REPORT_CATEGORIES:
            await interaction.response.send_message(
                f"⚠️ Invalid category. Use one of: {', '.join(REPORT_CATEGORIES.keys())}",
                ephemeral=True,
            )
            return

        severity_lower = severity.strip().lower()
        if severity_lower not in REPORT_SEVERITY:
            await interaction.response.send_message(
                f"⚠️ Invalid severity. Use one of: {', '.join(REPORT_SEVERITY.keys())}",
                ephemeral=True,
            )
            return

        # Parse anonymous flag
        is_anonymous = str(anonymous or "no").strip().lower() in {"yes", "y", "true", "1"}

        # Build report content
        reporter_text = "🔒 Anonymous" if is_anonymous else f"{interaction.user} (`{interaction.user.id}`)"
        content = (
            f"**Content ID:** `{content_id}`\n"
            f"**Reported by:** {reporter_text}\n"
            f"**Guild:** {interaction.guild.name} (`{interaction.guild.id}`)\n\n"
            f"**Description:**\n{description.strip()}"
        )

        # Add evidence
        evidence = {}
        if interaction.channel:
            evidence["channel_id"] = int(interaction.channel.id)
        if interaction.guild:
            evidence["guild_id"] = int(interaction.guild.id)

        # Store report first
        from utils.reporting import store_report
        import uuid
        report_id = uuid.uuid4().hex[:16]
        await store_report(
            report_id=report_id,
            guild_id=int(interaction.guild.id),
            reporter_id=int(interaction.user.id),
            category=category_lower,
            severity=severity_lower,
            description=content,
            reported_content_id=content_id,
            evidence=evidence,
            anonymous=is_anonymous,
        )

        # Send to server channel
        ok, report_id_returned = await send_report(
            bot=self.bot,
            guild_id=int(interaction.guild.id),
            title=f"Content Report: {content_id}",
            description=content,
            category=category_lower,
            severity=severity_lower,
            reporter_id=int(interaction.user.id) if not is_anonymous else None,
            reported_content_id=content_id,
            evidence=evidence,
        )
        report_id = report_id_returned or report_id

        # Auto-disable if high/critical
        auto_disabled = False
        if severity_lower in {"high", "critical"}:
            try:
                from utils.packs_store import get_custom_pack, upsert_custom_pack
                from utils.character_registry import merge_pack_payload

                pack = await get_custom_pack(content_id)
                if pack:
                    pack["disabled_by_report"] = True
                    pack["disabled_reason"] = f"Auto-disabled due to {severity_lower} severity report"
                    await upsert_custom_pack(pack)
                    try:
                        merge_pack_payload(pack)
                    except Exception:
                        pass
                    auto_disabled = True
            except Exception:
                pass

        # Send global if critical
        global_sent = 0
        if severity_lower == "critical" or category_lower in CRITICAL_CATEGORIES:
            global_sent = await send_global_report(
                bot=self.bot,
                guild_id=int(interaction.guild.id),
                title=f"Content Report: {content_id}",
                description=content,
                category=category_lower,
                severity=severity_lower,
                reporter_id=int(interaction.user.id),
                reported_content_id=content_id,
                evidence=evidence,
            )

        # Response
        msg = f"✅ Content report sent. Report ID: `{report_id}`" if report_id else "✅ Content report sent."
        if auto_disabled:
            msg += f"\n⚠️ Content `{content_id}` has been auto-disabled pending review."
        if global_sent > 0:
            msg += f"\n🚨 Critical report escalated to bot owners ({global_sent} notified)."
        await interaction.response.send_message(msg, ephemeral=True)

    @report.command(name="list", description="List open reports (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(limit="How many reports to show (max 20)")
    async def report_list(self, interaction: discord.Interaction, limit: int = 10):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        reports = await get_open_reports(guild_id=int(interaction.guild.id), limit=min(limit, 20))
        if not reports:
            await interaction.response.send_message("No open reports.", ephemeral=True)
            return

        lines = []
        for r in reports:
            rid = str(r.get("report_id", ""))[:12]
            cat = r.get("category", "unknown")
            sev = r.get("severity", "unknown")
            reported_user = r.get("reported_user_id")
            reported_content = r.get("reported_content_id")
            created = int(r.get("created_at", 0) or 0)
            age_hours = (time.time() - created) / 3600.0 if created > 0 else 0.0

            line = f"• `{rid}` {cat} {sev}"
            if reported_user:
                line += f" user=`{reported_user}`"
            if reported_content:
                line += f" content=`{reported_content}`"
            line += f" age={age_hours:.1f}h"
            lines.append(line)

        msg = f"**Open Reports** ({len(reports)})\n" + "\n".join(lines)[:1900]
        await interaction.response.send_message(msg, ephemeral=True)

    @report.command(name="view", description="View a specific report (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(report_id="The report ID to view")
    async def report_view(self, interaction: discord.Interaction, report_id: str):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        report = await get_report(report_id.strip())
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return

        # Check if report belongs to this guild
        if int(report.get("guild_id", 0)) != int(interaction.guild.id):
            await interaction.response.send_message("This report belongs to a different server.", ephemeral=True)
            return

        import time
        created = int(report.get("created_at", 0) or 0)
        age_hours = (time.time() - created) / 3600.0 if created > 0 else 0.0

        lines = [
            f"**Report ID:** `{report_id}`",
            f"**Status:** {REPORT_STATUSES.get(report.get('status', 'open'), 'Unknown')}",
            f"**Category:** {REPORT_CATEGORIES.get(report.get('category', ''), 'Unknown')}",
            f"**Severity:** {report.get('severity', 'unknown')}",
            f"**Reporter:** <@{report.get('reporter_id', 0)}> (`{report.get('reporter_id', 0)}`)",
            f"**Age:** {age_hours:.1f} hours",
        ]

        if report.get("reported_user_id"):
            is_repeat, count, recent = await detect_repeat_offender(user_id=int(report.get("reported_user_id")))
            lines.append(f"**Reported User:** <@{report.get('reported_user_id')}> (`{report.get('reported_user_id')}`)")
            if is_repeat:
                lines.append(f"⚠️ **REPEAT OFFENDER** ({count} open reports)")

        if report.get("reported_content_id"):
            lines.append(f"**Content ID:** `{report.get('reported_content_id')}`")

        if report.get("notes"):
            lines.append(f"**Notes:** {report.get('notes')}")

        lines.append(f"\n**Description:**\n{report.get('description', 'N/A')[:500]}")

        msg = "\n".join(lines)[:1900]
        await interaction.response.send_message(msg, ephemeral=True)

    @report.command(name="status_update", description="Update report status (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        report_id="The report ID",
        status="New status: open, investigating, resolved, dismissed",
        notes="Optional notes",
    )
    async def report_status_update(
        self,
        interaction: discord.Interaction,
        report_id: str,
        status: str,
        notes: str = "",
    ):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        status_lower = status.strip().lower()
        if status_lower not in REPORT_STATUSES:
            await interaction.response.send_message(
                f"⚠️ Invalid status. Use one of: {', '.join(REPORT_STATUSES.keys())}",
                ephemeral=True,
            )
            return

        report = await get_report(report_id.strip())
        if not report:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return

        if int(report.get("guild_id", 0)) != int(interaction.guild.id):
            await interaction.response.send_message("This report belongs to a different server.", ephemeral=True)
            return

        ok = await update_report_status(
            report_id=report_id.strip(),
            status=status_lower,
            updated_by=int(interaction.user.id),
            notes=notes,
        )

        if ok:
            await interaction.response.send_message(
                f"✅ Report status updated to **{REPORT_STATUSES[status_lower]}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("⚠️ Failed to update report status.", ephemeral=True)

    @report.command(name="check_user", description="Check if a user is a repeat offender (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user_id="The user ID to check")
    async def report_check_user(self, interaction: discord.Interaction, user_id: str):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        try:
            uid = int(user_id.strip())
        except Exception:
            await interaction.response.send_message("Invalid user ID.", ephemeral=True)
            return

        is_repeat, count, reports = await detect_repeat_offender(user_id=uid)

        lines = [
            f"**User:** <@{uid}> (`{uid}`)",
            f"**Repeat Offender:** {'⚠️ YES' if is_repeat else '✅ No'}",
            f"**Open Reports:** {count}",
            f"**Total Reports:** {len(reports)}",
        ]

        if reports:
            lines.append("\n**Recent Reports:**")
            for r in reports[:5]:
                rid = str(r.get("report_id", ""))[:12]
                cat = r.get("category", "unknown")
                sev = r.get("severity", "unknown")
                status = r.get("status", "unknown")
                lines.append(f"• `{rid}` {cat} {sev} {status}")

        msg = "\n".join(lines)[:1900]
        await interaction.response.send_message(msg, ephemeral=True)

    @report.command(name="analytics", description="Report analytics dashboard (admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(days="How many days to analyze (max 30)")
    async def report_analytics(self, interaction: discord.Interaction, days: int = 7):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        from utils.reporting import (
            get_open_reports,
            get_reports_by_user,
            list_tickets_by_status,
            REPORT_STATUSES,
            REPORT_CATEGORIES,
            REPORT_SEVERITY,
        )
        from collections import Counter

        days_i = max(1, min(30, int(days or 7)))
        cutoff_time = int(time.time()) - (days_i * 86400)

        # Get all reports for this guild (we'll filter by time)
        all_reports = []
        for status in REPORT_STATUSES.keys():
            reports = await list_tickets_by_status(status=status, limit=1000)
            for r in reports:
                if int(r.get("guild_id", 0)) == int(interaction.guild.id):
                    created = int(r.get("created_at", 0) or 0)
                    if created >= cutoff_time:
                        all_reports.append(r)

        if not all_reports:
            await interaction.response.send_message(
                f"No reports found in the last {days_i} days.",
                ephemeral=True,
            )
            return

        # Analytics
        by_category = Counter()
        by_severity = Counter()
        by_status = Counter()
        total_reports = len(all_reports)
        open_count = 0
        resolved_count = 0
        dismissed_count = 0
        repeat_offenders = set()

        for r in all_reports:
            cat = r.get("category", "unknown")
            sev = r.get("severity", "unknown")
            status = r.get("status", "open")
            reported_user = r.get("reported_user_id")

            by_category[cat] += 1
            by_severity[sev] += 1
            by_status[status] += 1

            if status == "open":
                open_count += 1
            elif status == "resolved":
                resolved_count += 1
            elif status == "dismissed":
                dismissed_count += 1

            # Check for repeat offenders
            if reported_user:
                user_reports = await get_reports_by_user(int(reported_user), limit=100)
                open_user_reports = [ur for ur in user_reports if ur.get("status") == "open"]
                if len(open_user_reports) >= 3:
                    repeat_offenders.add(int(reported_user))

        # Build dashboard
        lines = [
            f"📊 **Report Analytics Dashboard** (last {days_i} days)",
            f"",
            f"**Summary**",
            f"• Total Reports: **{total_reports}**",
            f"• Open: **{open_count}**",
            f"• Resolved: **{resolved_count}**",
            f"• Dismissed: **{dismissed_count}**",
            f"• Repeat Offenders: **{len(repeat_offenders)}**",
            f"",
            f"**By Category**",
        ]

        for cat, count in by_category.most_common():
            cat_name = REPORT_CATEGORIES.get(cat, cat.title())
            lines.append(f"• {cat_name}: **{count}**")

        lines.append(f"\n**By Severity**")
        for sev, count in by_severity.most_common():
            sev_info = REPORT_SEVERITY.get(sev, {})
            sev_label = sev_info.get("label", sev.title())
            emoji = sev_info.get("emoji", "")
            lines.append(f"• {emoji} {sev_label}: **{count}**")

        if repeat_offenders:
            lines.append(f"\n**Repeat Offenders** (3+ open reports)")
            for uid in list(repeat_offenders)[:5]:
                lines.append(f"• <@{uid}> (`{uid}`)")

        msg = "\n".join(lines)[:1900]
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SlashReport(bot))
