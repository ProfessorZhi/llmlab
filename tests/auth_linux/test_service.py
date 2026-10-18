"""The composition root: configuration in, a usable service out."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.auth_linux import errors, protocol
from backend.auth_linux.audit import ListAuditSink
from backend.auth_linux.broker_client import BrokerClient
from backend.auth_linux.handoff import AUTH_SOURCE_LINUX, UserDraft
from backend.auth_linux.policy import ROLE_ADMIN, ROLE_MEMBER
from backend.auth_linux.protocol import BrokerResponse, BrokerStatus
from backend.auth_linux.provider import LinuxPamAuthProvider
from backend.auth_linux.service import (
    LinuxAuthService,
    build_provider,
    build_service_from_environ,
)
from backend.auth_linux.throttle import InMemoryThrottle

from . import support

CLIENT_KEY = "203.0.113.9"


def make_responder(accounts, table):
    """A stand-in broker: correct password in, identity out."""

    def respond(frame: bytes, timeout: float) -> bytes:
        request = protocol.decode_request(frame)
        account = accounts.by_name(request.user)
        if account is None or table.get(request.user) != request.password:
            return protocol.encode_response(
                BrokerResponse(status=BrokerStatus.INVALID_CREDENTIALS)
            )
        return protocol.encode_response(
            BrokerResponse(
                status=BrokerStatus.OK,
                uid=account.uid,
                name=account.name,
                account_valid=True,
            )
        )

    return respond


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.owner = os.getuid()
        self.accounts = support.default_accounts()
        self.table = {
            support.ADMIN_NAME: support.GOOD_PASSWORD,
            support.MEMBER_NAME: support.GOOD_PASSWORD,
            support.OTHER_MEMBER_NAME: support.GOOD_PASSWORD,
        }

        self.allowlist_path = self._write_policy(
            "allowlist.json",
            json.dumps(
                {
                    "version": 1,
                    "allowed_users": [
                        support.ADMIN_NAME,
                        support.MEMBER_NAME,
                        support.OTHER_MEMBER_NAME,
                    ],
                }
            ),
        )
        self.grant_path = self._write_policy(
            "admins.json",
            json.dumps(
                {
                    "version": 1,
                    "host_id": support.SYNTHETIC_HOST_ID,
                    "platform_admins": [
                        {"uid": support.ADMIN_UID, "name": support.ADMIN_NAME}
                    ],
                }
            ),
        )

        self.env = {
            "LLM_LAB_LINUX_AUTH_ENABLED": "1",
            "LLM_LAB_LINUX_HOST_ID": support.SYNTHETIC_HOST_ID,
            "LLM_LAB_PAM_BROKER_SOCKET": str(self.dir / "broker.sock"),
            "LLM_LAB_LINUX_ALLOWLIST_FILE": str(self.allowlist_path),
            "LLM_LAB_LINUX_ADMIN_GRANT_FILE": str(self.grant_path),
            "LLM_LAB_LINUX_POLICY_OWNER_UID": str(self.owner),
        }

    def _write_policy(self, name: str, text: str, *, mode: int = 0o600) -> Path:
        """Write a policy file with an **explicit** mode, and return its path.

        ``Path.write_text`` creates the file with ``0o666 & ~umask``, so a host
        running umask 0002 (Debian/Ubuntu default for a shared group) produces a
        group-writable ``0664`` file. ``securefile`` refuses that -- correctly --
        which turns every test in this module into a false failure whose cause
        has nothing to do with what it asserts. Pinning the mode keeps the
        deliberate refusal paths (missing, malformed, foreign host, wrong owner)
        as the only ways to fail here.
        """
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, *, audit=None, env=None) -> LinuxAuthService:
        return build_service_from_environ(
            self.env if env is None else env,
            accounts=self.accounts,
            transport=support.InProcessTransport(make_responder(self.accounts, self.table)),
            throttle=InMemoryThrottle(),
            audit=audit,
        )


class WiringTests(ServiceHarness):
    def test_a_complete_configuration_produces_a_working_service(self) -> None:
        service = self.build()
        self.assertEqual(service.providers(), {"linux_pam": True})

    def test_the_feature_is_off_unless_asked_for(self) -> None:
        with self.assertRaises(errors.NotConfigured):
            build_service_from_environ({})

    def test_a_missing_allowlist_stops_start_up(self) -> None:
        env = dict(self.env)
        env["LLM_LAB_LINUX_ALLOWLIST_FILE"] = str(self.dir / "absent.json")
        with self.assertRaises(errors.ConfigError):
            self.build(env=env)

    def test_a_malformed_allowlist_stops_start_up(self) -> None:
        self._write_policy("allowlist.json", '{"version": 1, "allowed_users": ["Root"]}')
        with self.assertRaises(errors.ConfigError):
            self.build()

    def test_a_grant_for_another_host_stops_start_up(self) -> None:
        self._write_policy(
            "admins.json",
            json.dumps(
                {
                    "version": 1,
                    "host_id": "a-different-host",
                    "platform_admins": [{"uid": 4000, "name": "carol"}],
                }
            ),
        )
        with self.assertRaises(errors.ConfigError):
            self.build()

    def test_a_missing_grant_file_is_fine_and_grants_nothing(self) -> None:
        env = dict(self.env)
        del env["LLM_LAB_LINUX_ADMIN_GRANT_FILE"]
        service = self.build(env=env)
        identity = service.login(support.ADMIN_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        self.assertEqual(identity.role, ROLE_MEMBER)


class LoginTests(ServiceHarness):
    def test_an_allowlisted_member_signs_in(self) -> None:
        service = self.build()
        identity = service.login(support.MEMBER_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        self.assertEqual(identity.uid, support.MEMBER_UID)
        self.assertEqual(identity.role, ROLE_MEMBER)
        self.assertEqual(identity.auth_source, AUTH_SOURCE_LINUX)

    def test_a_granted_administrator_signs_in_as_one(self) -> None:
        service = self.build()
        identity = service.login(support.ADMIN_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        self.assertEqual(identity.role, ROLE_ADMIN)

    def test_a_wrong_password_raises_a_generic_refusal(self) -> None:
        service = self.build()
        with self.assertRaises(errors.InvalidCredentials) as caught:
            service.login(support.MEMBER_NAME, support.WRONG_PASSWORD, client_key=CLIENT_KEY)
        self.assertEqual(caught.exception.code, "invalid_credentials")

    def test_every_account_level_refusal_looks_the_same(self) -> None:
        service = self.build()
        cases = [
            ("root", support.GOOD_PASSWORD),
            ("stranger", support.GOOD_PASSWORD),
            (support.MEMBER_NAME, support.WRONG_PASSWORD),
            (support.MEMBER_NAME, ""),
            ("Root", support.GOOD_PASSWORD),
        ]
        for name, password in cases:
            with self.subTest(name=name):
                with self.assertRaises(errors.InvalidCredentials):
                    service.login(name, password, client_key=CLIENT_KEY)

    def test_a_disabled_service_refuses_even_with_a_valid_password(self) -> None:
        provider = LinuxPamAuthProvider(
            accounts=self.accounts,
            policy=support.default_policy(),
            broker=BrokerClient(
                support.InProcessTransport(make_responder(self.accounts, self.table))
            ),
            throttle=InMemoryThrottle(),
            host_id=support.SYNTHETIC_HOST_ID,
        )
        service = LinuxAuthService(provider, host_id=support.SYNTHETIC_HOST_ID, enabled=False)
        with self.assertRaises(errors.NotConfigured):
            service.login(support.MEMBER_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        # A service that is switched off must not be advertised to the UI either,
        # even if the provider itself was constructed healthy.
        self.assertEqual(service.providers(), {"linux_pam": False})

    def test_a_disabled_service_is_not_offered(self) -> None:
        provider = LinuxPamAuthProvider(
            accounts=self.accounts,
            policy=support.default_policy(),
            broker=BrokerClient(
                support.InProcessTransport(make_responder(self.accounts, self.table))
            ),
            throttle=InMemoryThrottle(),
            host_id=support.SYNTHETIC_HOST_ID,
            available=False,
        )
        service = LinuxAuthService(provider, host_id=support.SYNTHETIC_HOST_ID)
        self.assertEqual(service.providers(), {"linux_pam": False})

    def test_the_service_never_returns_a_credential(self) -> None:
        service = self.build()
        identity = service.login(support.MEMBER_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        self.assertNotIn(support.GOOD_PASSWORD, repr(identity))
        self.assertNotIn("password", repr(identity).lower())


class StatusTests(ServiceHarness):
    def test_status_describes_the_deployment_without_leaking_secrets(self) -> None:
        service = self.build()
        report = service.status()
        self.assertTrue(report.enabled)
        self.assertTrue(report.available)
        self.assertEqual(report.host_id, support.SYNTHETIC_HOST_ID)
        self.assertEqual(report.allowlist_size, 3)
        self.assertEqual(report.platform_admin_grants, 1)
        rendered = json.dumps(report.to_dict())
        self.assertNotIn(support.GOOD_PASSWORD, rendered)

    def test_status_does_not_name_the_allowlisted_accounts(self) -> None:
        """A count is enough for an operator; a list is a target.

        Scope: everything the status says *about policy*. The one excluded field
        is ``broker_socket`` -- a deployment path supplied by the caller, which
        may contain any text, including an account name when the temp root sits
        under a home directory (``/data/scratch/<user>/...``). Scanning the whole
        serialized blob made this assertion depend on where the host puts its
        temp files rather than on the status contract.
        """
        status = self.build().status().to_dict()
        status.pop("broker_socket", None)

        for name in (support.ADMIN_NAME, support.MEMBER_NAME, support.OTHER_MEMBER_NAME):
            with self.subTest(name=name):
                self.assertNotIn(name, json.dumps(status))

        # The policy surface is counts; nothing in it is a collection of members.
        for field, value in status.items():
            with self.subTest(field=field):
                self.assertNotIsInstance(value, (list, tuple, set, frozenset))


class HandoffTests(ServiceHarness):
    class RecordingHandoff:
        def __init__(self) -> None:
            self.drafts: list[UserDraft] = []
            self.token = object()

        def start_session(self, draft: UserDraft) -> object:
            self.drafts.append(draft)
            return self.token

    def test_the_service_hands_the_identity_over_and_returns_what_it_gets(self) -> None:
        service = self.build()
        identity = service.login(support.MEMBER_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        handoff = self.RecordingHandoff()
        result = service.begin_session(identity, handoff)

        self.assertIs(result, handoff.token)
        self.assertEqual(len(handoff.drafts), 1)
        draft = handoff.drafts[0]
        self.assertEqual(draft.uid, support.MEMBER_UID)
        self.assertEqual(draft.role, ROLE_MEMBER)
        self.assertEqual(draft.auth_source, AUTH_SOURCE_LINUX)

    def test_the_handoff_receives_no_password(self) -> None:
        service = self.build()
        identity = service.login(support.MEMBER_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        handoff = self.RecordingHandoff()
        service.begin_session(identity, handoff)
        draft = handoff.drafts[0]
        self.assertNotIn(support.GOOD_PASSWORD, str(vars(draft)))
        self.assertEqual(
            set(vars(draft)),
            {
                "user_id",
                "auth_source",
                "external_id",
                "display_name",
                "role",
                "host_id",
                "uid",
                "login_name",
            },
        )

    def test_the_service_itself_mints_nothing(self) -> None:
        service = self.build()
        for forbidden in ("sessions", "cookies", "csrf"):
            with self.subTest(symbol=forbidden):
                self.assertFalse(hasattr(service, forbidden))


class ProviderBuilderTests(ServiceHarness):
    def test_build_provider_reads_policy_from_disk(self) -> None:
        from backend.auth_linux.config import from_environ

        config = from_environ(self.env)
        provider = build_provider(
            config,
            accounts=self.accounts,
            transport=support.InProcessTransport(make_responder(self.accounts, self.table)),
            throttle=InMemoryThrottle(),
            audit=ListAuditSink(),
        )
        outcome = provider.login(support.ADMIN_NAME, support.GOOD_PASSWORD, client_key=CLIENT_KEY)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.identity.role, ROLE_ADMIN)

    def test_build_provider_requires_the_feature_to_be_enabled(self) -> None:
        from backend.auth_linux.config import from_environ

        with self.assertRaises(errors.NotConfigured):
            build_provider(from_environ({}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
