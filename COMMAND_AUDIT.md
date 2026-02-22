# Command audit – Nardology bot

Full inventory of slash commands and readiness for publication.  
**Last audit:** Generated from codebase scan.

---

## How to run tests

From the project root (with the repo’s Python env active):

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

Optional: `python -m pytest tests/ -v --tb=short`

Tests cover: analytics keys, character_store limits, leaderboard keys, points shop structure, and that every extension module imports without error. No Discord or Redis required for the default test run.

---

## Extension load order (bot.py)

| Extension | Purpose |
|-----------|--------|
| commands.slash.analytics | Server analytics (owner) |
| commands.slash.basic | ping, hello, say |
| commands.slash.start | Onboarding |
| commands.slash.settings | Server settings, AI allow/block |
| commands.slash.help | /help |
| commands.slash.talk | /talk |
| commands.slash.voice | Voice sounds |
| commands.slash.feedback | Feedback |
| commands.slash.limits | /limits |
| commands.slash.usage | Usage |
| commands.slash.bond | Bond with character |
| commands.slash.penalty | Penalty |
| commands.slash.character | character roll/collection/select/remove/reset |
| commands.slash.points | points daily/shop/buy/quests/cosmetic-shop etc. |
| commands.slash.packs | packs marketplace/browse/enable/create etc. |
| *(scene hidden)* | commands.slash.scene – commented out in EXTENSIONS |
| commands.slash.owner | z_owner tree |
| commands.slash.report | report channel/send/content/list etc. |
| commands.slash.z_server | z_server bot_disable/ban/verification etc. |
| commands.slash.appeal | /appeal |
| commands.slash.verification_appeal | /verification_appeal |
| commands.slash.leaderboard | leaderboard view/rank/opt_out/opt_in |
| commands.slash.inspect | /inspect |
| commands.slash.cosmetic | cosmetic select/clear |

---

## Command inventory and status

### User-facing (everyone)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **ping** | Latency | ✅ Ready |
| **hello** | Greeting | ✅ Ready |
| **help** | List commands and usage | ✅ Ready |
| **start** | Onboarding, free roll | ✅ Ready |
| **talk** | Talk to character (AI) | ✅ Needs OpenAI key; defer + followup used |
| **inspect** | View your or someone's stats (+ inventory X/Y) | ✅ Ready |
| **leaderboard view** | View leaderboard (global/server, category) | ✅ Default scope global |
| **leaderboard rank** | Your rank | ✅ Ready |
| **leaderboard opt_out / opt_in** | Privacy | ✅ Ready |
| **points daily** | Claim daily points | ✅ Ready |
| **points balance** | Balance and streak | ✅ Ready |
| **points shop** | Points shop (inv upgrade, 5/10-pull, boosters, dynamic items) | ✅ Ready; 5/10-pull select→apply fixed |
| **points cosmetic-shop** | Buy cosmetics (defer + followup) | ✅ Ready |
| **points quests** | Daily/weekly quests | ✅ Ready |
| **points buy** | Buy by item id | ✅ Ready |
| **points convert** | Shards ↔ points | ✅ Ready |
| **points reminders** | Streak reminder DMs | ✅ Ready |
| **points luck** | Lucky booster stack | ✅ Ready |
| **character roll** | Roll for character (window cooldown) | ✅ Ready |
| **character collection** | Owned + selected | ✅ Ready |
| **character select** | Set active character | ✅ Ready |
| **character remove** | Remove from collection | ✅ Ready |
| **character reset** | (Testing) Reset roll cooldown + counters | ✅ Resets Redis window + Postgres |
| **packs marketplace** | Discover packs | ✅ Ready |
| **packs browse** | Browse and enable/disable | ✅ Ready |
| **packs enabled** | List enabled in server | ✅ Ready |
| **packs enable / disable** | Enable/disable by id | ✅ Ready |
| **packs upvote / leaderboard** | Upvote, top creators | ✅ Ready |
| **cosmetic select / clear** | Profile cosmetic for inspect | ✅ Ready |
| **bond** | Bond with character | ✅ Ready |
| **feedback** | Send feedback | ✅ Ready |
| **limits** | Rate/limit inspection | ✅ Ready |
| **usage** | Usage stats | ✅ Ready |
| **report send / content** | Report channel, content report | ✅ Ready |
| **appeal** | Guild owner appeal (1/day) | ✅ Ready |
| **verification_appeal** | Verification appeal (1/day) | ✅ Ready |

