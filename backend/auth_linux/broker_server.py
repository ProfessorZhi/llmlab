"""The authentication broker: the privileged side of the socket.

This is the component that would eventually be installed by the platform
administrator. It is written so that the part which decides things can be tested
without root, without a socket, and without PAM; only the accept loop and the
socket-permission checks need a real machine.

What it is
----------
A single-purpose process. It accepts a connection, checks *who connected*,
reads one frame, and answers one question: does this password belong to this
allowlisted account. It can run ``pam_authenticate`` and ``pam_acct_mgmt`` and
read ``getpwnam``. It cannot run a command, open a session, write a file, or
act on behalf of any account.

Refusing is the default at every step
-------------------------------------
* An unknown caller is closed on before a single byte is read from it, so a
  process that is not allowed to ask cannot even cause the password to be
  buffered here.
* A caller that is allowed but asks about an account outside the allowlist, or
  about ``root``, or about a daemon, is refused *without PAM being consulted*.
* ``pam_unix`` in a stack configured with ``nullok`` treats an empty password as
  "correct". The empty password is therefore refused before the backend sees it.
* The account is re-resolved after PAM answers, and the answer is only believed
  if the UID and the name still agree. This closes the window in which an
  account is renamed or a UID is recycled midway through a check.

Threading model
---------------
One connection at a time, handled to completion before the next accept. A PAM
conversation is a fraction of a second and login volume on a single-host portal
is tiny, so serial handling costs nothing measurable and removes a whole class
of concurrency bugs from a security-critical component.
"""

from __future__ import annotations

import os
import socket
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import errors, protocol
from .accounts import PwdSource, canonical_login_name, uid_matches
from .audit import AuditEvent, AuditSink, NullAuditSink
from .pam_backend import PamBackend, PamOutcome
from .policy import AccountPolicy
from .protocol import BrokerStatus
from .reasons import DenialReason
from .throttle import SCOPE_LINUX_LOGIN, ThrottleHook, login_throttle_key

DEFAULT_SOCKET_MODE = 0o600
DEFAULT_BACKLOG = 8
DEFAULT_ACCEPT_POLL_SECONDS = 0.5
DEFAULT_CONNECTION_TIMEOUT_SECONDS = 5.0

_OUTCOME_TO_STATUS = {
    PamOutcome.AUTHENTICATED: BrokerStatus.OK,
    PamOutcome.INVALID_CREDENTIALS: BrokerStatus.INVALID_CREDENTIALS,
    PamOutcome.ACCOUNT_LOCKED: BrokerStatus.ACCOUNT_LOCKED,
    PamOutcome.ACCOUNT_EXPIRED: BrokerStatus.ACCOUNT_EXPIRED,
    PamOutcome.PASSWORD_EXPIRED: BrokerStatus.PASSWORD_EXPIRED,
    PamOutcome.ACCOUNT_DISABLED: BrokerStatus.ACCOUNT_DISABLED,
    PamOutcome.ACCOUNT_CHECK_FAILED: BrokerStatus.ACCOUNT_CHECK_FAILED,
    PamOutcome.UNAVAILABLE: BrokerStatus.PAM_UNAVAILABLE,
}


@dataclass(frozen=True)
class PeerCreds:
    """Who is on the other end, as reported by the kernel."""

    pid: int
    uid: int
    gid: int


def socket_peer_creds(sock: socket.socket) -> PeerCreds:
    """Read ``SO_PEERCRED`` from a connected Unix socket.

    The kernel fills this in from the connecting process's own credentials; the
    peer cannot influence it. Unlike a path or a token, it is not something the
    caller can claim to be. This is the reason the broker can be talked to at
    all without also being reachable by every account on the host.
    """
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:  # pragma: no cover - non-Linux
        raise errors.ConfigError(detail="SO_PEERCRED is unavailable; callers cannot be identified")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    except OSError as exc:
        raise errors.ConfigError(detail=f"cannot read peer credentials: {exc}") from None
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCreds(pid=pid, uid=uid, gid=gid)


