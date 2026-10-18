"""The web process's half of the broker contract.

Design rules, in order of how much they matter:

**Every failure is a refusal.** A missing socket, a refused connection, a
timeout, a truncated frame, an oversized frame, a status we did not define — all
of them produce ``authenticated=False``. :meth:`BrokerClient.verify` does not
raise for transport trouble and does not have a code path that returns success
because it could not get an answer. ``PAM_UNAVAILABLE`` is a refusal wearing a
different label, and the handler is expected to render it as "try again later"
rather than as a login.

**The client does not decide who may sign in.** It will happily ask the broker
about anyone. The allowlist, the UID floor and the ``root`` refusal live on the
privileged side, which is the only side that can be trusted to apply them, and
they are applied again by :mod:`backend.auth_linux.policy` on this side as well.
Two independent checks, either one of which is sufficient to refuse.

**The password is handled as bytes and wiped.** The frame sent to the broker
contains the password, so the frame lives in a mutable buffer that is
overwritten as soon as the round trip finishes. The original ``str`` that came
out of JSON decoding is a separate matter and is covered honestly in
``docs/linux-pam-login.md``: CPython strings are immutable, so the best available
practice is to keep their lifetime and their number of copies as small as
possible, which this package does by never storing, logging, formatting or
copying one.
"""

from __future__ import annotations

import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import protocol
from .protocol import BrokerStatus
from .reasons import DenialReason

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0


class BrokerTransport(Protocol):
    """One request frame in, one response frame out.

    Implemented over a Unix socket in production and by an in-process object in
    tests. The contract is narrow on purpose: it takes a mutable buffer so the
    caller can wipe it, and it either returns a complete frame or raises.
    """

    def roundtrip(self, frame: bytearray, *, timeout: float) -> bytes: ...


class UnixSocketTransport:
    """A one-shot connection to the broker's socket.

    Connect, write one frame, read one frame, close. There is no connection
    pooling and no retry: a retried password check is a second chance for a
    wrong password to be answered by a different, luckier moment, and pooling
    would keep a privileged connection alive between unrelated users' logins.
    """

    def __init__(
        self,
        socket_path: str | Path,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self._path = str(socket_path)
        self._connect_timeout = float(connect_timeout)
        self._socket_factory = socket_factory

    def roundtrip(self, frame: bytearray, *, timeout: float) -> bytes:
        sock = self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(float(timeout), self._connect_timeout))
            try:
                sock.connect(self._path)
            except OSError as exc:
                # Absent socket, refused connection, permission denied: the
                # broker is not usable right now. Not an authentication result.
                raise protocol.BrokerUnreachable(f"cannot reach the broker: {exc}") from None

            sock.settimeout(float(timeout))
            try:
                sock.sendall(frame)
            except OSError as exc:
                raise protocol.BrokerUnreachable(f"cannot write to the broker: {exc}") from None

            return protocol.read_frame(sock)
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover - defensive
                pass


@dataclass(frozen=True)
class BrokerVerification:
    """What the broker told us, already reduced to a decision."""

    authenticated: bool
    status: str
    reason: str | None
    uid: int | None = None
    name: str | None = None
    account_valid: bool | None = None

    @classmethod
    def unavailable(cls, reason: str, *, status: str = BrokerStatus.PAM_UNAVAILABLE) -> "BrokerVerification":
        return cls(authenticated=False, status=status, reason=reason)


