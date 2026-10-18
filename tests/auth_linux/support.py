"""Shared, offline test doubles for the Linux sign-in path.

Everything in this module is synthetic. No real Linux account is looked up, no
real password is used, no password is ever *chosen* here, and no socket on the
machine is touched except by the one integration test, which creates its own in
a temporary directory.

A word on what a :class:`FakePamBackend` is worth: it proves that the machinery
*around* PAM behaves — that an allowlisted account reaches the backend and a
refused one does not, that the answer is mapped correctly, that a malformed
answer is refused. It proves nothing about PAM. A fake that returns
``AUTHENTICATED`` is not evidence that any real password would be accepted by
``pam_unix``, and the runbook says so in as many words.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.auth_linux.accounts import LinuxAccount
from backend.auth_linux.broker_server import BrokerConfig, PeerCreds, build_broker_config
from backend.auth_linux.pam_backend import PamOutcome, PamResult
from backend.auth_linux.policy import AccountPolicy, AdminGrant, Allowlist

# Synthetic names, none of which is a real host account. None of them is
# looked up for real; the account table is a test double.
SYNTHETIC_HOST_ID = "dell-test-01"
ADMIN_NAME = "carol"
ADMIN_UID = 4000
MEMBER_NAME = "alice"
MEMBER_UID = 4001
OTHER_MEMBER_NAME = "dave"
OTHER_MEMBER_UID = 4002
# The person whose Linux account name is *not* yet known. The tests use this to
# prove there is no alias mechanism that would quietly bind "dana" to "bob".
UNBOUND_NAME = "dana"
REAL_ACCOUNT_NAME = "bob"
REAL_ACCOUNT_UID = 4003

# Obviously synthetic. Never a real credential, and never used against anything
# that is not a test double.
GOOD_PASSWORD = "synthetic-test-only"
WRONG_PASSWORD = "synthetic-wrong-value-0002"


def make_account(name: str, uid: int, *, shell: str = "/bin/bash") -> LinuxAccount:
    return LinuxAccount(name=name, uid=uid, gid=uid, shell=shell, home=f"/home/{name}")


class FakeClock:
    """A clock the test moves by hand, so expiry never needs a sleep."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakePwdSource:
    """A synthetic account table, standing in for NSS."""

    def __init__(self, accounts: list[LinuxAccount] | None = None) -> None:
        self.accounts: dict[str, LinuxAccount] = {}
        for account in accounts or []:
            self.add(account)

    def add(self, account: LinuxAccount) -> "FakePwdSource":
        self.accounts[account.name] = account
        return self

    def remove(self, name: str) -> None:
        self.accounts.pop(name, None)

    def by_name(self, name: str) -> LinuxAccount | None:
        return self.accounts.get(name)

    def by_uid(self, uid: int) -> LinuxAccount | None:
        for account in self.accounts.values():
            if account.uid == uid:
                return account
        return None


class FakePamBackend:
    """A scripted PAM backend.

    ``table`` maps a synthetic account name to the synthetic password that this
    double will accept. ``outcomes`` overrides the outcome for a name, which is
    how the tests drive the locked/expired/disabled branches without inventing
    credentials for them.
    """

    def __init__(
        self,
        table: dict[str, str] | None = None,
        outcomes: dict[str, str] | None = None,
    ) -> None:
        self.table = dict(table or {})
        self.outcomes = dict(outcomes or {})
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, user: str, password: str) -> PamResult:
        # Recorded so tests can assert that a refused account never reached PAM,
        # and that the password the caller supplied is the one that was checked.
        self.calls.append((user, password))
        scripted = self.outcomes.get(user)
        if scripted is not None:
            return PamResult(outcome=scripted, detail="scripted")
        expected = self.table.get(user)
        if expected is not None and expected == password:
            return PamResult(outcome=PamOutcome.AUTHENTICATED)
        return PamResult(outcome=PamOutcome.INVALID_CREDENTIALS)

    @property
    def users_seen(self) -> list[str]:
        return [user for user, _ in self.calls]


class ExplodingPamBackend:
    """A backend that raises, to prove a PAM fault cannot become a login."""

    def __init__(self, exception: BaseException | None = None) -> None:
        self._exception = exception or RuntimeError("synthetic backend fault")
        self.calls = 0

    def verify(self, *, user: str, password: str) -> PamResult:
        self.calls += 1
        raise self._exception


class InProcessTransport:
    """A broker transport that never opens a socket.

    ``responder`` receives the request frame and returns a response frame. Used
    for the client-side tests, where the point is the mapping and the refusal
    paths rather than the socket.
    """

    def __init__(self, responder) -> None:
        self._responder = responder
        self.frames: list[bytes] = []

    def roundtrip(self, frame: bytearray, *, timeout: float) -> bytes:
        self.frames.append(bytes(frame))
        return self._responder(bytes(frame), timeout)


class RejectingTransport:
    """A transport that always raises, for the fail-closed paths."""

    def __init__(self, exception: BaseException) -> None:
        self._exception = exception

    def roundtrip(self, frame: bytearray, *, timeout: float) -> bytes:
        raise self._exception


