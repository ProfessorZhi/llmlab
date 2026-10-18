"""The wire contract between the web process and the authentication broker.

Both sides of the socket import this module, so the request and response shapes
cannot drift apart. The format is one JSON object per frame, newline-terminated,
with every field validated on arrival.

What is deliberately *not* in the request:

``service``
    The PAM service name is the broker's own configuration. If a client could
    name it, a caller would be choosing which PAM stack to run against, and the
    usual way that goes wrong is a stack with ``nullok`` or a permissive
    ``pam_acct_mgmt``. The broker reads its service from its own root-owned
    configuration and ignores any attempt to supply one — attempts to supply one
    fail the request, because unknown keys are an error rather than noise.

Anything else not listed is also an error. A frame is either exactly what we
write, or it is rejected and counted; there is no lenient parse that tries to
find a request inside a malformed message.

Every field is bounded. The password is the reason: a frame is read into memory
in the web process *and* in the broker, so an unbounded frame is a way to make a
privileged process allocate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

PROTOCOL_VERSION = 1
OP_VERIFY = "verify"

# A request is a login name, a password and a correlation id. 4 KiB is a
# generous ceiling that still refuses to be a memory-exhaustion primitive.
MAX_FRAME_BYTES = 4096
MAX_PASSWORD_BYTES = 1024
MAX_LOGIN_NAME_LENGTH = 32
MAX_REQUEST_ID_LENGTH = 64

FRAME_TERMINATOR = b"\n"


class ProtocolError(Exception):
    """A frame was not exactly what this protocol says a frame is.

    Internal. It never reaches a client as-is: the provider maps it to
    :data:`backend.auth_linux.reasons.DenialReason.BROKER_PROTOCOL_ERROR`, which
    the client renders as "verification unavailable". A malformed broker reply
    must not become a login.
    """


class BrokerUnreachable(ProtocolError):
    """The conversation could not be held at all.

    A subclass of :class:`ProtocolError` so that a caller which does not care
    about the distinction still refuses, while a caller that does care can tell
    "the broker is not there" (an operational problem, and the expected state
    until the broker is installed) from "the broker answered with nonsense".
    Both are refusals; only the audit reason differs.
    """


# --- outcomes the broker reports --------------------------------------------


class BrokerStatus:
    """Precise outcomes. The broker may report these; clients must collapse them."""

    OK = "ok"
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_EXPIRED = "account_expired"
    PASSWORD_EXPIRED = "password_expired"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_CHECK_FAILED = "account_check_failed"
    USER_NOT_ALLOWED = "user_not_allowed"
    EMPTY_PASSWORD = "empty_password"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"
    PAM_UNAVAILABLE = "pam_unavailable"
    INTERNAL_ERROR = "internal_error"


ALL_STATUSES = frozenset(
    value
    for name, value in vars(BrokerStatus).items()
    if not name.startswith("_") and isinstance(value, str)
)

# Statuses for which a UID and a name are meaningful.
SUCCESS_STATUSES = frozenset({BrokerStatus.OK})

_REQUEST_KEYS = frozenset({"version", "op", "user", "password", "request_id"})
_RESPONSE_BASE_KEYS = frozenset({"version", "status"})
_RESPONSE_SUCCESS_KEYS = _RESPONSE_BASE_KEYS | {"uid", "name", "account_valid"}


@dataclass(frozen=True)
class BrokerRequest:
    user: str
    password: str = field(repr=False)
    request_id: str
    version: int = PROTOCOL_VERSION
    op: str = OP_VERIFY

    def __repr__(self) -> str:
        # `field(repr=False)` covers the dataclass-generated repr; this override
        # covers the case where someone formats the object by hand. A password
        # must not reach a traceback, a log line or a debugger dump by accident.
        return f"BrokerRequest(user={self.user!r}, request_id={self.request_id!r}, password=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class BrokerResponse:
    status: str
    uid: int | None = None
    name: str | None = None
    account_valid: bool | None = None
    version: int = PROTOCOL_VERSION

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS_STATUSES


def validate_request(request: BrokerRequest) -> None:
    """Reject a request we should never put on the wire."""
    if request.version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if request.op != OP_VERIFY:
        raise ProtocolError("unsupported operation")
    if not isinstance(request.user, str) or not request.user:
        raise ProtocolError("request has no user")
    if len(request.user) > MAX_LOGIN_NAME_LENGTH:
        raise ProtocolError("request user is too long")
    if not isinstance(request.password, str):
        raise ProtocolError("request password is not a string")
    if len(request.password.encode("utf-8", "surrogatepass")) > MAX_PASSWORD_BYTES:
        raise ProtocolError("request password is too long")
    if not isinstance(request.request_id, str) or not request.request_id:
        raise ProtocolError("request has no request id")
    if len(request.request_id) > MAX_REQUEST_ID_LENGTH:
        raise ProtocolError("request id is too long")


def encode_request(request: BrokerRequest) -> bytes:
    validate_request(request)
    return _encode_frame(
        {
            "version": request.version,
            "op": request.op,
            "user": request.user,
            "password": request.password,
            "request_id": request.request_id,
        }
    )


def decode_request(frame: bytes) -> BrokerRequest:
    document = _decode_frame(frame)
    _require_exact_keys(document, _REQUEST_KEYS, what="request")

    if document["version"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if document["op"] != OP_VERIFY:
        raise ProtocolError("unsupported operation")

    user = document["user"]
    password = document["password"]
    request_id = document["request_id"]
    if not isinstance(user, str) or not user:
        raise ProtocolError("request user must be a non-empty string")
    if not isinstance(password, str):
        raise ProtocolError("request password must be a string")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("request id must be a non-empty string")

    request = BrokerRequest(user=user, password=password, request_id=request_id)
    validate_request(request)
    return request


def encode_response(response: BrokerResponse) -> bytes:
    if response.status not in ALL_STATUSES:
        raise ProtocolError("unsupported status")
    document: dict[str, Any] = {"version": response.version, "status": response.status}
    if response.status in SUCCESS_STATUSES:
        if not isinstance(response.uid, int) or isinstance(response.uid, bool) or response.uid < 0:
            raise ProtocolError("successful response needs a numeric uid")
        if not isinstance(response.name, str) or not response.name:
            raise ProtocolError("successful response needs a name")
        if not isinstance(response.account_valid, bool):
            raise ProtocolError("successful response needs an account validity flag")
        document["uid"] = response.uid
        document["name"] = response.name
        document["account_valid"] = response.account_valid
    return _encode_frame(document)


def decode_response(frame: bytes) -> BrokerResponse:
    document = _decode_frame(frame)

    if "status" not in document or not isinstance(document["status"], str):
        raise ProtocolError("response has no status")
    status = document["status"]
    if status not in ALL_STATUSES:
        raise ProtocolError("response status is not one we defined")

    if status in SUCCESS_STATUSES:
        # A success reply must be complete. Half an identity is not one.
        _require_exact_keys(document, _RESPONSE_SUCCESS_KEYS, what="response")
        uid = document["uid"]
        name = document["name"]
        account_valid = document["account_valid"]
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise ProtocolError("response uid must be a non-negative integer")
        if not isinstance(name, str) or not name:
            raise ProtocolError("response name must be a non-empty string")
        if not isinstance(account_valid, bool):
            raise ProtocolError("response account_valid must be a boolean")
        return BrokerResponse(
            status=status, uid=uid, name=name, account_valid=account_valid, version=document["version"]
        )

    # Failure replies carry nothing but the status. There is no field in which a
    # broker could try to volunteer credential material.
    _require_exact_keys(document, _RESPONSE_BASE_KEYS, what="response")
    return BrokerResponse(status=status, version=document["version"])


# --- framing ----------------------------------------------------------------


class _RecvStream(Protocol):
    def recv(self, bufsize: int) -> bytes: ...


def read_frame(stream: _RecvStream, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    """Read exactly one newline-terminated frame, bounded.

    Returns the frame *without* the terminator. Raises
    :class:`BrokerUnreachable` when the read never completed, and
    :class:`ProtocolError` when bytes arrived but broke the contract (an
    oversized line, or a second frame appended to the first). Callers treat
    every one of those as "verification did not happen".
    """
    buffer = bytearray()
    while True:
        if len(buffer) > max_bytes:
            raise ProtocolError("frame exceeds the maximum size")
        try:
            chunk = stream.recv(min(1024, max_bytes + 1 - len(buffer)))
        except OSError as exc:
            raise BrokerUnreachable(f"cannot read from the broker: {exc}") from None
        if not chunk:
            raise BrokerUnreachable("peer closed before a complete frame arrived")
        buffer.extend(chunk)
        if FRAME_TERMINATOR in buffer:
            break

    frame, _, trailing = buffer.partition(FRAME_TERMINATOR)
    if trailing:
        # Two frames in one read means the peer is not speaking this protocol.
        # Silently keeping the first would let a server smuggle extra content.
        raise ProtocolError("unexpected trailing data after the frame")
    if len(frame) > max_bytes:
        raise ProtocolError("frame exceeds the maximum size")
    return bytes(frame)


def _encode_frame(document: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ProtocolError(f"cannot encode frame: {exc}") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the maximum size")
    return encoded + FRAME_TERMINATOR


def _decode_frame(frame: bytes) -> dict[str, Any]:
    if not isinstance(frame, (bytes, bytearray)):
        raise ProtocolError("frame must be bytes")
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the maximum size")
    try:
        document = json.loads(bytes(frame).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ProtocolError("frame must be a JSON object")
    if document.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    return document


def _require_exact_keys(document: dict[str, Any], expected: frozenset[str], *, what: str) -> None:
    keys = set(document)
    extra = keys - expected
    missing = expected - keys
    if extra:
        raise ProtocolError(f"{what} has unsupported keys: {sorted(extra)}")
    if missing:
        raise ProtocolError(f"{what} is missing keys: {sorted(missing)}")


__all__ = [
    "ALL_STATUSES",
    "BrokerRequest",
    "BrokerResponse",
    "BrokerStatus",
    "BrokerUnreachable",
    "FRAME_TERMINATOR",
    "MAX_FRAME_BYTES",
    "MAX_LOGIN_NAME_LENGTH",
    "MAX_PASSWORD_BYTES",
    "MAX_REQUEST_ID_LENGTH",
    "OP_VERIFY",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "SUCCESS_STATUSES",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
    "read_frame",
    "validate_request",
]
