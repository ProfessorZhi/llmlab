"""The provider: check ordering, deny cases, and what reaches the broker."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors
from backend.auth_linux.audit import ListAuditSink
from backend.auth_linux.broker_client import BrokerClient
from backend.auth_linux.broker_server import PeerCreds
from backend.auth_linux.handoff import AUTH_SOURCE_LINUX
from backend.auth_linux.pam_backend import UnavailablePamBackend
from backend.auth_linux.policy import ROLE_ADMIN, ROLE_MEMBER
from backend.auth_linux.provider import LinuxPamAuthProvider, LoginOutcome
from backend.auth_linux.reasons import DenialReason
from backend.auth_linux.throttle import InMemoryThrottle

from . import support

PEER = PeerCreds(pid=4242, uid=support.MEMBER_UID, gid=support.MEMBER_UID)
CLIENT_KEY = "203.0.113.9"


class Harness(unittest.TestCase):
    """A provider wired to a real broker's decision core, in process."""

    def build(
        self,
        *,
        accounts=None,
        pam=None,
        policy=None,
        provider_throttle=None,
        broker_throttle=None,
        broker_client=None,
        audit=None,
        available=True,
    ):
        (
            broker,
            config,
            resolved_accounts,
            broker_audit,
            resolved_broker_throttle,
            clock,
        ) = support.build_broker(
            accounts=accounts,
            pam=pam,
            policy=policy,
            throttle=broker_throttle,
            audit=audit,
        )
        transport = support.InProcessTransport(
            lambda frame, timeout: broker.process_frame(frame, PEER)
        )
        client = broker_client if broker_client is not None else BrokerClient(transport)
        provider = LinuxPamAuthProvider(
            accounts=resolved_accounts,
            policy=config.policy,
            broker=client,
            # A separate limiter from the broker's, so the two counters are
            # independent in the tests exactly as they are in a deployment.
            throttle=provider_throttle
            if provider_throttle is not None
            else InMemoryThrottle(clock=clock),
            host_id=support.SYNTHETIC_HOST_ID,
            audit=audit if audit is not None else ListAuditSink(),
            available=available,
        )
        self.transport = transport
        self.broker_pam = config.pam_backend
        return provider, resolved_accounts, clock

    def login(self, provider, name, password, client_key=CLIENT_KEY) -> LoginOutcome:
        return provider.login(name, password, client_key=client_key)


class SuccessTests(Harness):
    def test_an_allowlisted_member_signs_in_as_a_member(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertTrue(outcome.ok)
        identity = outcome.identity
        self.assertIsNotNone(identity)
        self.assertEqual(identity.login_name, support.MEMBER_NAME)
        self.assertEqual(identity.uid, support.MEMBER_UID)
        self.assertEqual(identity.role, ROLE_MEMBER)
        self.assertEqual(identity.auth_source, AUTH_SOURCE_LINUX)
        self.assertEqual(identity.host_id, support.SYNTHETIC_HOST_ID)
        self.assertIs(identity.account_valid, True)

    def test_a_granted_administrator_signs_in_as_one(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, support.ADMIN_NAME, support.GOOD_PASSWORD)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.identity.role, ROLE_ADMIN)

    def test_the_identity_carries_no_credential_material(self) -> None:
        provider, _, _ = self.build()
        identity = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD).identity
        rendered = repr(identity)
        self.assertNotIn(support.GOOD_PASSWORD, rendered)
        self.assertNotIn("password", rendered.lower())
        self.assertNotIn("hash", rendered.lower())

    def test_the_identity_does_not_echo_the_broker_reply(self) -> None:
        provider, _, _ = self.build()
        identity = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD).identity
        self.assertEqual(
            set(vars(identity)),
            {"login_name", "uid", "host_id", "role", "display_name", "account_valid", "auth_source"},
        )