class FakeSocket:
    """Just enough of a socket for the connection handler.

    ``recv_calls`` is the interesting field: a rejected peer must be closed on
    without a single read, so the password is never even buffered.
    """

    def __init__(self, inbound: bytes = b"") -> None:
        self.inbound = bytearray(inbound)
        self.sent = bytearray()
        self.recv_calls = 0
        self.sendall_calls = 0

    def recv(self, bufsize: int) -> bytes:
        self.recv_calls += 1
        chunk = bytes(self.inbound[:bufsize])
        del self.inbound[:bufsize]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sendall_calls += 1
        self.sent.extend(data)


@dataclass
class BrokerHarness:
    """A broker, its doubles, and the handles a test needs to make assertions."""

    broker: object
    accounts: FakePwdSource
    pam: object
    audit: object
    throttle: object
    clock: FakeClock
    config: BrokerConfig
    peer: PeerCreds
    allowlist: Allowlist = field(default_factory=lambda: Allowlist(frozenset()))
    admin_grant: AdminGrant | None = None


def peer(uid: int = 4001, gid: int = 4001, pid: int = 4242) -> PeerCreds:
    return PeerCreds(pid=pid, uid=uid, gid=gid)


def default_allowlist() -> Allowlist:
    return Allowlist(frozenset({ADMIN_NAME, MEMBER_NAME, OTHER_MEMBER_NAME, REAL_ACCOUNT_NAME}))


def default_admin_grant() -> AdminGrant:
    return AdminGrant(
        host_id=SYNTHETIC_HOST_ID, entries=frozenset({(ADMIN_UID, ADMIN_NAME)})
    )


def default_policy(
    *,
    allowlist: Allowlist | None = None,
    admin_grant: AdminGrant | None = None,
    min_human_uid: int = 1000,
) -> AccountPolicy:
    return AccountPolicy(
        allowlist=allowlist if allowlist is not None else default_allowlist(),
        host_id=SYNTHETIC_HOST_ID,
        admin_grant=admin_grant if admin_grant is not None else default_admin_grant(),
        min_human_uid=min_human_uid,
    )


def default_accounts() -> FakePwdSource:
    return FakePwdSource(
        [
            make_account(ADMIN_NAME, ADMIN_UID),
            make_account(MEMBER_NAME, MEMBER_UID),
            make_account(OTHER_MEMBER_NAME, OTHER_MEMBER_UID),
            make_account(REAL_ACCOUNT_NAME, REAL_ACCOUNT_UID),
            make_account("root", 0),
            make_account("daemon", 1, shell="/usr/sbin/nologin"),
            make_account("systemd-resolve", 991, shell="/usr/sbin/nologin"),
        ]
    )


def default_pam() -> FakePamBackend:
    return FakePamBackend(
        table={
            ADMIN_NAME: GOOD_PASSWORD,
            MEMBER_NAME: GOOD_PASSWORD,
            OTHER_MEMBER_NAME: GOOD_PASSWORD,
            REAL_ACCOUNT_NAME: GOOD_PASSWORD,
        }
    )


def build_broker(
    *,
    socket_path: str = "/tmp/unused-for-direct-calls.sock",
    accounts: FakePwdSource | None = None,
    pam=None,
    policy: AccountPolicy | None = None,
    throttle=None,
    audit=None,
    allowed_peer_uids: set[int] | None = None,
    host_id: str = SYNTHETIC_HOST_ID,
    require_socket_owner_uid: int | None = None,
    require_socket_dir_owner_uid: int | None = None,
):
    """Wire a broker for direct, socket-free testing of its decision core."""
    from backend.auth_linux.audit import ListAuditSink
    from backend.auth_linux.broker_server import PamBroker
    from backend.auth_linux.throttle import InMemoryThrottle

    clock = FakeClock()
    resolved_accounts = accounts if accounts is not None else default_accounts()
    resolved_audit = audit if audit is not None else ListAuditSink()
    resolved_throttle = throttle if throttle is not None else InMemoryThrottle(clock=clock)

    config = build_broker_config(
        socket_path=socket_path,
        host_id=host_id,
        policy=policy if policy is not None else default_policy(),
        accounts=resolved_accounts,
        pam_backend=pam if pam is not None else default_pam(),
        throttle=resolved_throttle,
        allowed_peer_uids=allowed_peer_uids if allowed_peer_uids is not None else {MEMBER_UID},
        require_socket_owner_uid=require_socket_owner_uid,
        require_socket_dir_owner_uid=require_socket_dir_owner_uid,
    )
    broker = PamBroker(config, audit=resolved_audit)
    return broker, config, resolved_accounts, resolved_audit, resolved_throttle, clock


__all__ = [
    "ADMIN_NAME",
    "ADMIN_UID",
    "GOOD_PASSWORD",
    "MEMBER_NAME",
    "MEMBER_UID",
    "OTHER_MEMBER_NAME",
    "OTHER_MEMBER_UID",
    "REAL_ACCOUNT_NAME",
    "REAL_ACCOUNT_UID",
    "SYNTHETIC_HOST_ID",
    "UNBOUND_NAME",
    "WRONG_PASSWORD",
    "BrokerHarness",
    "ExplodingPamBackend",
    "FakeClock",
    "FakePamBackend",
    "FakePwdSource",
    "FakeSocket",
    "InProcessTransport",
    "RejectingTransport",
    "build_broker",
    "default_accounts",
    "default_admin_grant",
    "default_allowlist",
    "default_pam",
    "default_policy",
    "make_account",
    "peer",
]
