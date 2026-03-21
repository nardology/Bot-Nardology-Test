# Connection traits: cost and tuning

This document estimates **extra AI cost**, **storage**, and **knobs** for the connection traits system.

## Tunables (environment / config)

| Knob | Default | Effect |
|------|---------|--------|
| `CONNECTION_CONTEXT_MAX_CHARS` | `3500` | Max characters injected into `/talk` from connection profile (see `config.py`). |
| `utils/connection_traits_catalog.py` | — | **Shop source of truth** (enable/disable traits, prices). |
| `OPENAI_MODEL_FREE` | `gpt-4.1-nano` | Used for lightweight emotion classification when keywords don’t match. |
| `COSMETIC_SHARD_PRICE` | `50` | Shard price for `/points cosmetic-buy-shard`. |
| `BASE_URL` | — | Required for OAuth redirect and dashboard links. |
| `CONNECTION_OAUTH_CLIENT_ID` / `SECRET` | falls back to `DISCORD_*` | Discord OAuth for `/connection` web UI. |

## Per-feature cost (approximate)

| Feature | Extra prompt tokens (typical) | Extra AI calls |
|---------|----------------------------------|----------------|
| Remember name / hobbies / speech style / weekly+ daily text | Only injected text (bounded by max chars) | **0** |
| **Emotion adapt** | +1 short line (“Detected user emotional tone: …”) | **0–1** small call if keywords don’t match (`max_output_tokens≈8`) |
| Semi-permanent / permanent memory | Same injection; longer history only if you store more in `payload` | **0** (MVP) |

Optional Phase 2 (not implemented by default): monthly **summarization** job for permanent tier → **+1** modest call per user per month.

## Storage (Postgres)

- Table `character_connection_profiles`: JSON text for `purchased_traits_json` + `payload_json`.
- Worst case: a few KB per (user, character) if users fill weekly/daily fields to limits.
- Retention: raw JSON until user edits; roll weekly `week_id` in payload for Sunday flow.

## Shards vs points

- Duplicate shard payouts: `utils/shard_economy.py` (`DUPLICATE_SHARDS_BY_RARITY`).
- `/points convert`: **10 points = 1 shard** parity before **10% fee on output** (`utils/shard_economy.py`).

## Operations

- Background loop: `utils/connection_traits_loop.py` (every ~15 minutes) — Sunday weekly DM + random check-in DMs (rate-limited with Redis when available).
