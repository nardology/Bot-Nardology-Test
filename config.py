import logging
import os
import sys

_config_log = logging.getLogger("config")


def _as_bool(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "y", "on"}

# ---- Discord ----
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")

# ---- OpenAI ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
OPENAI_MODEL_FREE = os.getenv("OPENAI_MODEL_FREE", "gpt-4.1-nano").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
# Hard default timeout: keep requests from hanging shards.
# You can override via env OPENAI_TIMEOUT_S.
OPENAI_TIMEOUT_S = float(os.getenv("OPENAI_TIMEOUT_S", "20").strip() or "20")

# ---- Environment ----
ENVIRONMENT = os.getenv("ENVIRONMENT", "prod").strip().lower()
BOT_NAME = os.getenv("BOT_NAME", "Bot-Nardology")

# ---- Legal / public links ----
TERMS_OF_SERVICE_URL = "https://neon-cranachan-dfa64a.netlify.app/"
PRIVACY_POLICY_URL = "https://sage-malabi-770900.netlify.app/"
SUPPORT_SERVER_URL = "https://discord.gg/F4TNTDvHP9"

# ---- Emergency kill switch ----
# If true, ALL AI calls are disabled immediately (even if Redis/DB are unhealthy).
# Useful for incident response / cost containment.
AI_DISABLED = _as_bool("AI_DISABLED", "false")

# ---- Revenue-linked cost caps (Phase 4) ----
# Daily AI spend cap per guild in cents. Prevents any single guild from becoming
# unprofitable. Free default: 5 cents/day (~$1.50/month). Pro default: 50 cents/day
# (~$15/month vs $4.99 revenue = generous margin). Set to 0 to disable.
AI_COST_CAP_FREE_DAILY_CENTS: float = float(os.getenv("AI_COST_CAP_FREE_DAILY_CENTS", "5").strip() or "5")
AI_COST_CAP_PRO_DAILY_CENTS: float = float(os.getenv("AI_COST_CAP_PRO_DAILY_CENTS", "50").strip() or "50")

# ---- Guild sync ----
def _parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).split(","):
        p = part.strip()
        if not p:
            continue
        if p.isdigit():
            out.append(int(p))
    # stable de-dupe
    seen: set[int] = set()
    uniq: list[int] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    return uniq


_DEV_GUILD_RAW = os.getenv("DEV_GUILD_ID")
DEV_GUILD_IDS = _parse_id_list(_DEV_GUILD_RAW)
DEV_GUILD_ID = DEV_GUILD_IDS[0] if DEV_GUILD_IDS else None  # backwards-compat

_SYNC_RAW = os.getenv("SYNC_GUILD_ID")
SYNC_GUILD_IDS = _parse_id_list(_SYNC_RAW)
SYNC_GUILD_ID = SYNC_GUILD_IDS[0] if SYNC_GUILD_IDS else None  # backwards-compat

# ---- Owners ----
BOT_OWNER_IDS = {
    int(x.strip())
    for x in (os.getenv("BOT_OWNER_IDS") or "").split(",")
    if x.strip().isdigit()
}

# ---- Custom pack limits (for monetization) ----
# These apply to non-bot-owners. Bot owners (BOT_OWNER_IDS) are unlimited.
# Phase 5: Pro guilds get unlimited packs and higher total character cap.
MAX_CUSTOM_PACKS_PER_GUILD = int(os.getenv("MAX_CUSTOM_PACKS_PER_GUILD", "3") or "3")
MAX_CUSTOM_CHARS_PER_PACK = int(os.getenv("MAX_CUSTOM_CHARS_PER_PACK", "25") or "25")
MAX_CUSTOM_CHARS_TOTAL_PER_GUILD = int(os.getenv("MAX_CUSTOM_CHARS_TOTAL_PER_GUILD", "100") or "100")
MAX_CUSTOM_CHARS_TOTAL_PER_GUILD_PRO = int(os.getenv("MAX_CUSTOM_CHARS_TOTAL_PER_GUILD_PRO", "250") or "250")

# ---- Stripe (payments & subscriptions) ----
STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip() or None
STRIPE_WEBHOOK_SECRET = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip() or None

# Price IDs (create these in the Stripe Dashboard -> Products)
STRIPE_PRICE_PRO_MONTHLY = (os.getenv("STRIPE_PRICE_PRO_MONTHLY") or "").strip() or None

