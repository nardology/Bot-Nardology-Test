# Schema change safety (audit checklist)

When changing ORM models or database schema, follow this checklist so existing commands (points, streak, daily, quests, etc.) keep working in production.

## Adding new columns to production tables

1. **Add an Alembic migration** that introduces the column (same type and default as in the model).
2. **Update the corresponding `_ensure_*` helper** in [utils/db.py](../utils/db.py) if that table has one:
   - `points_wallet` → `_ensure_points_wallet_columns` (keep in sync with migrations 0013, 0014, and any future points_wallet migrations)
   - `character_user_state` → `_ensure_character_user_state_columns`
   - Stripe/premium tables → `_ensure_stripe_columns`
3. Use `ADD COLUMN IF NOT EXISTS` with the same type and default so deployments that skip migrations still get the column on next startup.

## Before changing a model or its migrations

- **Grep for usages**: Search for the model name and table name (e.g. `PointsWallet`, `points_wallet`) to see all call sites. Ensure no code assumes the old schema.
- **Critical tables**: `PointsWallet` is used by points_store (daily, streak, balance), quests (apply_quest_event), stats (get_user_stats), dashboard_queries, and slash commands (points, owner). Changing its columns without updating both migrations and `_ensure_points_wallet_columns` will cause `UndefinedColumnError` in production if migrations were not run.

## Deploy

- Run `alembic upgrade head` (or equivalent) in production when new migrations exist. The `_ensure_*` logic is a safety net for environments where migrations are not run; it is not a replacement for proper migration-based deploys.
