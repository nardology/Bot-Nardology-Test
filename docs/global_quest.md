# Global monthly quest (community training)

## Overview

- **Training points** are earned when a user uses `/talk` with their **selected** character (same rules as bond XP: owned character, not base `fun`/`serious` defaults).
- Formula: `max(1, round((1 + bond_xp_gained) * character_multiplier))` per qualifying message.
- **Scope**: **global** (one progress bar for all servers) or **guild** (only that `guild_id` contributes to the bar).
- Only **one** event receives training per `/talk`: **guild-scoped** events take priority over **global**.

## URLs

- Public status: `{BASE_URL}/global-quest` (optional `?guild_id=` for API context).
- Owner editor: `{BASE_URL}/global-quest/edit?token=...` (same admin token as `/z_owner admin_link`).
- Discord: `/z_owner global_quest_link` — DM with editor + public links.

## Resolution

- **Success**: community training ≥ target → `reward_points` to every user who contributed; optional **badge** for contributors (`user_profile_badges`, shown on `/inspect` as “Event badges”).
- **Failure**: end time passes without reaching target → `failure_points` to contributors (often negative).
- A background tick (every 5 minutes) resolves time-based outcomes even if nobody talks at the deadline.

## Database

Run Alembic migration `0018_global_quest_system`.