# Points bundle price IDs (100 points per $1 USD)
STRIPE_PRICE_POINTS_500 = (os.getenv("STRIPE_PRICE_POINTS_500") or "").strip() or None
STRIPE_PRICE_POINTS_1000 = (os.getenv("STRIPE_PRICE_POINTS_1000") or "").strip() or None
STRIPE_PRICE_POINTS_2500 = (os.getenv("STRIPE_PRICE_POINTS_2500") or "").strip() or None
STRIPE_PRICE_POINTS_5000 = (os.getenv("STRIPE_PRICE_POINTS_5000") or "").strip() or None
STRIPE_PRICE_POINTS_10000 = (os.getenv("STRIPE_PRICE_POINTS_10000") or "").strip() or None

# Map price_id -> points amount (built at import time from the env vars above)
STRIPE_POINTS_BUNDLES: dict[str, int] = {}
for _price_id, _pts in [
    (STRIPE_PRICE_POINTS_500, 500),
    (STRIPE_PRICE_POINTS_1000, 1_000),
    (STRIPE_PRICE_POINTS_2500, 2_500),
    (STRIPE_PRICE_POINTS_5000, 5_000),
    (STRIPE_PRICE_POINTS_10000, 10_000),
]:
    if _price_id:
        STRIPE_POINTS_BUNDLES[_price_id] = _pts

# Gift premium price in cents (default: $4.99 = 499 cents per month)
STRIPE_GIFT_UNIT_AMOUNT_CENTS: int = int(os.getenv("STRIPE_GIFT_UNIT_AMOUNT_CENTS", "499"))

# Redirect URLs after Stripe Checkout (your Discord invite link works fine)
STRIPE_SUCCESS_URL = (os.getenv("STRIPE_SUCCESS_URL") or "https://discord.com").strip()
STRIPE_CANCEL_URL = (os.getenv("STRIPE_CANCEL_URL") or "https://discord.com").strip()

# ---- Payments feature flag ----
# Set to "true" to enable Stripe-powered commands (subscribe, gift, buy_points).
# Default is false so the bot can launch without any payment infrastructure.
PAYMENTS_ENABLED = _as_bool("PAYMENTS_ENABLED", "false")

# ---- One-time maintenance flags ----
CLEANUP_DEV_COMMANDS_ONCE = _as_bool("CLEANUP_DEV_COMMANDS_ONCE", "false")
CLEAR_GLOBAL_COMMANDS_ONCE = _as_bool("CLEAR_GLOBAL_COMMANDS_ONCE", "false")
CLEAR_GUILD_COMMANDS_ONCE = _as_bool("CLEAR_GUILD_COMMANDS_ONCE", "false")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
    """Check for required and recommended environment variables.

    Called at import time. In production, missing critical vars cause a hard
    exit so the problem is obvious (instead of a cryptic error 5 minutes later).
    """
    is_prod = ENVIRONMENT != "dev"
    errors: list[str] = []
    warnings: list[str] = []

    # Required always
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN (or TOKEN) is not set. The bot cannot start.")

    # Required in production
    if is_prod:
        if not os.getenv("DATABASE_URL"):
            errors.append("DATABASE_URL is not set. Postgres is required in production.")
        if PAYMENTS_ENABLED and not STRIPE_WEBHOOK_SECRET:
            warnings.append(
                "STRIPE_WEBHOOK_SECRET is not set. Stripe webhooks will be rejected "
                "in production. Set this to the signing secret from your Stripe dashboard."
            )

    # Recommended (warn only)
    if not OPENAI_API_KEY:
        warnings.append("OPENAI_API_KEY is not set. AI features (/talk) will not work.")
    if PAYMENTS_ENABLED and not STRIPE_SECRET_KEY:
        warnings.append("STRIPE_SECRET_KEY is not set. Stripe payments will be disabled.")
    if not BOT_OWNER_IDS:
        warnings.append(
            "BOT_OWNER_IDS is not set. Owner commands (/z_owner, /z_server) will "
            "be inaccessible. Set to a comma-separated list of Discord user IDs."
        )

    for w in warnings:
        _config_log.warning("CONFIG WARNING: %s", w)

    if errors:
        for e in errors:
            _config_log.critical("CONFIG ERROR: %s", e)
        if is_prod:
            sys.exit(1)


validate_config()