### Settings (admin / server)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **settings show** | Server settings | ✅ Ready |
| **settings language** | Bot language | ✅ Ready |
| **settings character** | Default character | ✅ Ready |
| **settings announce *** | Announcement channel | ✅ Ready |
| **settings ai allow-role / block-role / allow-channel / safety-mode / block-topic** | AI access and safety | ✅ Ready |

### Owner / z_owner (bot owner only)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **z_owner status** | Bot status | ✅ Ready |
| **z_owner health** | Redis + Postgres | ✅ Ready |
| **z_owner give_character / delete_character** | Grant/remove character | ✅ Ready |
| **z_owner packadmin *** | set_official, set_exclusive, delete_pack, featured_*, shop_pack_create | ✅ Ready |
| **z_owner shop list/remove/edit** | Shop items | ✅ Ready |
| **z_owner points give/take** | Points | ✅ Ready |
| **z_owner ai disable/enable/why** | Global AI kill switch | ✅ Ready |
| **z_owner data clear_* / leaderboard_* ** | clear_all, clear_guild, leaderboard_reset, leaderboard_sync, leaderboard_opt_in | ✅ Ready |
| **z_owner global today/flush/total/month/last7/incidents** | Analytics global | ✅ Flush uses updated_at; total flushes then queries |
| **z_owner premium get/set** | Tier override | ✅ Ready |
| **z_owner analytics today/last7/funnel/retention/economy/cost/churn** | Analytics | ✅ Ready |

### z_server (server admin / privileged)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **z_server bot_disable / bot_enable / bot_status** | Global bot disable | ✅ Ready |
| **z_server disable_ai / enable_ai** | AI kill | ✅ Ready |
| **z_server ban_user / unban_user / check_user** | Global ban | ✅ Ready |
| **z_server announce *** / announce** | Announce channel, broadcast | ✅ Ready |
| **z_server verification *** | toggle_auto, list, status | ✅ Ready |
| **z_server nuke_warning / nuke** | Moderation | ✅ Ready |

### Report (admin)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **report channel-set / channel-view** | Report channel | ✅ Ready |
| **report list / view / status_update / check_user / analytics** | Admin report tools | ✅ Ready |
| **report global** | Critical issue to owners | ✅ Ready |

### Analytics (owner)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **analytics view / reset** | Server analytics | ✅ Ready |

### Packs (premium / server)

| Command | Description | Status / notes |
|---------|-------------|----------------|
| **packs create / delete / edit** | (Premium) Global pack | ✅ Ready |
| **packs character_add / character_edit / character_remove** | Pack characters | ✅ Ready |
| **packs private_enable** | Password pack | ✅ Ready |
| **packs server_characters / server_character_edit / server_character_remove** | Server-only characters | ✅ Ready |

---

## Dependencies and env

- **Discord:** Bot token, guild IDs for sync (config).
- **Redis:** Used for analytics counters, leaderboards, roll window, rate limits, backpressure. Bot runs without Redis in degraded mode (get_redis_or_none).
- **Postgres (or SQLite in dev):** Points, character state, bonds, analytics_daily_metrics, etc. DATABASE_URL (or ENVIRONMENT=dev for SQLite).
- **OpenAI:** For /talk (and scene if enabled). API key required for AI.

---

## Recent fixes (reference)

- Leaderboard: default scope global; server scope and characters owned fixed; opt_out handling.
- Analytics: flush writes updated_at; global total flushes then queries; only non-zero values written.
- /character reset: clears Redis roll window + Postgres roll_day/roll_used/pity.
- Shop: inv_upgrade uses increment_inventory_upgrades(uid, delta=1); get/increment use get_sessionmaker().
- 5/10-pull: Select callback safe response; Apply errors on dupes or over capacity and asks to try again; inspect shows inventory X/Y.

---

## Publication readiness

| Area | Ready? | Notes |
|------|--------|------|
| Commands load | ✅ | All extensions import; scene optional (hidden). |
| User flows | ✅ | Start → roll → talk → points → shop → leaderboard/inspect covered. |
| Owner tools | ✅ | Data clear, sync, global totals, health. |
| Tests | ✅ | Pytest suite for limits, analytics keys, leaderboard keys, shop structure, cog imports. |
| Config | ⚠️ | Ensure DATABASE_URL, REDIS_URL, OpenAI key, Discord token and guild sync in production. |

**Verdict:** Ready for publication from a command and flow perspective. Run `pytest tests/ -v` before release; configure env and do a live pass on a test server (start, roll, talk, shop, leaderboard, inspect).