@dataclass(frozen=True)
class BrokerConfig:
    socket_path: Path
    host_id: str
    policy: AccountPolicy
    accounts: PwdSource
    pam_backend: PamBackend
    throttle: ThrottleHook

    # Which local accounts may talk to this socket. Empty means "nobody", which
    # is the safe reading of an unconfigured broker.
    allowed_peer_uids: frozenset[int] = field(default_factory=frozenset)
    allowed_peer_gids: frozenset[int] = field(default_factory=frozenset)

    # Deployment expectations, checked before the socket is created. None skips
    # the check; that is for tests only, and a deployment should leave 0.
    require_socket_owner_uid: int | None = 0
    require_socket_dir_owner_uid: int | None = 0

    socket_mode: int = DEFAULT_SOCKET_MODE
    backlog: int = DEFAULT_BACKLOG

    # How long ``accept`` waits before the loop re-checks whether it should stop.
    # Closing a listening socket does not reliably wake an ``accept`` already
    # blocked in another thread, so the loop needs a bounded wait of its own or
    # shutdown would hang rather than being merely slow.
    accept_poll_seconds: float = DEFAULT_ACCEPT_POLL_SECONDS

    # How long a connection may take to deliver its frame. Without this, one
    # client that connects and then says nothing would occupy the serial broker
    # indefinitely, and nobody else could sign in. A frame is a few hundred
    # bytes on a local socket, so this only ever fires on a peer that is stuck
    # or hostile.
    connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS

    def peer_is_allowed(self, peer: PeerCreds) -> bool:
        """UID *or* GID match. A GID is only as good as its membership, which is
        why it is offered as an alternative for group-managed deployments and
        not as the default."""
        return peer.uid in self.allowed_peer_uids or peer.gid in self.allowed_peer_gids


