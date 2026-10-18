"""Composition, and the entry point the rest of the portal calls.

There is no HTTP here, no routing, no cookie handling and no session. The
service takes a submitted login name and password, plus an opaque
``client_key`` the caller derives from the request, and either returns a
:class:`~backend.auth_linux.provider.VerifiedIdentity` or raises a
:class:`~backend.auth_linux.errors.LinuxAuthError` that is already safe to
render.

Whoever wires this into the existing WSGI application supplies the routes; this
module supplies the decision.

The factory below is the whole wiring, in one readable place, so an operator can
see exactly which pieces a deployment gets: the platform's real
:class:`pwd` lookups, the root-owned allowlist and grant read from disk, a Unix
socket to the broker, and an in-memory throttle. The throttle is the one piece a
deployment should replace — the auth service's database-backed limiter is
strictly better, and this factory takes one if the caller has it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import errors
from .accounts import PwdSource, SystemPwdSource
from .audit import AuditSink, NullAuditSink
from .broker_client import BrokerClient, BrokerTransport, UnixSocketTransport
from .config import LinuxAuthConfig, from_environ
from .handoff import SessionHandoff, UserDraft, draft_from_identity
from .policy import AccountPolicy, AdminGrant, Allowlist, load_admin_grant, load_allowlist, no_admin_grant
from .provider import LinuxPamAuthProvider, VerifiedIdentity
from .securefile import FileFacts, default_fs_probe
from .throttle import InMemoryThrottle, ThrottleHook


@dataclass(frozen=True)
class LinuxAuthStatus:
    """Operator-facing summary. Never rendered to an end user."""

    enabled: bool
    available: bool
    host_id: str | None
    allowlist_size: int
    platform_admin_grants: int
    broker_socket: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "host_id": self.host_id,
            "allowlist_size": self.allowlist_size,
            "platform_admin_grants": self.platform_admin_grants,
            "broker_socket": self.broker_socket,
        }


class LinuxAuthService:
    def __init__(
        self,
        provider: LinuxPamAuthProvider,
        *,
        host_id: str | None = None,
        allowlist: Allowlist | None = None,
        admin_grant: AdminGrant | None = None,
        broker_socket: str | None = None,
        enabled: bool = True,
        audit: AuditSink | None = None,
    ) -> None:
        self._provider = provider
        self._host_id = host_id
        self._allowlist = allowlist
        self._admin_grant = admin_grant
        self._broker_socket = broker_socket
        self._enabled = bool(enabled)
        self._audit = audit if audit is not None else NullAuditSink()

    @property
    def provider(self) -> LinuxPamAuthProvider:
        return self._provider

    # --- discovery -----------------------------------------------------------

    def providers(self) -> dict[str, bool]:
        """Which sign-in methods the UI may offer. Never raises.

        Shaped to slot into the existing ``/auth/csrf`` payload's ``providers``
        object as a sibling of ``local_admin`` and ``feishu``.
        """
        return {"linux_pam": bool(self._enabled and self._provider.is_available())}

    def status(self) -> LinuxAuthStatus:
        return LinuxAuthStatus(
            enabled=self._enabled,
            available=self._provider.is_available(),
            host_id=self._host_id,
            allowlist_size=len(self._allowlist.allowed_users) if self._allowlist else 0,
            platform_admin_grants=len(self._admin_grant.entries) if self._admin_grant else 0,
            broker_socket=self._broker_socket,
        )

    # --- login ---------------------------------------------------------------

    def login(self, login_name: str, password: str, *, client_key: str) -> VerifiedIdentity:
        """Verify a Linux account, or raise an error that is safe to render.

        The password arrives as a ``str``, is handed to the provider, and is
        never stored, logged, formatted or returned. See the runbook for what
        can and cannot be guaranteed about its lifetime in a CPython process.
        """
        if not self._enabled:
            raise errors.NotConfigured(detail="the Linux sign-in path is not enabled")

        outcome = self._provider.login(login_name, password, client_key=client_key)
        if not outcome.ok or outcome.identity is None:
            raise outcome.to_client_error()
        return outcome.identity

    # --- handoff -------------------------------------------------------------

    def begin_session(self, identity: VerifiedIdentity, handoff: SessionHandoff) -> object:
        """Hand a verified identity to the auth service's session manager.

        The only thing this package does with a session is ask someone else to
        start one. ``handoff`` is the auth service's implementation; see
        :mod:`backend.auth_linux.handoff` for the rules it must hold.
        """
        draft: UserDraft = draft_from_identity(identity)
        return handoff.start_session(draft)


# --- wiring ------------------------------------------------------------------


def build_provider(
    config: LinuxAuthConfig,
    *,
    audit: AuditSink | None = None,
    accounts: PwdSource | None = None,
    transport: BrokerTransport | None = None,
    throttle: ThrottleHook | None = None,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> LinuxPamAuthProvider:
    """Assemble the provider from configuration, reading policy from disk."""
    enabled = config.require_enabled()

    allowlist = load_allowlist(
        _required_path(enabled.allowlist_path, "allowlist"),
        required_owner_uid=enabled.policy_owner_uid,
        required_parent_owner_uid=enabled.policy_parent_owner_uid,
        fs_probe=fs_probe,
    )

    if enabled.admin_grant_path is not None:
        admin_grant = load_admin_grant(
            enabled.admin_grant_path,
            host_id=enabled.host,
            required_owner_uid=enabled.policy_owner_uid,
            required_parent_owner_uid=enabled.policy_parent_owner_uid,
            fs_probe=fs_probe,
        )
    else:
        admin_grant = no_admin_grant(enabled.host)

    policy = AccountPolicy(
        allowlist=allowlist,
        host_id=enabled.host,
        admin_grant=admin_grant,
        min_human_uid=enabled.min_human_uid,
    )

    resolved_transport = (
        transport
        if transport is not None
        else UnixSocketTransport(_required_path(enabled.broker_socket_path, "broker socket"))
    )
    broker = BrokerClient(resolved_transport, timeout=enabled.broker_timeout_seconds)

    resolved_throttle = (
        throttle
        if throttle is not None
        else InMemoryThrottle(
            max_failures=enabled.max_failures,
            window_seconds=enabled.window_seconds,
            lockout_seconds=enabled.lockout_seconds,
        )
    )

    return LinuxPamAuthProvider(
        accounts=accounts if accounts is not None else SystemPwdSource(),
        policy=policy,
        broker=broker,
        throttle=resolved_throttle,
        host_id=enabled.host,
        audit=audit,
        available=True,
    )


def build_service_from_environ(
    environ: Mapping[str, str] | None = None,
    *,
    audit: AuditSink | None = None,
    accounts: PwdSource | None = None,
    transport: BrokerTransport | None = None,
    throttle: ThrottleHook | None = None,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> LinuxAuthService:
    """The root call: configuration in, a service out.

    Raises :class:`~backend.auth_linux.errors.NotConfigured` when the feature is
    switched off, so a caller can simply not offer the sign-in method, and
    :class:`~backend.auth_linux.errors.ConfigError` when it is switched on
    incorrectly.
    """
    source = os.environ if environ is None else environ
    config = from_environ(source)
    provider = build_provider(
        config,
        audit=audit,
        accounts=accounts,
        transport=transport,
        throttle=throttle,
        fs_probe=fs_probe,
    )

    allowlist = load_allowlist(
        _required_path(config.allowlist_path, "allowlist"),
        required_owner_uid=config.policy_owner_uid,
        required_parent_owner_uid=config.policy_parent_owner_uid,
        fs_probe=fs_probe,
    )
    admin_grant = (
        load_admin_grant(
            config.admin_grant_path,
            host_id=config.host,
            required_owner_uid=config.policy_owner_uid,
            required_parent_owner_uid=config.policy_parent_owner_uid,
            fs_probe=fs_probe,
        )
        if config.admin_grant_path is not None
        else no_admin_grant(config.host)
    )

    return LinuxAuthService(
        provider,
        host_id=config.host,
        allowlist=allowlist,
        admin_grant=admin_grant,
        broker_socket=str(config.broker_socket_path) if config.broker_socket_path else None,
        enabled=True,
        audit=audit,
    )


def _required_path(path: Path | None, what: str) -> Path:
    if path is None:  # pragma: no cover - guarded by config validation
        raise errors.ConfigError(detail=f"{what} path is not configured")
    return path


__all__ = [
    "LinuxAuthService",
    "LinuxAuthStatus",
    "build_provider",
    "build_service_from_environ",
]