class OrderingTests(Harness):
    """What must be refused *before* the Broker is consulted."""

    def assert_no_broker_call(self) -> None:
        self.assertEqual(self.transport.frames, [], "the broker was consulted")
        self.assertEqual(self.broker_pam.users_seen, [], "PAM was consulted")

    def test_a_malformed_name_is_refused_before_anything_else(self) -> None:
        provider, accounts, _ = self.build()
        for name in ("Root", "-carol", "carol\n", "carol/../root", "1carol", "", "a" * 40):
            with self.subTest(name=name):
                outcome = self.login(provider, name, support.GOOD_PASSWORD)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.reason, DenialReason.INVALID_NAME)
        self.assert_no_broker_call()

    def test_an_empty_password_is_refused_before_the_broker(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, support.MEMBER_NAME, "")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.EMPTY_PASSWORD)
        self.assert_no_broker_call()

    def test_a_non_string_password_is_refused(self) -> None:
        provider, _, _ = self.build()
        for password in (None, 123, b"x", ["x"]):
            with self.subTest(password=password):
                outcome = self.login(provider, support.MEMBER_NAME, password)  # type: ignore[arg-type]
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.reason, DenialReason.EMPTY_PASSWORD)
        self.assert_no_broker_call()

    def test_an_unknown_account_is_refused_without_asking_the_broker(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, "stranger", support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.UNKNOWN_USER)
        self.assert_no_broker_call()

    def test_root_is_refused_without_asking_the_broker(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, "root", support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.ROOT_FORBIDDEN)
        self.assert_no_broker_call()

    def test_a_service_account_is_refused_without_asking_the_broker(self) -> None:
        provider, _, _ = self.build()
        for name in ("daemon", "systemd-resolve"):
            with self.subTest(name=name):
                outcome = self.login(provider, name, support.GOOD_PASSWORD)
                self.assertFalse(outcome.ok)
                self.assertIn(
                    outcome.reason,
                    {DenialReason.EXPLICITLY_DENIED, DenialReason.SYSTEM_ACCOUNT,
                     DenialReason.NON_LOGIN_SHELL},
                )
        self.assert_no_broker_call()

    def test_an_account_outside_the_allowlist_is_refused_without_asking_the_broker(self) -> None:
        provider, accounts, _ = self.build()
        accounts.add(support.make_account(support.UNBOUND_NAME, 1700))
        outcome = self.login(provider, support.UNBOUND_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.NOT_ALLOWLISTED)
        self.assert_no_broker_call()


class CredentialTests(Harness):
    def test_a_wrong_password_is_refused(self) -> None:
        provider, _, _ = self.build()
        outcome = self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.INVALID_CREDENTIALS)
        self.assertEqual(self.broker_pam.users_seen, [support.MEMBER_NAME])

    def test_one_accounts_password_is_not_anothers(self) -> None:
        pam = support.default_pam()
        pam.table = {support.ADMIN_NAME: "a-different-synthetic-value"}
        provider, _, _ = self.build(pam=pam)
        self.assertFalse(
            self.login(provider, support.ADMIN_NAME, support.GOOD_PASSWORD).ok
        )

    def test_a_locked_account_is_refused_and_told_so_only_internally(self) -> None:
        from backend.auth_linux.pam_backend import PamOutcome

        pam = support.default_pam()
        pam.outcomes[support.MEMBER_NAME] = PamOutcome.ACCOUNT_LOCKED
        provider, _, _ = self.build(pam=pam)
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.ACCOUNT_LOCKED)
        # ...and the client learns only the generic refusal.
        self.assertIsInstance(outcome.to_client_error(), errors.InvalidCredentials)
        self.assertEqual(outcome.to_client_error().code, "invalid_credentials")

    def test_an_unavailable_broker_refuses_and_says_so(self) -> None:
        provider, _, _ = self.build(pam=UnavailablePamBackend())
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.BROKER_UNAVAILABLE)
        error = outcome.to_client_error()
        self.assertIsInstance(error, errors.PamUnavailable)
        self.assertEqual(error.http_status, 503)

    def test_an_absent_broker_refuses(self) -> None:
        client = BrokerClient(
            support.RejectingTransport(
                __import__("backend.auth_linux.protocol", fromlist=["x"]).BrokerUnreachable("gone")
            )
        )
        provider, _, _ = self.build(broker_client=client)
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.BROKER_UNAVAILABLE)

    def test_a_broker_reply_about_another_account_is_refused(self) -> None:
        """The confused-deputy case: a well-formed answer about the wrong person."""
        from backend.auth_linux.protocol import BrokerResponse, BrokerStatus
        from backend.auth_linux import protocol

        def responder(frame, timeout):
            request = protocol.decode_request(frame)
            return protocol.encode_response(
                BrokerResponse(
                    status=BrokerStatus.OK, uid=0, name="root", account_valid=True
                )
            )

        client = BrokerClient(support.InProcessTransport(responder))
        provider, _, _ = self.build(broker_client=client)
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.UID_MISMATCH)

    def test_a_success_reply_with_a_failed_account_check_is_refused(self) -> None:
        from backend.auth_linux import protocol
        from backend.auth_linux.protocol import BrokerResponse, BrokerStatus

        def responder(frame, timeout):
            return protocol.encode_response(
                BrokerResponse(
                    status=BrokerStatus.OK,
                    uid=support.MEMBER_UID,
                    name=support.MEMBER_NAME,
                    account_valid=False,
                )
            )

        provider, _, _ = self.build(
            broker_client=BrokerClient(support.InProcessTransport(responder))
        )
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.ACCOUNT_CHECK_FAILED)

    def test_a_broker_that_raises_is_refused(self) -> None:
        provider, _, _ = self.build(
            broker_client=BrokerClient(support.RejectingTransport(OSError("synthetic")))
        )
        self.assertFalse(self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD).ok)


