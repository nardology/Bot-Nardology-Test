from __future__ import annotations

import hashlib
import hmac
import os


def hash_pack_password(password: str, *, salt_hex: str | None = None, rounds: int = 200_000) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) using PBKDF2-HMAC-SHA256."""
    pw = (password or "").encode("utf-8")
    if salt_hex:
        salt = bytes.fromhex(salt_hex)
    else:
        salt = os.urandom(16)
        salt_hex = salt.hex()

    dk = hashlib.pbkdf2_hmac("sha256", pw, salt, int(rounds))
    return salt_hex, dk.hex()


def verify_pack_password(password: str, *, salt_hex: str, hash_hex: str, rounds: int = 200_000) -> bool:
    """Constant-time verify."""
    try:
        _, candidate = hash_pack_password(password, salt_hex=salt_hex, rounds=rounds)
        return hmac.compare_digest(candidate, str(hash_hex or ""))
    except Exception:
        return False
