"""Password hashing for the single local administrator account.

We do not invent a password scheme. The default is ``scrypt`` from the standard
library (memory-hard, available on every supported interpreter including the
offline test box). Argon2id is accepted when ``argon2-cffi`` happens to be
installed, but it is never required.

Encoded form (self-describing so parameters can be raised later and detected)::

    scrypt$n=16384,r=8,p=1$<b64 salt>$<b64 digest>

Verification always goes through :func:`hmac.compare_digest`; there is no code
path that compares digests with ``==``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
DIGEST_BYTES = 32

_SCRYPT = "scrypt"
_ARGON2ID_PREFIX = "$argon2id$"
_SUPPORTED_ALGORITHMS = ("scrypt", "argon2id")

# OpenSSL's default scrypt maxmem (32 MiB) is enough for the defaults here, but
# we compute an explicit ceiling so that raising `n` fails loudly at hash time
# rather than silently at verification time.
_MIN_MAXMEM = 32 * 1024 * 1024


def argon2_available() -> bool:
    """True when ``argon2-cffi`` can be imported in this interpreter."""
    try:
        import argon2.low_level  # noqa: F401
    except Exception:  # pragma: no cover - depends on the host
        return False
    return True


def _maxmem(n: int, r: int, p: int) -> int:
    return max(_MIN_MAXMEM, 128 * r * (n + p + 2))


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None


def _encode_params(n: int, r: int, p: int) -> str:
    return f"n={n},r={r},p={p}"


def hash_password(
    password: str,
    *,
    algorithm: str = "scrypt",
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
    salt: bytes | None = None,
) -> str:
    """Return an encoded hash for ``password``.

    Raises :class:`ValueError` for an empty password: the administrator account
    must start with *no* password rather than a blank one, so callers are never
    allowed to hash the empty string by accident.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")

    if algorithm == "scrypt":
        if salt is None:
            salt = secrets.token_bytes(SALT_BYTES)
        if len(salt) < SALT_BYTES:
            raise ValueError(f"salt must be at least {SALT_BYTES} bytes")
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=DIGEST_BYTES,
            maxmem=_maxmem(n, r, p),
        )
        return "$".join([_SCRYPT, _encode_params(n, r, p), _b64encode(salt), _b64encode(digest)])

    if algorithm == "argon2id":
        if not argon2_available():
            raise ValueError("argon2id requested but argon2-cffi is not installed")
        from argon2 import PasswordHasher

        return PasswordHasher().hash(password)

    raise ValueError(f"unsupported password algorithm: {algorithm!r}")


def _parse_scrypt(encoded: str) -> tuple[dict[str, int], bytes, bytes] | None:
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != _SCRYPT:
        return None
    params: dict[str, int] = {}
    for chunk in parts[1].split(","):
        key, _, value = chunk.partition("=")
        if key not in ("n", "r", "p") or not value.isdigit():
            return None
        params[key] = int(value)
    if set(params) != {"n", "r", "p"}:
        return None
    if params["n"] < 2 or params["n"] & (params["n"] - 1):
        return None
    if params["r"] < 1 or params["p"] < 1:
        return None
    salt = _b64decode(parts[2])
    digest = _b64decode(parts[3])
    if not salt or not digest:
        return None
    return params, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification.

    Returns ``False`` for every malformed input instead of raising, so that a
    corrupt credential file degrades into "cannot log in" rather than a crash
    that might be distinguishable from a wrong password.
    """
    if not isinstance(password, str) or not password:
        return False
    if not isinstance(encoded, str) or not encoded:
        return False

    if encoded.startswith(_ARGON2ID_PREFIX):
        return _verify_argon2(password, encoded)

    parsed = _parse_scrypt(encoded)
    if parsed is None:
        return False
    params, salt, expected = parsed
    try:
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            dklen=len(expected),
            maxmem=_maxmem(params["n"], params["r"], params["p"]),
            **params,
        )
    except (ValueError, TypeError, OverflowError):
        return False
    return hmac.compare_digest(computed, expected)


def _verify_argon2(password: str, encoded: str) -> bool:
    if not argon2_available():  # pragma: no cover - host dependent
        return False
    from argon2 import PasswordHasher
    from argon2.exceptions import (
        InvalidHashError,
        VerificationError,
        VerifyMismatchError,
    )

    try:
        PasswordHasher().verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # pragma: no cover - defensive
        return False
    return True


def is_well_formed(encoded: str) -> bool:
    """True when ``encoded`` is a hash string this module can verify."""
    if not isinstance(encoded, str) or not encoded:
        return False
    if encoded.startswith(_ARGON2ID_PREFIX):
        return argon2_available()
    return _parse_scrypt(encoded) is not None


def hash_needs_rehash(encoded: str) -> bool:
    """True when ``encoded`` used weaker or different parameters than today's."""
    if not isinstance(encoded, str) or not encoded:
        return True
    if encoded.startswith(_ARGON2ID_PREFIX):
        if not argon2_available():  # pragma: no cover - host dependent
            return True
        from argon2 import PasswordHasher

        try:
            return PasswordHasher().check_needs_rehash(encoded)
        except Exception:  # pragma: no cover - defensive
            return True
    parsed = _parse_scrypt(encoded)
    if parsed is None:
        return True
    params, _salt, _digest = parsed
    return (params["n"], params["r"], params["p"]) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


__all__ = [
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "argon2_available",
    "hash_needs_rehash",
    "hash_password",
    "is_well_formed",
    "verify_password",
]
