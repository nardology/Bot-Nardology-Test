# Data lifetimes (Phase 1)

This bot uses a **hot / cold** split:

- **Redis = hot/ephemeral** (fast, cheap, can be lost without breaking core ownership data)
- **Postgres = cold/durable** (source-of-truth for long-lived user/guild state)

## Redis (ephemeral)

> If Redis is missing/unavailable, the bot degrades gracefully (settings/counters may not persist, but commands should not crash).

| Purpose | Key prefix / examples | TTL / lifetime | Recoverable? |
|---|---|---:|---|
| AI concurrency slots | `ai:conc:global`, `ai:conc:guild:{guild_id}`, `ai:lease:{lease_id}` | Lease keys expire via `AI_LEASE_TTL_S` (default ~70s). Concurrency counters are also given the same TTL to avoid sticking after crashes. | Yes (self-heals by TTL) |
| /talk usage counters (budgets) | `talk:count:guild:{guild_id}:{YYYYMMDD}`; `talk:count:user:{guild_id}:{user_id}:{YYYYMMDD}` | ~8 days (enables rolling 7-day + daily checks) | Yes (only affects rate limiting) |
| /scene turn usage counters (budgets) | `scene:turns:guild:{guild_id}:{YYYYMMDD}`; `scene:turns:user:{guild_id}:{user_id}:{YYYYMMDD}` | ~8 days | Yes |
| Guild settings (fast reads) | `guild:{guild_id}:settings` (Redis hash of JSON) | No TTL by default | Mostly (some are mirrored in Postgres) |
| Guild list settings (allowlists, etc.) | `guild:{guild_id}:list:{key}` (Redis set) | No TTL by default | Depends on feature |
| Short-term talk memory | (managed by `utils.talk_memory`) | TTL via `MEMORY_TTL_SECONDS` | Yes |
| Rate limiters / cooldowns | (managed by `utils.ai_limits`, `utils.say_limits`) | Window-based | Yes |
| Penalties | (managed by `utils/AI_penalties.py`) | Until timestamp / TTL-based | Yes |

## Postgres (durable)

> Postgres is the source-of-truth for long-lived state.

| Table / model | What it stores | Lifetime |
|---|---|---|
| `PremiumEntitlement` | guild tier (`free`/`pro`) | Durable |
| `GuildSetting` | long-term guild settings (if/when migrated) | Durable |
| `CharacterUserState` | per-user active character / prefs | Durable |
| `CharacterOwnedStyle` | owned characters / collection | Durable |
| `CharacterCustomStyle` | custom character definitions | Durable |
| `BondState` | bond XP + nickname per character | Durable |
| `VoiceSound` | voice assets metadata | Durable |
| `AnalyticsDailyMetric` | daily aggregated analytics | Durable |
| `UserFirstSeen` | first-seen timestamps for cohorting | Durable |

## Emergency controls

- `AI_DISABLED=true` will immediately disable all AI calls (kill switch).
