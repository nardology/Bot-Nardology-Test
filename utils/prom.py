"""Prometheus metric definitions.

All metric objects are created at import time so any module can increment them.
The /metrics HTTP endpoint (registered in core/stripe_webhook.py) calls
``prometheus_client.generate_latest()`` to render current values.

If ``prometheus_client`` is not installed the module exposes no-op stubs so
the rest of the bot keeps working.
"""
from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram, Gauge

    commands_total = Counter(
        "bot_commands_total",
        "Total commands processed",
        ["command", "status"],
    )

    command_latency = Histogram(
        "bot_command_latency_seconds",
        "Command latency in seconds",
        ["command"],
        buckets=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
    )

    stripe_events_total = Counter(
        "bot_stripe_events_total",
        "Stripe webhook events received",
        ["event_type"],
    )

    ai_cost_usd = Counter(
        "bot_ai_cost_usd_total",
        "Estimated cumulative AI cost in USD",
    )

    active_guilds = Gauge(
        "bot_active_guilds",
        "Number of guilds the bot is currently in",
    )

    PROMETHEUS_AVAILABLE = True

except ImportError:
    # Provide no-op stubs so code that calls .inc() / .set() won't crash.
    class _Noop:
        def labels(self, **_kw):
            return self
        def inc(self, _amount=1):
            pass
        def observe(self, _value):
            pass
        def set(self, _value):
            pass

    commands_total = _Noop()  # type: ignore[assignment]
    command_latency = _Noop()  # type: ignore[assignment]
    stripe_events_total = _Noop()  # type: ignore[assignment]
    ai_cost_usd = _Noop()  # type: ignore[assignment]
    active_guilds = _Noop()  # type: ignore[assignment]

    PROMETHEUS_AVAILABLE = False
