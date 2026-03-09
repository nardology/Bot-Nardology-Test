from __future__ import annotations

import logging
import os
import re
from io import BytesIO
from typing import Optional

import discord

import httpx

# We reuse the existing S3/R2 helper used by /voice.
# If you want pack/character images to be globally available across multiple bot
# instances, set ASSET_STORAGE_MODE=s3 and configure S3_* env vars.
from utils.object_store import upload_bytes as _s3_upload_bytes


logger = logging.getLogger(__name__)

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore


_SAFE_RE = re.compile(r"[^a-zA-Z0-9._/-]+")


def _assets_root() -> str:
    # Railway volume-friendly default
    return os.getenv("ASSETS_DIR", "data/assets")


def asset_storage_mode() -> str:
    """Storage mode for images saved via upload.

    - local (default): save to ASSETS_DIR and reference via asset:...
    - s3: upload to S3/R2 and return a public/presigned URL
    """
    return (os.getenv("ASSET_STORAGE_MODE", "local") or "local").strip().lower()


def _asset_bucket() -> str:
    # Allow separate bucket; fall back to the voice bucket if shared.
    return (os.getenv("ASSET_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip()


def _asset_public_base_url() -> str:
    return (os.getenv("ASSET_PUBLIC_BASE_URL") or "").strip().rstrip("/")


# Max size for fetched embed images (Discord embed image limit is 8MB; keep smaller for safety)
_FETCH_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_FETCH_IMAGE_TIMEOUT_S = 12.0


async def fetch_embed_image_as_file(url: str | None, *, filename: str = "image.png") -> discord.File | None:
    """Fetch image from URL and return a discord.File for use as attachment. Returns None on failure.

    Use with embed.set_image(url='attachment://filename') and send(files=[file]).
    Only allows https URLs; respects _FETCH_IMAGE_MAX_BYTES and timeout.
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s.startswith("https://"):
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_IMAGE_TIMEOUT_S) as client:
            resp = await client.get(s)
            resp.raise_for_status()
            data = resp.content
            if not data or len(data) > _FETCH_IMAGE_MAX_BYTES:
                logger.warning("fetch_embed_image_as_file: bad size (%s bytes) for %s", len(data) if data else 0, s[:80])
                return None
            base = (filename or "image").strip()
            if not base.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                base = base + ".png"
            return discord.File(BytesIO(data), filename=base)
    except Exception:
        logger.warning("fetch_embed_image_as_file failed for url=%s", s[:80], exc_info=True)
        return None


def resolve_embed_image_url(url: str | None) -> str | None:
    """Resolve image_url for use in Discord embeds (no file attach).

    - None/empty -> None
    - asset:rel/path -> ASSET_PUBLIC_BASE_URL/rel/path if set, else None
    - https? -> return as-is (Discord can load it)
    """
    if not url or not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    if s.startswith("asset:"):
        rel = s[len("asset:"):].strip().lstrip("/")
        if not rel:
            return None
        base = _asset_public_base_url()
        if not base:
            return None
        return f"{base}/{rel}"
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return None


def _asset_presign_expires_s() -> int | None:
    raw = (os.getenv("ASSET_PRESIGN_EXPIRES_S") or "").strip()
    if raw.isdigit():
        try:
            return max(60, int(raw))
        except Exception:
            return None
    return None


def _asset_key_prefix() -> str:
    return (os.getenv("ASSET_S3_PREFIX", "assets") or "assets").strip().strip("/")


def _max_asset_bytes() -> int:
    """Maximum allowed bytes for uploaded assets.

    Default: 2MB. Override with env MAX_ASSET_MB or MAX_ASSET_BYTES.
    """
    raw_bytes = (os.getenv("MAX_ASSET_BYTES") or "").strip()
    if raw_bytes.isdigit():
        return max(64 * 1024, int(raw_bytes))
    raw_mb = (os.getenv("MAX_ASSET_MB") or "2").strip()
    try:
        mb = float(raw_mb)
        return max(64 * 1024, int(mb * 1024 * 1024))
    except Exception:
        return 2 * 1024 * 1024


def ensure_assets_dirs() -> None:
    """Create base assets directory."""
    try:
        os.makedirs(_assets_root(), exist_ok=True)
    except Exception:
        # Non-fatal
        pass


def _clean_rel(rel_path: str) -> str:
    rel = (rel_path or "").strip().lstrip("/")
    rel = _SAFE_RE.sub("", rel)
    # Prevent path traversal
    rel = rel.replace("..", "")
    return rel


def asset_abspath(rel_path: str) -> str:
    rel = _clean_rel(rel_path)
    return os.path.join(_assets_root(), rel)


def get_discord_file_for_asset(rel_path: str) -> Optional[discord.File]:
    """Return a discord.File for an existing asset path, else None."""
    abs_path = asset_abspath(rel_path)
    if not os.path.isfile(abs_path):
        return None
    filename = os.path.basename(abs_path)
    try:
        return discord.File(abs_path, filename=filename)
    except Exception:
        return None


def _infer_ext(filename: str, content_type: str | None) -> str:
    base = (filename or "").strip()
    ext = os.path.splitext(base)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return ext
    ct = (content_type or "").lower().strip()
    if ct == "image/png":
        return ".png"
    if ct in {"image/jpg", "image/jpeg"}:
        return ".jpg"
    if ct == "image/webp":
        return ".webp"
    if ct == "image/gif":
        return ".gif"
    return ".png"


async def save_attachment_image(
    *,
    attachment: discord.Attachment,
    rel_dir: str,
    basename: str,
    max_bytes: int | None = None,
    upscale_min_px: int = 0,
) -> tuple[bool, str, str | None]:
    """Save an image/gif attachment into assets.

    Returns: (ok, message, rel_path)
    """
    if attachment is None:
        return False, "No file attached.", None

    ct = (attachment.content_type or "").lower()
    if not ct.startswith("image/"):
        return False, "Please upload an image or GIF (not a video).", None

    limit = int(max_bytes) if isinstance(max_bytes, int) and max_bytes > 0 else _max_asset_bytes()
    # Cheap pre-check (Discord provides size)
    try:
        if getattr(attachment, "size", None) and int(attachment.size) > limit:
            mb = limit / (1024 * 1024)
            return False, f"Image too large (max {mb:.1f}MB).", None
    except Exception:
        pass

    ext = _infer_ext(attachment.filename or "", attachment.content_type)
    rel_dir_clean = _clean_rel(rel_dir)
    base_clean = _SAFE_RE.sub("", (basename or "").strip().lower())
    if not base_clean:
        base_clean = "image"

    out_rel = f"{rel_dir_clean}/{base_clean}{ext}" if rel_dir_clean else f"{base_clean}{ext}"

    try:
        data = await attachment.read()
        if not data:
            return False, "Uploaded file was empty.", None

        if len(data) > limit:
            mb = limit / (1024 * 1024)
            return False, f"Image too large (max {mb:.1f}MB).", None

        # Optional upscaling for tiny images (NOT for GIFs).
        if upscale_min_px and ext != ".gif" and Image is not None:
            try:
                im = Image.open(BytesIO(data))
                w, h = im.size
                target = int(upscale_min_px)
                if w > 0 and h > 0 and max(w, h) < target:
                    scale = target / float(max(w, h))
                    new_w = max(1, int(round(w * scale)))
                    new_h = max(1, int(round(h * scale)))
                    im = im.resize((new_w, new_h), resample=Image.LANCZOS)
                    out = BytesIO()
                    fmt = "PNG" if ext == ".png" else "JPEG" if ext in {".jpg", ".jpeg"} else "WEBP" if ext == ".webp" else "PNG"
                    save_kwargs = {}
                    if fmt == "JPEG":
                        save_kwargs["quality"] = 92
                        save_kwargs["optimize"] = True
                    im.save(out, format=fmt, **save_kwargs)
                    data2 = out.getvalue()
                    # Only keep upscaled if it stays within limit.
                    if data2 and len(data2) <= limit:
                        data = data2
            except Exception:
                # non-fatal: just keep original
                pass
        # If configured, upload to S3/R2 and return a public/presigned URL.
        if asset_storage_mode() == "s3":
            prefix = _asset_key_prefix()
            key = f"{prefix}/{out_rel.lstrip('/')}"
            try:
                ref = await _s3_upload_bytes(
                    key=key,
                    data=data,
                    content_type=(attachment.content_type or "image/png"),
                    bucket_override=_asset_bucket() or None,
                    public_base_url_override=(_asset_public_base_url() or None),
                    presign_expires_s_override=_asset_presign_expires_s(),
                    public_base_url_env="ASSET_PUBLIC_BASE_URL",
                    presign_expires_env="ASSET_PRESIGN_EXPIRES_S",
                )
                return True, "Saved.", ref.url
            except Exception as e:
                # Give actionable feedback without leaking secrets.
                missing: list[str] = []
                if not (os.getenv("S3_BUCKET") or os.getenv("ASSET_S3_BUCKET")):
                    missing.append("S3_BUCKET")
                if not (os.getenv("S3_ACCESS_KEY_ID") or "").strip():
                    missing.append("S3_ACCESS_KEY_ID")
                if not (os.getenv("S3_SECRET_ACCESS_KEY") or "").strip():
                    missing.append("S3_SECRET_ACCESS_KEY")

                # For R2, endpoint is required; for AWS S3 it is optional.
                if (os.getenv("S3_ENDPOINT_URL") or "").strip() == "":
                    # Only call it out as missing if user likely intends R2.
                    # (Most people using this feature are on R2.)
                    missing.append("S3_ENDPOINT_URL (for R2)")

                logger.exception("S3/R2 asset upload failed: %s", e)
                if missing:
                    return (
                        False,
                        "⚠️ Asset storage is set to s3, but these env vars look missing/empty: "
                        + ", ".join(missing)
                        + ".",
                        None,
                    )
                return (
                    False,
                    "⚠️ Asset storage is set to s3 but the upload failed. Check S3_ENDPOINT_URL, bucket access, and credentials.",
                    None,
                )

        # Default: save locally under ASSETS_DIR.
        ensure_assets_dirs()
        out_abs = asset_abspath(out_rel)
        os.makedirs(os.path.dirname(out_abs), exist_ok=True)
        with open(out_abs, "wb") as f:
            f.write(data)
        return True, "Saved.", out_rel
    except Exception:
        return False, "Failed to save uploaded image.", None