class PamBroker:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        audit: AuditSink | None = None,
        peer_creds_reader: Callable[[socket.socket], PeerCreds] = socket_peer_creds,
    ) -> None:
        self._config = config
        self._audit = audit if audit is not None else NullAuditSink()
        self._peer_creds = peer_creds_reader
        self._listener: socket.socket | None = None
        self._stopping = False

    # --- decision core (no sockets involved) --------------------------------

    def process_frame(self, frame: bytes | bytearray, peer: PeerCreds) -> bytes:
        """Turn one request frame into one response frame.

        Everything that decides an authentication outcome happens here, which is
        what makes the whole security-relevant path testable without root.
        """
        if not self._config.peer_is_allowed(peer):
            # Belt and braces: the accept loop already refuses such a peer before
            # reading, so reaching here means someone called this directly.
            self._record("broker_request", "rejected", DenialReason.BROKER_PROTOCOL_ERROR, peer=peer)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INVALID_REQUEST))

        try:
            request = protocol.decode_request(frame)
        except protocol.ProtocolError:
            self._record("broker_request", "rejected", DenialReason.BROKER_PROTOCOL_ERROR, peer=peer)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INVALID_REQUEST))

        login = canonical_login_name(request.user)
        if login is None:
            self._record("broker_login", "rejected", DenialReason.INVALID_NAME, peer=peer)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INVALID_REQUEST))

        if request.password == "":
            # Before PAM, always: a `nullok` stack would call this a success.
            self._record("broker_login", "rejected", DenialReason.EMPTY_PASSWORD, peer=peer, actor=login)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.EMPTY_PASSWORD))

        throttle_key = login_throttle_key(client_key=f"peer-uid:{peer.uid}", login_name=login)
        try:
            self._config.throttle.check(SCOPE_LINUX_LOGIN, throttle_key)
        except errors.RateLimited:
            self._record("broker_login", "throttled", DenialReason.RATE_LIMITED, peer=peer, actor=login)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.RATE_LIMITED))

        account = self._config.accounts.by_name(login)
        if account is None:
            self._charge_failure(throttle_key)
            self._record("broker_login", "rejected", DenialReason.UNKNOWN_USER, peer=peer, actor=login)
            return protocol.encode_response(
                protocol.BrokerResponse(status=BrokerStatus.INVALID_CREDENTIALS)
            )

        decision = self._config.policy.evaluate(account)
        if not decision.allowed:
            self._charge_failure(throttle_key)
            self._record(
                "broker_login", "rejected", decision.reason or "policy_denied", peer=peer, actor=login
            )
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.USER_NOT_ALLOWED))

        try:
            result = self._config.pam_backend.verify(user=login, password=request.password)
        except Exception as exc:  # noqa: BLE001 - a PAM fault must not become a login
            # Deliberately broad: whatever a backend does, the answer here is a
            # refusal. The exception text is not echoed to the caller.
            self._charge_failure(throttle_key)
            self._record(
                "broker_login", "error", DenialReason.BROKER_UNAVAILABLE, peer=peer, actor=login,
                detail_type=type(exc).__name__,
            )
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INTERNAL_ERROR))

        status = _OUTCOME_TO_STATUS.get(result.outcome, BrokerStatus.INTERNAL_ERROR)
        if status != BrokerStatus.OK:
            self._charge_failure(throttle_key)
            self._record("broker_login", "rejected", result.outcome, peer=peer, actor=login)
            return protocol.encode_response(protocol.BrokerResponse(status=status))

        # PAM said yes. Now check that "yes" is still about the account we
        # asked about: the name is re-resolved, and both the name→uid and
        # uid→name directions have to agree.
        confirmed = self._config.accounts.by_name(login)
        if confirmed is None or not uid_matches(account, uid=confirmed.uid, name=confirmed.name):
            self._record("broker_login", "rejected", DenialReason.UID_MISMATCH, peer=peer, actor=login)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INTERNAL_ERROR))

        by_uid = self._config.accounts.by_uid(confirmed.uid)
        if by_uid is None or by_uid.name != login:
            self._record("broker_login", "rejected", DenialReason.UID_MISMATCH, peer=peer, actor=login)
            return protocol.encode_response(protocol.BrokerResponse(status=BrokerStatus.INTERNAL_ERROR))

        self._config.throttle.record_success(SCOPE_LINUX_LOGIN, throttle_key)
        self._record("broker_login", "success", None, peer=peer, actor=login)
        return protocol.encode_response(
            protocol.BrokerResponse(
                status=BrokerStatus.OK, uid=confirmed.uid, name=confirmed.name, account_valid=True
            )
        )

    # --- connection handling -------------------------------------------------

    def handle_connection(self, conn: socket.socket) -> None:
        """Serve one connection.

        The peer is identified first and refused before the request is read, so
        an unauthorised process cannot make this component hold its password
        even briefly.
        """
        try:
            peer = self._peer_creds(conn)
        except errors.ConfigError as exc:
            self._record("broker_peer", "rejected", DenialReason.BROKER_PROTOCOL_ERROR, detail_type=type(exc).__name__)
            return

        if not self._config.peer_is_allowed(peer):
            self._record("broker_peer", "rejected", DenialReason.NOT_ALLOWLISTED, peer=peer)
            return

        buffer = bytearray()
        try:
            buffer.extend(protocol.read_frame(conn))
            response = self.process_frame(buffer, peer)
        except protocol.ProtocolError:
            self._record("broker_request", "rejected", DenialReason.BROKER_PROTOCOL_ERROR, peer=peer)
            response = protocol.encode_response(
                protocol.BrokerResponse(status=BrokerStatus.INVALID_REQUEST)
            )
        finally:
            # The frame held the password. Overwrite our copy of it.
            _wipe(buffer)

        try:
            conn.sendall(response)
        except OSError:
            # The caller went away. Nothing to do and nothing to leak.
            self._record("broker_response", "undeliverable", None, peer=peer)

    # --- socket lifecycle ----------------------------------------------------

    def prepare_listener(self) -> socket.socket:
        """Create, harden and bind the listening socket.

        The permission checks come first. A socket inside a directory that other
        accounts can write is a socket other accounts can replace, and the peer
        check would then be protecting the wrong endpoint.
        """
        if getattr(socket, "SO_PEERCRED", None) is None:  # pragma: no cover - non-Linux
            raise errors.ConfigError(detail="SO_PEERCRED is unavailable; refusing to serve")

        path = Path(self._config.socket_path)
        self._check_directory(path.parent)

        if path.exists() or path.is_symlink():
            facts = os.lstat(path)
            if not stat.S_ISSOCK(facts.st_mode):
                # Never delete something that is not ours to delete.
                raise errors.ConfigError(detail="socket path exists and is not a socket")
            path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            os.chmod(str(path), self._config.socket_mode)
            if self._config.require_socket_owner_uid is not None:
                owner = os.lstat(str(path)).st_uid
                if owner != self._config.require_socket_owner_uid:
                    raise errors.ConfigError(detail="socket is not owned by the required account")
            listener.listen(self._config.backlog)
            listener.settimeout(self._config.accept_poll_seconds)
        except BaseException:
            listener.close()
            raise

        self._listener = listener
        return listener

    def _check_directory(self, directory: Path) -> None:
        required = self._config.require_socket_dir_owner_uid
        try:
            facts = os.stat(directory)
        except OSError as exc:
            raise errors.ConfigError(detail=f"socket directory is unusable: {exc}") from None
        if not stat.S_ISDIR(facts.st_mode):
            raise errors.ConfigError(detail="socket directory is not a directory")
        if required is None:
            return
        if facts.st_uid != required:
            raise errors.ConfigError(detail="socket directory is not owned by the required account")
        if stat.S_IMODE(facts.st_mode) & 0o077:
            raise errors.ConfigError(
                detail="socket directory is accessible to group/other; expected 0700"
            )

    def serve_forever(self, *, on_ready: Callable[[socket.socket], None] | None = None) -> None:
        listener = self._listener or self.prepare_listener()
        if on_ready is not None:
            on_ready(listener)

        while not self._stopping:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                # Nothing waiting. Re-check `_stopping` and go round again.
                continue
            except OSError:
                # The listener was closed under us: expected on shutdown.
                break
            try:
                # Bounded, so a peer that connects and then goes quiet cannot
                # hold the serial broker open.
                conn.settimeout(self._config.connection_timeout_seconds)
                self.handle_connection(conn)
            finally:
                try:
                    conn.close()
                except OSError:  # pragma: no cover - defensive
                    pass

    def stop(self) -> None:
        self._stopping = True
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:  # pragma: no cover - defensive
                pass

    # --- helpers -------------------------------------------------------------

    def _charge_failure(self, throttle_key: str) -> None:
        self._config.throttle.record_failure(SCOPE_LINUX_LOGIN, throttle_key)

    def _record(
        self,
        event: str,
        outcome: str,
        reason: str | None,
        *,
        peer: PeerCreds | None = None,
        actor: str | None = None,
        detail_type: str | None = None,
    ) -> None:
        fields: dict[str, object] = {}
        if peer is not None:
            # Peer identity is safe to record: it is not credential material, and
            # an audit trail of *who asked* is the point of the broker.
            fields["peer_uid"] = peer.uid
            fields["peer_gid"] = peer.gid
            fields["peer_pid"] = peer.pid
        if detail_type is not None:
            fields["detail_type"] = detail_type
        self._audit.record(
            AuditEvent(
                event=event,
                outcome=outcome,
                reason=reason,
                actor=actor,
                host_id=self._config.host_id,
                fields=fields,
            )
        )