# The broker is allowed to be precise; the collapse to a small, fixed set of
# public errors happens in `reasons.client_error_for`. Everything here that is
# not `OK` maps to *some* reason, and every reason maps to a refusal.
_STATUS_TO_REASON: dict[str, str | None] = {
    BrokerStatus.OK: None,
    BrokerStatus.INVALID_CREDENTIALS: DenialReason.INVALID_CREDENTIALS,
    BrokerStatus.ACCOUNT_LOCKED: DenialReason.ACCOUNT_LOCKED,
    BrokerStatus.ACCOUNT_EXPIRED: DenialReason.ACCOUNT_EXPIRED,
    BrokerStatus.PASSWORD_EXPIRED: DenialReason.PASSWORD_EXPIRED,
    BrokerStatus.ACCOUNT_DISABLED: DenialReason.ACCOUNT_DISABLED,
    BrokerStatus.ACCOUNT_CHECK_FAILED: DenialReason.ACCOUNT_CHECK_FAILED,
    BrokerStatus.USER_NOT_ALLOWED: DenialReason.NOT_ALLOWLISTED,
    BrokerStatus.EMPTY_PASSWORD: DenialReason.EMPTY_PASSWORD,
    BrokerStatus.RATE_LIMITED: DenialReason.RATE_LIMITED,
    # The broker saying "your request was malformed" means the two ends
    # disagree about the protocol. That is an operational fault, not a verdict
    # about the account, so it must not read as "wrong password".
    BrokerStatus.INVALID_REQUEST: DenialReason.BROKER_PROTOCOL_ERROR,
    BrokerStatus.PAM_UNAVAILABLE: DenialReason.BROKER_UNAVAILABLE,
    # An internal error is the server's problem; refusing is the only safe read.
    BrokerStatus.INTERNAL_ERROR: DenialReason.BROKER_UNAVAILABLE,
}


class BrokerClient:
    def __init__(
        self,
        transport: BrokerTransport,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request_id_factory: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._transport = transport
        self._timeout = float(timeout)
        self._request_id_factory = request_id_factory

    def verify(self, user: str, password: str) -> BrokerVerification:
        """Ask the broker about ``(user, password)``.

        Returns an outcome; never signals failure by raising and never returns
        success on anything other than an explicit, complete, well-formed
        success frame from the broker.
        """
        request = protocol.BrokerRequest(
            user=user, password=password, request_id=self._request_id_factory()
        )

        try:
            frame = bytearray(protocol.encode_request(request))
        except protocol.ProtocolError:
            # Our own request was not well formed. Nothing was sent.
            return BrokerVerification.unavailable(DenialReason.BROKER_PROTOCOL_ERROR)

        try:
            response_frame = self._transport.roundtrip(frame, timeout=self._timeout)
        except protocol.BrokerUnreachable:
            # Covers "not installed yet", which is the expected state today.
            return BrokerVerification.unavailable(DenialReason.BROKER_UNAVAILABLE)
        except protocol.ProtocolError:
            # Bytes arrived and did not obey the contract.
            return BrokerVerification.unavailable(DenialReason.BROKER_PROTOCOL_ERROR)
        except OSError:
            return BrokerVerification.unavailable(DenialReason.BROKER_UNAVAILABLE)
        finally:
            # The password was in that buffer. Overwrite it before doing anything
            # else, including before the response is interpreted.
            _wipe(frame)

        try:
            response = protocol.decode_response(response_frame)
        except protocol.ProtocolError:
            return BrokerVerification.unavailable(DenialReason.BROKER_PROTOCOL_ERROR)

        if response.status != BrokerStatus.OK:
            reason = _STATUS_TO_REASON.get(response.status, DenialReason.BROKER_PROTOCOL_ERROR)
            return BrokerVerification(
                authenticated=False, status=response.status, reason=reason
            )

        # A success reply has to be internally consistent as well as well formed.
        if response.account_valid is not True or response.uid is None or response.name is None:
            return BrokerVerification(
                authenticated=False,
                status=response.status,
                reason=DenialReason.ACCOUNT_CHECK_FAILED,
                uid=response.uid,
                name=response.name,
                account_valid=response.account_valid,
            )

        return BrokerVerification(
            authenticated=True,
            status=response.status,
            reason=None,
            uid=response.uid,
            name=response.name,
            account_valid=True,
        )


def _wipe(buffer: bytearray) -> None:
    """Overwrite a buffer that held credential material.

    Best-effort: it removes the copy we control. It cannot reach copies the
    interpreter made while decoding the request, which is why the surrounding
    design keeps those copies to a minimum rather than relying on this.
    """
    for index in range(len(buffer)):
        buffer[index] = 0


__all__ = [
    "BrokerClient",
    "BrokerTransport",
    "BrokerVerification",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "UnixSocketTransport",
]
