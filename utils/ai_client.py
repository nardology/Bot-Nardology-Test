# utils/ai_client.py
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import config


# ----------------------------
# Stable exception types (yours)
# ----------------------------
class AIError(RuntimeError):
    """Base class for AI errors (stable for your codebase)."""


class AIConfigError(AIError):
    """Missing/invalid configuration (e.g., no API key)."""


class AITimeoutError(AIError):
    """Request exceeded timeout."""


class AIConnectionError(AIError):
    """Network/DNS/TLS/connection issues reaching the API."""


class AIRateLimitError(AIError):
    """429 rate limit / quota / throttling."""


class AIAuthError(AIError):
    """401/403 auth problems."""


class AIStatusError(AIError):
    """Non-OK response that isn't auth/rate-limit."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"AI request failed (status {status_code})")
        self.status_code = int(status_code)


@dataclass(frozen=True)
class AIResult:
    text: str
    raw: dict[str, Any]
    # Best-effort usage extracted from Responses API.
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


# ----------------------------
# Config helpers
# ----------------------------
def _require_key() -> str:
    key = (getattr(config, "OPENAI_API_KEY", None) or "").strip()
    if not key:
        raise AIConfigError("OPENAI_API_KEY is missing")
    return key


def _base_url() -> str:
    return (getattr(config, "OPENAI_BASE_URL", None) or "https://api.openai.com/v1").rstrip("/")


def _endpoint() -> str:
    return _base_url() + "/responses"


def _model() -> str:
    return (getattr(config, "OPENAI_MODEL", None) or "gpt-4.1-mini").strip()


def _default_timeout_s() -> float:
    try:
        return float(getattr(config, "OPENAI_TIMEOUT_S", 40.0) or 40.0)
    except Exception:
        return 40.0


# ----------------------------
# Response parsing
# ----------------------------
def _extract_text(resp_json: dict[str, Any]) -> str:
    """
    Extract output text from Responses API JSON.
    Handles common shapes; safe fallback.
    """
    out = resp_json.get("output") or []
    parts: list[str] = []

    for item in out:
        content = item.get("content") or []
        for c in content:
            ctype = c.get("type")

            # Standard Responses API shape:
            # {"type":"output_text","text":"..."}
            if ctype == "output_text" and c.get("text"):
                parts.append(str(c["text"]))
                continue

            # Some adapters use "text"
            if ctype == "text":
                if isinstance(c.get("text"), str) and c["text"].strip():
                    parts.append(str(c["text"]))
                    continue

                # sometimes: {"text": {"value": "..."}}
                t = c.get("text")
                if isinstance(t, dict) and isinstance(t.get("value"), str) and t["value"].strip():
                    parts.append(str(t["value"]))
                    continue

    if parts:
        return "".join(parts).strip()

    # fallback shapes
    if isinstance(resp_json.get("output_text"), str):
        return str(resp_json["output_text"]).strip()

    # sometimes: {"response": "..."}
    if isinstance(resp_json.get("response"), str):
        return str(resp_json["response"]).strip()

    return ""


def _extract_usage_tokens(resp_json: dict[str, Any]) -> tuple[int, int, int]:
    """Extract (input_tokens, output_tokens, total_tokens) from Responses API JSON.

    The Responses API typically returns:
      {"usage": {"input_tokens": X, "output_tokens": Y, "total_tokens": Z}}

    This function is best-effort and returns zeros if unavailable.
    """
    try:
        usage = resp_json.get("usage")
        if not isinstance(usage, dict):
            return (0, 0, 0)
        it = int(usage.get("input_tokens") or 0)
        ot = int(usage.get("output_tokens") or 0)
        tt = usage.get("total_tokens")
        if tt is None:
            tt = it + ot
        tt_i = int(tt or 0)
        # Guard negative/odd values
        if it < 0:
            it = 0
        if ot < 0:
            ot = 0
        if tt_i < 0:
            tt_i = 0
        return (it, ot, tt_i)
    except Exception:
        return (0, 0, 0)


def _safe_err_text(r: httpx.Response) -> str:
    try:
        return (r.text or "")[:800]
    except Exception:
        return ""


def _try_parse_error_message(r: httpx.Response) -> str:
    """
    Best-effort parse of OpenAI error JSON, else short body.
    """
    try:
        data = r.json()
        # common: {"error": {"message": "...", "type": "...", ...}}
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:800]
        # some APIs: {"message": "..."}
        msg2 = data.get("message")
        if isinstance(msg2, str) and msg2.strip():
            return msg2.strip()[:800]
    except Exception:
        pass
    return _safe_err_text(r)


def _parse_retry_after_seconds(r: httpx.Response) -> Optional[float]:
    """
    Returns Retry-After seconds if present and valid.
    """
    try:
        ra = r.headers.get("Retry-After")
        if not ra:
            return None
        ra = ra.strip()
        return float(int(ra))
    except Exception:
        return None


def _jitter_sleep(base_s: float) -> float:
    # jitter within +-40%
    j = 0.6 + random.random() * 0.8
    return max(0.05, base_s * j)


def _get_env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        v = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        v = default
    return max(min_value, v)


# ----------------------------
# Shared AsyncClient (faster, fewer sockets)
# ----------------------------
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Per-process safety valve: limits simultaneous in-flight HTTP requests
# even if Redis concurrency caps are high or misconfigured.
_proc_sem: Optional[asyncio.Semaphore] = None
_proc_sem_lock = asyncio.Lock()


async def _get_proc_semaphore() -> asyncio.Semaphore:
    global _proc_sem
    if _proc_sem is not None:
        return _proc_sem
    async with _proc_sem_lock:
        if _proc_sem is not None:
            return _proc_sem
        # Default: 20 concurrent in-flight requests per process.
        # Tune via AI_PROCESS_CONCURRENCY.
        cap = _get_env_int("AI_PROCESS_CONCURRENCY", 20, min_value=1)
        _proc_sem = asyncio.Semaphore(cap)
        return _proc_sem


async def _get_client(timeout_s: float) -> httpx.AsyncClient:
    global _client
    if _client is not None:
        return _client

    async with _client_lock:
        if _client is not None:
            return _client

        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)
        _client = httpx.AsyncClient(limits=limits)
        return _client


async def aclose_ai_client() -> None:
    """
    Optional: call on shutdown if you have a graceful close path.
    Not required, but nice on long-running processes.
    """
    global _client
    async with _client_lock:
        if _client is not None:
            try:
                await _client.aclose()
            except Exception:
                pass
            _client = None


# ----------------------------
# Public API
# ----------------------------
async def generate_text(
    user: str | None = None,
    *,
    system: str,
    temperature: float = 0.8,
    max_output_tokens: int = 350,
    timeout_s: float | None = None,
    return_raw: bool = False,
    model: str | None = None,
    **kwargs,
) -> str | AIResult:
    """
    Async OpenAI call using HTTPX (Responses API).

    Supports BOTH call styles:
      - await generate_text("hello", system="...")
      - await generate_text(user="hello", system="...")

    Raises stable exceptions for callers to handle.
    """
    # Support keyword user=... even if caller passes it via kwargs
    if user is None:
        kw_user = kwargs.get("user")
        if isinstance(kw_user, str):
            user = kw_user

    if not isinstance(user, str) or not user.strip():
        raise AIError("generate_text() missing required 'user' prompt text.")

    url = _endpoint()
    key = _require_key()  # validate early

    if timeout_s is None:
        timeout_s = _default_timeout_s()

    payload = {
        "model": model or _model(),
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    attempts = _get_env_int("OPENAI_RETRY_ATTEMPTS", 2, min_value=1)  # default 2 (initial + 1 retry)

    client = await _get_client(timeout_s)
    sem = await _get_proc_semaphore()

    for attempt in range(1, attempts + 1):
        try:
            async with sem:
                r = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "User-Agent": "discord-bot/1.0 (utils/ai_client.py)",
                    },
                    json=payload,
                    timeout=httpx.Timeout(timeout_s),
                )

            # Auth
            if r.status_code in (401, 403):
                raise AIAuthError("AI authentication failed (check API key / permissions).")

            # Rate limit / quota
            if r.status_code == 429:
                if attempt < attempts:
                    ra = _parse_retry_after_seconds(r)
                    sleep_s = ra if ra is not None else _jitter_sleep(1.0)
                    await asyncio.sleep(sleep_s)
                    continue
                raise AIRateLimitError("AI is rate-limited right now (429).")

            # 5xx transient
            if r.status_code in (500, 502, 503, 504):
                if attempt < attempts:
                    await asyncio.sleep(_jitter_sleep(1.0))
                    continue
                raise AIStatusError(r.status_code, f"AI service error ({r.status_code}).")

            # Other non-OK
            if r.status_code < 200 or r.status_code >= 300:
                msg = _try_parse_error_message(r)
                raise AIStatusError(r.status_code, f"AI request failed ({r.status_code}): {msg}")

            data = r.json()
            txt = _extract_text(data)

            it, ot, tt = _extract_usage_tokens(data)

            if return_raw:
                return AIResult(text=txt, raw=data, input_tokens=it, output_tokens=ot, total_tokens=tt)
            return txt

        except httpx.TimeoutException as e:
            if attempt < attempts:
                await asyncio.sleep(_jitter_sleep(0.6))
                continue
            raise AITimeoutError("AI request timed out.") from e

        except httpx.RequestError as e:
            if attempt < attempts:
                await asyncio.sleep(_jitter_sleep(0.6))
                continue
            raise AIConnectionError("Failed to reach AI service.") from e

        except AIError:
            raise

        except Exception as e:
            if attempt < attempts:
                await asyncio.sleep(_jitter_sleep(0.6))
                continue
            raise AIError(f"AI request failed: {type(e).__name__}: {e}") from e

    raise AIError("AI request failed after retries.")
