from __future__ import annotations

"""Simple object storage helper (S3/R2 compatible).

This is used for scalable /voice uploads.

Env vars:
  VOICE_STORAGE_MODE=s3
  S3_ENDPOINT_URL (optional, for R2/MinIO)
  S3_REGION (optional)
  S3_BUCKET (required)
  S3_ACCESS_KEY_ID (required)
  S3_SECRET_ACCESS_KEY (required)

Optional:
  VOICE_PUBLIC_BASE_URL (if your bucket is public via a CDN; we will build URLs)

If VOICE_PUBLIC_BASE_URL is not set, we generate presigned GET URLs (default expiry 7 days).
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    url: str


def storage_mode() -> str:
    return (os.getenv("VOICE_STORAGE_MODE", "local") or "local").strip().lower()


def _require_env(name: str) -> str:
    v = (os.getenv(name, "") or "").strip()
    if not v:
        raise RuntimeError(f"{name} is missing")
    return v


def _s3_client():
    try:
        import boto3  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "boto3 is required for VOICE_STORAGE_MODE=s3. Add boto3 to requirements.txt."
        ) from e

    endpoint_url = (os.getenv("S3_ENDPOINT_URL", "") or "").strip() or None
    region = (os.getenv("S3_REGION", "") or "").strip() or None

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=_require_env("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_require_env("S3_SECRET_ACCESS_KEY"),
    )


def _public_base_url(env_name: str, override: Optional[str] = None) -> Optional[str]:
    """Return a public base URL if configured.

    Voice uses VOICE_PUBLIC_BASE_URL.
    Image assets can override with ASSET_PUBLIC_BASE_URL.
    """
    if override:
        return override.rstrip("/")
    u = (os.getenv(env_name, "") or "").strip()
    return u.rstrip("/") if u else None


def _presign_expires(env_name: str, override: Optional[int] = None) -> int:
    if isinstance(override, int) and override > 0:
        return max(60, override)
    raw = (os.getenv(env_name, "604800") or "604800").strip()
    try:
        return max(60, int(raw))
    except Exception:
        return 604800


async def upload_bytes(
    *,
    key: str,
    data: bytes,
    content_type: str = "audio/wav",
    bucket_override: Optional[str] = None,
    public_base_url_override: Optional[str] = None,
    presign_expires_s_override: Optional[int] = None,
    public_base_url_env: str = "VOICE_PUBLIC_BASE_URL",
    presign_expires_env: str = "VOICE_PRESIGN_EXPIRES_S",
) -> ObjectRef:
    """Upload bytes to S3/R2.

    Backwards compatible defaults are tuned for /voice.
    Image assets can pass overrides/env names.
    """
    bucket = (bucket_override or os.getenv("S3_BUCKET") or "").strip()
    if not bucket:
        raise RuntimeError("S3_BUCKET is missing")

    def _do_upload() -> None:
        c = _s3_client()
        c.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    await asyncio.to_thread(_do_upload)

    base = _public_base_url(public_base_url_env, public_base_url_override)
    if base:
        url = f"{base}/{key.lstrip('/')}"
        return ObjectRef(bucket=bucket, key=key, url=url)

    # Presigned (default 7 days)
    expires_s = _presign_expires(presign_expires_env, presign_expires_s_override)

    def _do_presign() -> str:
        c = _s3_client()
        return c.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=max(60, int(expires_s)),
        )

    url = await asyncio.to_thread(_do_presign)
    return ObjectRef(bucket=bucket, key=key, url=url)


async def download_bytes(*, key: str) -> bytes:
    bucket = _require_env("S3_BUCKET")

    def _do_download() -> bytes:
        c = _s3_client()
        obj = c.get_object(Bucket=bucket, Key=key)
        body = obj.get("Body")
        return body.read() if body else b""

    return await asyncio.to_thread(_do_download)


async def delete_object(*, key: str) -> None:
    """Best-effort delete."""
    bucket = _require_env("S3_BUCKET")

    def _do_delete() -> None:
        c = _s3_client()
        try:
            c.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass

    await asyncio.to_thread(_do_delete)
