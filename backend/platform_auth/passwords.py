"""Password hashing for **platform** accounts only.

These credentials are independent of every Linux account. Nothing here reads
``/etc/shadow``, PAM, an SSH identity, or a UID; a platform password is never
compared with a Linux password, and an account may exist on one side only.

Format (versioned, self-describing)::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

The parameters travel with the hash, so a future increase in cost can be
applied per user without a migration flag day, and a stored hash stays
verifiable after the module defaults change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .errors import ValidationError

#: scrypt cost. 2**14 with r=8/p=1 is ~16 MiB and tens of milliseconds here;
#: it is the documented minimum we ship, not a one-off tuning value.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
#: 64 MiB ceiling keeps one verification from exhausting a shared workstation.
MAX_MEMORY_BYTES = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
SCHEME = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def validate_password(password: object) -> str:
    if not isinstance(password, str):
        raise ValidationError("password must be text")
    if password == "":
        raise ValidationError("password must not be empty")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError("password is too long")
    if password.strip() == "":
        raise ValidationError("password must not be only whitespace")
    return password


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=MAX_MEMORY_BYTES,
    )


def hash_password(
    password: object,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> str:
    """Return a salted, versioned scrypt hash. The plaintext is never stored."""
    text = validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(text, salt, n, r, p)
    return f"{SCHEME}${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def parse_hash(stored: object) -> tuple[int, int, int, bytes, bytes]:
    if not isinstance(stored, str):
        raise ValidationError("stored hash is not text")
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise ValidationError("unsupported password hash")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, TypeError) as exc:  # pragma: no cover - corrupt row
        raise ValidationError("malformed password hash") from exc
    if n < 2 or r < 1 or p < 1:
        raise ValidationError("unsafe password hash parameters")
    return n, r, p, salt, digest


def verify_password(stored: object, password: object) -> bool:
    """Constant-time verification. Never raises for a wrong password."""
    try:
        n, r, p, salt, expected = parse_hash(stored)
        if not isinstance(password, str) or password == "":
            return False
        actual = _derive(password, salt, n, r, p)
    except ValidationError:
        return False
    return hmac.compare_digest(actual, expected)


#: A fixed hash used when the login does not exist, so an unknown account costs
#: the same scrypt work as a known one with the wrong password.
DUMMY_HASH = hash_password("dummy-password-for-timing-only")


def burn_verification(password: object) -> None:
    """Spend comparable work for a login that does not exist."""
    verify_password(DUMMY_HASH, password if isinstance(password, str) else "")


__all__ = [
    "DUMMY_HASH",
    "MAX_MEMORY_BYTES",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "SCHEME",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "burn_verification",
    "hash_password",
    "parse_hash",
    "validate_password",
    "verify_password",
]
