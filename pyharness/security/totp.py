from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time

_ALGORITHMS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def _decode_seed(seed_b32: str) -> bytes:
    """Decode a base32 TOTP seed, tolerating the formatting provisioning UIs use:
    spaces/hyphens, lowercase, and missing padding. Error messages never include
    the seed — it is credential material."""
    compact = seed_b32.replace(" ", "").replace("-", "").upper()
    padded = compact + "=" * (-len(compact) % 8)
    try:
        key = base64.b32decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("TOTP seed is not valid base32") from exc
    if not key:
        raise ValueError("TOTP seed is empty")
    return key


def totp_code(
    seed_b32: str,
    *,
    digits: int = 6,
    period: int = 30,
    algorithm: str = "sha1",
    at: float | None = None,
) -> str:
    """The current RFC 6238 TOTP code for a base32 seed. Defaults (6 digits,
    30s period, SHA-1) match what nearly every site provisions; `at` fixes the
    time for tests and defaults to now. Runs parent-side only — neither the
    seed nor the returned code may reach agent code (see browser.fill_totp)."""
    try:
        digest = _ALGORITHMS[algorithm]
    except KeyError:
        raise ValueError(
            f"unknown TOTP algorithm {algorithm!r} (use sha1/sha256/sha512)"
        ) from None
    key = _decode_seed(seed_b32)
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)
