# utils/metrics.py
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("bot.metrics")

# Phase 3: AI cost estimation (tokens → USD). Override via env.
DEFAULT_PRICE_PER_1K_TOKENS = 0.002  # GPT-4o-mini approx


@dataclass
class MetricEvent:
    command: str
    guild_id: int | None
    user_id: int | None
    ok: bool
    latency_ms: int
    input_chars: int = 0
    output_chars: int = 0
    model: str | None = None
    est_cost_usd: float = 0.0
    error_type: str | None = None


# Super rough fallback cost estimate if you don't have token counts.
# You can tighten later by switching generate_text to return usage tokens.
def estimate_cost_usd(*, input_chars: int, output_chars: int, price_per_1k_tokens: float = 0.002) -> float:
    # ~4 chars/token heuristic
    in_tokens = input_chars / 4.0
    out_tokens = output_chars / 4.0
    tokens = in_tokens + out_tokens
    return (tokens / 1000.0) * price_per_1k_tokens


class MetricsTimer:
    def __init__(self, command: str, guild_id: int | None, user_id: int | None, input_chars: int = 0, model: str | None = None):
        self.command = command
        self.guild_id = guild_id
        self.user_id = user_id
        self.input_chars = int(input_chars or 0)
        self.model = model
        self._t0 = time.perf_counter()

    def finish(self, *, ok: bool, output_chars: int = 0, error_type: str | None = None) -> MetricEvent:
        ms = int((time.perf_counter() - self._t0) * 1000)
        outc = int(output_chars or 0)
        cost = estimate_cost_usd(input_chars=self.input_chars, output_chars=outc)
        return MetricEvent(
            command=self.command,
            guild_id=self.guild_id,
            user_id=self.user_id,
            ok=bool(ok),
            latency_ms=ms,
            input_chars=self.input_chars,
            output_chars=outc,
            model=self.model,
            est_cost_usd=cost,
            error_type=error_type,
        )


def emit(ev: MetricEvent) -> None:
    logger.info(
        "METRIC cmd=%s ok=%s ms=%s guild=%s user=%s in=%s out=%s cost=%.6f err=%s model=%s",
        ev.command,
        ev.ok,
        ev.latency_ms,
        ev.guild_id,
        ev.user_id,
        ev.input_chars,
        ev.output_chars,
        ev.est_cost_usd,
        ev.error_type or "",
        ev.model or "",
    )

    try:
        from utils.prom import commands_total, command_latency, ai_cost_usd
        status = "ok" if ev.ok else "error"
        commands_total.labels(command=ev.command, status=status).inc()
        command_latency.labels(command=ev.command).observe(ev.latency_ms / 1000.0)
        if ev.est_cost_usd > 0:
            ai_cost_usd.inc(ev.est_cost_usd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 3: Dashboard query helpers (retention, economy, AI cost, churn)
# ---------------------------------------------------------------------------


def _price_per_1k_tokens() -> float:
    try:
        return float(os.getenv("AI_COST_PER_1K_TOKENS", str(DEFAULT_PRICE_PER_1K_TOKENS)))
    except Exception:
        return DEFAULT_PRICE_PER_1K_TOKENS


def estimate_ai_cost_usd_from_tokens(tokens: int) -> float:
    """Estimate USD cost from token count. Uses env AI_COST_PER_1K_TOKENS."""
    price = _price_per_1k_tokens()
    return (int(tokens or 0) / 1000.0) * price