class ThrottleTests(Harness):
    def test_repeated_failures_are_throttled_and_the_broker_is_left_alone(self) -> None:
        throttle = InMemoryThrottle(max_failures=2, window_seconds=60, lockout_seconds=300)
        provider, _, _ = self.build(provider_throttle=throttle)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        calls_before = len(self.transport.frames)
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, DenialReason.RATE_LIMITED)
        self.assertIsInstance(outcome.to_client_error(), errors.RateLimited)
        self.assertEqual(len(self.transport.frames), calls_before, "the broker was consulted")

    def test_a_success_clears_the_counter(self) -> None:
        throttle = InMemoryThrottle(max_failures=3, window_seconds=60, lockout_seconds=300)
        provider, _, _ = self.build(provider_throttle=throttle)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        # Two failures then a success: the counter is clear, so this is the
        # first failure of a new window rather than the third of an old one.
        outcome = self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.assertEqual(outcome.reason, DenialReason.INVALID_CREDENTIALS)

    def test_an_unknown_account_is_counted_too(self) -> None:
        """Otherwise a 429 would confirm that an account exists."""
        clock = support.FakeClock()
        throttle = InMemoryThrottle(max_failures=2, window_seconds=60, lockout_seconds=300, clock=clock)
        provider, _, _ = self.build(provider_throttle=throttle)
        self.login(provider, "stranger", support.GOOD_PASSWORD)
        self.login(provider, "stranger", support.GOOD_PASSWORD)
        outcome = self.login(provider, "stranger", support.GOOD_PASSWORD)
        self.assertEqual(outcome.reason, DenialReason.RATE_LIMITED)

    def test_throttle_keys_separate_clients(self) -> None:
        throttle = InMemoryThrottle(max_failures=1, window_seconds=60, lockout_seconds=300)
        provider, _, _ = self.build(provider_throttle=throttle)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD, client_key="a")
        outcome = self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD, client_key="b")
        self.assertTrue(outcome.ok)


class AvailabilityTests(Harness):
    def test_availability_reflects_configuration(self) -> None:
        provider, _, _ = self.build()
        self.assertTrue(provider.is_available())
        self.assertEqual(provider.name, "linux_pam")

    def test_a_disabled_deployment_is_not_offered(self) -> None:
        provider, _, _ = self.build(available=False)
        self.assertFalse(provider.is_available())


class AuditTests(Harness):
    def test_no_audit_record_contains_a_password(self) -> None:
        audit = ListAuditSink()
        provider, _, _ = self.build(audit=audit, pam=support.default_pam())
        self.login(provider, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.login(provider, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.login(provider, "root", support.GOOD_PASSWORD)
        self.login(provider, "stranger", support.GOOD_PASSWORD)
        rendered = str([event.to_dict() for event in audit.events])
        self.assertNotIn(support.GOOD_PASSWORD, rendered)
        self.assertNotIn(support.WRONG_PASSWORD, rendered)

    def test_the_reason_is_recorded_internally_even_when_the_client_learns_nothing(self) -> None:
        audit = ListAuditSink()
        provider, _, _ = self.build(audit=audit)
        self.login(provider, "root", support.GOOD_PASSWORD)
        self.assertIn(DenialReason.ROOT_FORBIDDEN, audit.reasons())


class OutcomeTests(unittest.TestCase):
    def test_a_successful_outcome_has_no_client_error(self) -> None:
        outcome = LoginOutcome(ok=True)
        with self.assertRaises(AssertionError):
            outcome.to_client_error()

    def test_every_failure_maps_to_a_refusal(self) -> None:
        for reason in sorted(__import__("backend.auth_linux.reasons", fromlist=["x"]).ALL_REASONS):
            with self.subTest(reason=reason):
                error = LoginOutcome(ok=False, reason=reason).to_client_error()
                self.assertNotEqual(error.http_status, 200)

    def test_an_outcome_without_a_reason_still_refuses_generically(self) -> None:
        error = LoginOutcome(ok=False, reason=None).to_client_error()
        self.assertIsInstance(error, errors.InvalidCredentials)

    def test_the_retry_hint_survives_the_mapping(self) -> None:
        outcome = LoginOutcome(ok=False, reason=DenialReason.RATE_LIMITED, retry_after=17)
        self.assertEqual(outcome.to_client_error().retry_after, 17)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
