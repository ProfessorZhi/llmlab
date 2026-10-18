"""The ``AuthProvider`` abstraction, and the Linux/PAM implementation of it.

An ``AuthProvider`` answers one question — "is this person who they say they
are" — and reports the answer as an internal :class:`LoginOutcome`. It does not
know about HTTP, sessions, cookies or roles beyond naming the role the policy
assigned. A second provider (the Feishu one under development elsewhere, or a
test double) can sit behind the same protocol without either provider knowing
about the other.

The order of checks in :meth:`LinuxPamAuthProvider.login` is the security
design, so it is stated once, here:

1. **Shape.** Is the submitted name even a Linux login name? A name that is not
   well formed never reaches NSS, PAM, or the broker.
2. **An empty password is not a password.** Refused here as well as in the
   broker, because a ``nullok`` PAM stack would otherwise call it a success.
3. **Throttle.** Counted before anything expensive, and counted for names that
   do not exist, so the counter cannot be used to ask "does this account
   exist?".
4. **Does the account exist?** A read-only lookup. Failure is a refusal.
5. **May this account be a portal identity at all?** The policy, which is where
   ``root``, service accounts and the allowlist are enforced.
6. **Only now ask the broker**, which is the only step that costs a privileged
   conversation.
7. **Disbelieve the answer unless it is self-consistent.** The broker's UID and
   name must agree with our own read-only view of the same account. A broker —
   or a process that has taken the broker's place on the socket — cannot name a
   different account than the one we asked about.

A known limitation, stated rather than hidden
---------------------------------------------
Step 6 runs only for accounts the policy admits. An account that is refused at
step 4 or 5 gets its answer without a broker round trip, so the *time* a
response takes is a weak signal about whether a name is a policy-admitted
account. Two things make this acceptable here, and both should be re-examined
if the threat model changes: Linux account names on a shared host are already
discoverable by any local user (``getent passwd``), and the portal allowlist
names the platform's own staff rather than anything confidential. It is
documented so that it is a decision rather than an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import errors
from .accounts import PwdSource, canonical_login_name, uid_matches
from .audit import AuditEvent, AuditSink, NullAuditSink
from .broker_client import BrokerClient
from .handoff import AUTH_SOURCE_LINUX
from .policy import AccountPolicy
from .reasons import DenialReason
from .throttle import SCOPE_LINUX_LOGIN, ThrottleHook, login_throttle_key

PROVIDER_NAME = "linux_pam"


@dataclass(frozen=True)
class VerifiedIdentity:
    """A Linux account whose password has just been verified.

    This is the *whole* output of the sign-in path. It carries no credential, no
    hash, no shadow field, no broker detail: an account name, the UID that the
    name resolves to on this host, the host, and the role the policy assigned.
    """

    login_name: str
    uid: int
    host_id: str
    role: str
    display_name: str
    account_valid: bool = True
    auth_source: str = AUTH_SOURCE_LINUX


@dataclass(frozen=True)
class LoginOutcome:
    """The result of an attempt, before it is rendered for a client.

    ``reason`` stays internal. :meth:`to_client_error` is the only supported way
    to turn an outcome into something a client may see, and it cannot produce
    anything but a refusal.
    """

    ok: bool
    reason: str | None = None
    identity: VerifiedIdentity | None = None
    retry_after: int | None = None

    def to_client_error(self) -> errors.LinuxAuthError:
        from .reasons import client_error_for

        if self.ok:  # pragma: no cover - guarded by callers
            raise AssertionError("a successful outcome has no client error")
        return client_error_for(self.reason or DenialReason.INVALID_CREDENTIALS, retry_after=self.retry_after)


class AuthProvider(Protocol):
    """What the rest of the portal is allowed to assume about a provider."""

    name: str

    def is_available(self) -> bool: ...

    def login(self, login_name: str, password: str, *, client_key: str) -> LoginOutcome: ...


class LinuxPamAuthProvider:
    def __init__(
        self,
        *,
        accounts: PwdSource,
        policy: AccountPolicy,
        broker: BrokerClient,
        throttle: ThrottleHook,
        host_id: str,
        audit: AuditSink | None = None,
        available: bool = True,
    ) -> None:
        self._accounts = accounts
        self._policy = policy
        self._broker = broker
        self._throttle = throttle
        self._host_id = host_id
        self._audit = audit if audit is not None else NullAuditSink()
        self._available = bool(available)

    name = PROVIDER_NAME

    def is_available(self) -> bool:
        """Whether this sign-in method should be offered at all.

        This is a statement about *configuration*, not about reachability. A
        broker that is configured but down is still offered, and fails closed
        with ``PAM_UNAVAILABLE`` at the moment of use, which is more honest than
        a capability probe that quietly changes the login page's shape.
        """
        return self._available

    def login(self, login_name: str, password: str, *, client_key: str) -> LoginOutcome:
        login = canonical_login_name(login_name)
        if login is None:
            self._record("linux_login", "rejected", DenialReason.INVALID_NAME, client_key)
            return LoginOutcome(ok=False, reason=DenialReason.INVALID_NAME)

        if not isinstance(password, str) or password == "":
            self._record("linux_login", "rejected", DenialReason.EMPTY_PASSWORD, client_key, actor=login)
            return LoginOutcome(ok=False, reason=DenialReason.EMPTY_PASSWORD)

        throttle_key = login_throttle_key(client_key=client_key, login_name=login)
        try:
            self._throttle.check(SCOPE_LINUX_LOGIN, throttle_key)
        except errors.RateLimited as exc:
            self._record("linux_login", "throttled", DenialReason.RATE_LIMITED, client_key, actor=login)
            return LoginOutcome(ok=False, reason=DenialReason.RATE_LIMITED, retry_after=exc.retry_after)

        account = self._accounts.by_name(login)
        if account is None:
            self._charge(throttle_key)
            self._record("linux_login", "rejected", DenialReason.UNKNOWN_USER, client_key, actor=login)
            return LoginOutcome(ok=False, reason=DenialReason.UNKNOWN_USER)

        decision = self._policy.evaluate(account)
        if not decision.allowed:
            self._charge(throttle_key)
            self._record(
                "linux_login", "rejected", decision.reason or "policy_denied", client_key, actor=login
            )
            return LoginOutcome(ok=False, reason=decision.reason or DenialReason.NOT_ALLOWLISTED)

        verification = self._broker.verify(login, password)
        if not verification.authenticated:
            self._charge(throttle_key)
            reason = verification.reason or DenialReason.INVALID_CREDENTIALS
            self._record("linux_login", "rejected", reason, client_key, actor=login)
            return LoginOutcome(ok=False, reason=reason)

        if not uid_matches(account, uid=verification.uid, name=verification.name):
            # The broker answered about somebody else, or about nobody. Either
            # way it is not an authentication.
            self._charge(throttle_key)
            self._record("linux_login", "rejected", DenialReason.UID_MISMATCH, client_key, actor=login)
            return LoginOutcome(ok=False, reason=DenialReason.UID_MISMATCH)

        if verification.account_valid is not True:
            self._charge(throttle_key)
            self._record(
                "linux_login", "rejected", DenialReason.ACCOUNT_CHECK_FAILED, client_key, actor=login
            )
            return LoginOutcome(ok=False, reason=DenialReason.ACCOUNT_CHECK_FAILED)

        self._throttle.record_success(SCOPE_LINUX_LOGIN, throttle_key)
        role = decision.role or "member"
        self._record("linux_login", "success", None, client_key, actor=login)
        return LoginOutcome(
            ok=True,
            identity=VerifiedIdentity(
                login_name=login,
                uid=account.uid,
                host_id=self._host_id,
                role=role,
                display_name=account.name,
            ),
        )

    def _charge(self, throttle_key: str) -> None:
        self._throttle.record_failure(SCOPE_LINUX_LOGIN, throttle_key)

    def _record(
        self,
        event: str,
        outcome: str,
        reason: str | None,
        client_key: str,
        *,
        actor: str | None = None,
    ) -> None:
        self._audit.record(
            AuditEvent(
                event=event,
                outcome=outcome,
                reason=reason,
                actor=actor,
                host_id=self._host_id,
                # The client key is a request-derived address, not a credential.
                fields={"client_key": client_key},
            )
        )


__all__ = [
    "PROVIDER_NAME",
    "AuthProvider",
    "LinuxPamAuthProvider",
    "LoginOutcome",
    "VerifiedIdentity",
]
