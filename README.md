# Bot-Nardology (Test)

This repo is the **product-hardening** version of Bot-Nardology.

## Phase 1: Internal Strength

Core loop (required): **Roll → Talk → Bond → Collect → Cooldown**  
See `docs/core_loop.md`.

### AI architecture

- **All AI calls go through** `core/ai_gateway.py` (do not import OpenAI clients from feature modules).
- Gateway responsibilities:
  - Redis-backed concurrency gating (with safe degraded fallback if Redis is unavailable)
  - Circuit breaker
  - Tier+mode token clamping
  - **Daily + weekly budgets** (talk/scene) via `core/ai_usage.py`
  - Friendly error mapping

### Storage

- Redis = hot/ephemeral (cooldowns, limits, temporary memory, active sessions)
- Postgres = cold/durable (ownership, bonds, analytics, payments, audit)

See `docs/data_lifetimes.md`.

### Emergency kill switch

Set `AI_DISABLED=true` to instantly disable all AI calls.

## Testing

Optional test suite (no Discord or Redis required for most tests):

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

See `COMMAND_AUDIT.md` for a full command inventory and publication checklist.

## Next feature checklist

- Stripe webhook + entitlement syncing
- /voice add persistence
- Owner/admin analytics dashboard