def _wipe(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


def build_broker_config(
    *,
    socket_path: str | Path,
    host_id: str,
    policy: AccountPolicy,
    accounts: PwdSource,
    pam_backend: PamBackend,
    throttle: ThrottleHook,
    allowed_peer_uids: Iterable[int] = (),
    allowed_peer_gids: Iterable[int] = (),
    require_socket_owner_uid: int | None = 0,
    require_socket_dir_owner_uid: int | None = 0,
    socket_mode: int = DEFAULT_SOCKET_MODE,
    accept_poll_seconds: float = DEFAULT_ACCEPT_POLL_SECONDS,
    connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
) -> BrokerConfig:
    """Build a configuration, refusing one that could not restrict callers.

    An empty caller set is accepted, because "nobody may connect" is a coherent
    if useless state, but it is worth being explicit that it is not the same as
    "anyone may connect".
    """
    return BrokerConfig(
        socket_path=Path(socket_path),
        host_id=host_id,
        policy=policy,
        accounts=accounts,
        pam_backend=pam_backend,
        throttle=throttle,
        allowed_peer_uids=frozenset(int(uid) for uid in allowed_peer_uids),
        allowed_peer_gids=frozenset(int(gid) for gid in allowed_peer_gids),
        require_socket_owner_uid=require_socket_owner_uid,
        require_socket_dir_owner_uid=require_socket_dir_owner_uid,
        socket_mode=socket_mode,
        accept_poll_seconds=accept_poll_seconds,
        connection_timeout_seconds=connection_timeout_seconds,
    )


__all__ = [
    "DEFAULT_ACCEPT_POLL_SECONDS",
    "DEFAULT_BACKLOG",
    "DEFAULT_CONNECTION_TIMEOUT_SECONDS",
    "DEFAULT_SOCKET_MODE",
    "BrokerConfig",
    "PamBroker",
    "PeerCreds",
    "build_broker_config",
    "socket_peer_creds",
]
