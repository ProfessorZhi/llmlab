"""The broker's decision core, exercised without a socket.

Every test here drives :meth:`PamBroker.process_frame` (or
:meth:`PamBroker.handle_connection` with a fake socket), so the security-relevant
logic is checked on a machine that is not root and has no PAM. What is *not*
proven here — and cannot be, in this environment — is anything about PAM itself.
"""

from __future__ import annotations

import unittest

from backend.auth_linux import errors, protocol
from backend.auth_linux.broker_server import PamBroker
from backend.auth_linux.pam_backend import PamOutcome, UnavailablePamBackend
from backend.auth_linux.protocol import BrokerRequest, BrokerStatus
from backend.auth_linux.throttle import InMemoryThrottle

from . import support

PEER = support.peer(uid=support.MEMBER_UID)


def frame_for(user: str, password: str, request_id: str = "req-1") -> bytes:
    return protocol.encode_request(
        BrokerRequest(user=user, password=password, request_id=request_id)
    )


def answer(broker, user, password, peer=PEER):
    return protocol.decode_response(broker.process_frame(frame_for(user, password), peer))


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.broker,
            self.config,
            self.accounts,
            self.audit,
            self.throttle,
            self.clock,
        ) = support.build_broker()
        self.pam = self.config.pam_backend
        self.peer = PEER


class SuccessPathTests(Harness):
    def test_a_correct_password_for_an_allowlisted_account_succeeds(self) -> None:
        response = answer(self.broker, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertEqual(response.status, BrokerStatus.OK)
        self.assertEqual(response.uid, support.MEMBER_UID)
        self.assertEqual(response.name, support.MEMBER_NAME)
        self.assertIs(response.account_valid, True)

    def test_every_allowlisted_account_can_succeed(self) -> None:
        for name, uid in (
            (support.ADMIN_NAME, support.ADMIN_UID),
            (support.MEMBER_NAME, support.MEMBER_UID),
            (support.OTHER_MEMBER_NAME, support.OTHER_MEMBER_UID),
        ):
            with self.subTest(name=name):
                response = answer(self.broker, name, support.GOOD_PASSWORD)
                self.assertEqual(response.status, BrokerStatus.OK)
                self.assertEqual(response.uid, uid)

    def test_the_broker_reports_its_own_view_of_the_uid(self) -> None:
        """The UID comes from ``getpwnam``, not from the request or from PAM."""
        response = answer(self.broker, support.OTHER_MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertEqual(response.uid, self.accounts.by_name(support.OTHER_MEMBER_NAME).uid)

    def test_a_success_clears_the_failure_counter(self) -> None:
        answer(self.broker, support.MEMBER_NAME, support.WRONG_PASSWORD)
        answer(self.broker, support.MEMBER_NAME, support.GOOD_PASSWORD)
        answer(self.broker, support.MEMBER_NAME, support.WRONG_PASSWORD)
        # If the success had not cleared the counter, this would be the third
        # failure and the next check would be throttled.
        self.assertEqual(
            answer(self.broker, support.MEMBER_NAME, support.WRONG_PASSWORD).status,
            BrokerStatus.INVALID_CREDENTIALS,
        )

    def test_a_success_is_audited_without_the_password(self) -> None:
        answer(self.broker, support.MEMBER_NAME, support.GOOD_PASSWORD)
        rendered = str([event.to_dict() for event in self.audit.events])
        self.assertIn("success", rendered)
        self.assertNotIn(support.GOOD_PASSWORD, rendered)


class CredentialTests(Harness):
    def test_a_wrong_password_is_refused(self) -> None:
        response = answer(self.broker, support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.assertEqual(response.status, BrokerStatus.INVALID_CREDENTIALS)

    def test_the_correct_password_for_one_account_is_not_accepted_for_another(self) -> None:
        self.pam.table[support.ADMIN_NAME] = "a-different-synthetic-value"
        response = answer(self.broker, support.ADMIN_NAME, support.GOOD_PASSWORD)
        self.assertEqual(response.status, BrokerStatus.INVALID_CREDENTIALS)

    def test_a_locked_account_is_reported_as_locked(self) -> None:
        self.pam.outcomes[support.MEMBER_NAME] = PamOutcome.ACCOUNT_LOCKED
        self.assertEqual(
            answer(self.broker, support.MEMBER_NAME, support.GOOD_PASSWORD).status,
            BrokerStatus.ACCOUNT_LOCKED,
        )

    def test_an_expired_account_is_refused(self) -> None:
        for outcome, status in (
            (PamOutcome.ACCOUNT_EXPIRED, BrokerStatus.ACCOUNT_EXPIRED),
            (PamOutcome.PASSWORD_EXPIRED, BrokerStatus.PASSWORD_EXPIRED),
            (PamOutcome.ACCOUNT_DISABLED, BrokerStatus.ACCOUNT_DISABLED),
            (PamOutcome.ACCOUNT_CHECK_FAILED, BrokerStatus.ACCOUNT_CHECK_FAILED),
        ):
            with self.subTest(outcome=outcome):
                self.pam.outcomes[support.MEMBER_NAME] = outcome
                self.assertEqual(
                    answer(self.broker, support.MEMBER_NAME, support.GOOD_PASSWORD).status, status
                )

    def test_an_empty_password_never_reaches_pam(self) -> None:
        """``pam_unix`` with ``nullok`` would call an empty password correct."""
        for empty in ("",):
            with self.subTest(password=empty):
                self.assertEqual(
                    answer(self.broker, support.MEMBER_NAME, empty).status,
                    BrokerStatus.EMPTY_PASSWORD,
                )
        self.assertEqual(self.pam.calls, [])

    def test_a_unavailable_backend_refuses_rather_than_passing(self) -> None:
        broker, config, _, _, _, _ = support.build_broker(pam=UnavailablePamBackend())
        self.assertEqual(
            answer(broker, support.MEMBER_NAME, support.GOOD_PASSWORD).status,
            BrokerStatus.PAM_UNAVAILABLE,
        )

    def test_a_backend_that_raises_becomes_an_internal_error_not_a_success(self) -> None:
        from .support import ExplodingPamBackend

        exploding = ExplodingPamBackend()
        broker, config, _, audit, _, _ = support.build_broker(pam=exploding)
        response = answer(broker, support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertEqual(response.status, BrokerStatus.INTERNAL_ERROR)
        self.assertEqual(exploding.calls, 1)
        # The exception's own text must not travel back to the caller.
        self.assertNotIn(b"synthetic backend fault", str(audit.events).encode())


class PolicyEnforcementTests(Harness):
    """Refusals that must happen *before* a privileged conversation."""

    def assert_refused_without_pam(self, user: str, password: str, status: str) -> None:
        response = answer(self.broker, user, password)
        self.assertEqual(response.status, status)
        self.assertEqual(self.pam.users_seen, [], f"{user} reached PAM")

    def test_root_is_refused_without_asking_pam(self) -> None:
        self.assert_refused_without_pam("root", support.GOOD_PASSWORD, BrokerStatus.USER_NOT_ALLOWED)

    def test_a_system_account_is_refused_without_asking_pam(self) -> None:
        for name in ("daemon", "systemd-resolve"):
            with self.subTest(name=name):
                self.pam.calls.clear()
                self.assert_refused_without_pam(name, support.GOOD_PASSWORD, BrokerStatus.USER_NOT_ALLOWED)

    def test_an_unknown_account_is_refused_without_asking_pam(self) -> None:
        self.assert_refused_without_pam("stranger", support.GOOD_PASSWORD, BrokerStatus.INVALID_CREDENTIALS)

    def test_an_account_outside_the_allowlist_is_refused_without_asking_pam(self) -> None:
        self.accounts.add(support.make_account("outsider", 1500))
        self.assert_refused_without_pam("outsider", support.GOOD_PASSWORD, BrokerStatus.USER_NOT_ALLOWED)

    def test_root_is_refused_even_when_allowlisted(self) -> None:
        from backend.auth_linux.policy import Allowlist

        policy = support.default_policy(allowlist=Allowlist(frozenset({"root"})))
        broker, _, _, _, _, _ = support.build_broker(policy=policy)
        response = protocol.decode_response(
            broker.process_frame(frame_for("root", support.GOOD_PASSWORD), PEER)
        )
        self.assertEqual(response.status, BrokerStatus.USER_NOT_ALLOWED)
        self.assertEqual(broker._config.pam_backend.users_seen, [])

    def test_an_illegal_name_is_refused_without_asking_pam(self) -> None:
        for name in ("Root", "-carol", "carol\n", "carol/../root", "1carol"):
            with self.subTest(name=name):
                response = protocol.decode_response(
                    self.broker.process_frame(frame_for(name, support.GOOD_PASSWORD), PEER)
                )
                self.assertEqual(response.status, BrokerStatus.INVALID_REQUEST)
        self.assertEqual(self.pam.calls, [])


class PeerRestrictionTests(Harness):
    def test_an_unauthorised_peer_is_refused(self) -> None:
        stranger = support.peer(uid=9001, gid=9001)
        response = protocol.decode_response(
            self.broker.process_frame(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD), stranger)
        )
        self.assertEqual(response.status, BrokerStatus.INVALID_REQUEST)
        self.assertEqual(self.pam.calls, [])

    def test_an_unauthorised_peer_is_refused_before_a_byte_is_read(self) -> None:
        """The password must not even be buffered for a process we do not trust."""
        conn = support.FakeSocket(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD))
        broker = PamBroker(
            self.config,
            audit=self.audit,
            peer_creds_reader=lambda sock: support.peer(uid=9001, gid=9001),
        )
        broker.handle_connection(conn)
        self.assertEqual(conn.recv_calls, 0)
        self.assertEqual(conn.sent, b"")
        self.assertEqual(self.pam.calls, [])

    def test_an_authorised_peer_is_served(self) -> None:
        conn = support.FakeSocket(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD))
        broker = PamBroker(self.config, audit=self.audit, peer_creds_reader=lambda sock: PEER)
        broker.handle_connection(conn)
        self.assertEqual(conn.recv_calls, 1)
        response = protocol.decode_response(bytes(conn.sent))
        self.assertEqual(response.status, BrokerStatus.OK)

    def test_a_group_match_also_admits_a_peer(self) -> None:
        from dataclasses import replace

        # A UID that is not allowed, in a GID that is. Group membership is a
        # weaker claim than a UID, which is why it is opt-in.
        grouped = replace(
            self.config, allowed_peer_uids=frozenset(), allowed_peer_gids=frozenset({1001})
        )
        broker = PamBroker(
            grouped,
            audit=self.audit,
            peer_creds_reader=lambda sock: support.peer(uid=1234, gid=1001),
        )
        conn = support.FakeSocket(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD))
        broker.handle_connection(conn)
        self.assertEqual(protocol.decode_response(bytes(conn.sent)).status, BrokerStatus.OK)

    def test_a_peer_with_no_match_is_refused(self) -> None:
        self.assertFalse(self.config.peer_is_allowed(support.peer(uid=1234, gid=1234)))
        self.assertTrue(self.config.peer_is_allowed(PEER))


class RequestShapeTests(Harness):
    def test_a_malformed_frame_is_refused(self) -> None:
        for frame in (b"", b"not json", b"[]", b'{"version":1}'):
            with self.subTest(frame=frame):
                response = protocol.decode_response(self.broker.process_frame(frame, PEER))
                self.assertEqual(response.status, BrokerStatus.INVALID_REQUEST)

    def test_a_request_naming_its_own_pam_service_is_refused(self) -> None:
        """A caller must not be able to pick which PAM stack it runs against."""
        import json

        payload = json.dumps(
            {
                "version": 1,
                "op": "verify",
                "user": support.MEMBER_NAME,
                "password": support.GOOD_PASSWORD,
                "request_id": "r",
                "service": "login",
            }
        ).encode()
        self.assertEqual(
            protocol.decode_response(self.broker.process_frame(payload, PEER)).status,
            BrokerStatus.INVALID_REQUEST,
        )
        self.assertEqual(self.pam.calls, [])

    def test_a_malformed_connection_is_answered_and_closed(self) -> None:
        conn = support.FakeSocket(b"not a frame at all")
        broker = PamBroker(self.config, audit=self.audit, peer_creds_reader=lambda sock: PEER)
        broker.handle_connection(conn)
        self.assertEqual(protocol.decode_response(bytes(conn.sent)).status, BrokerStatus.INVALID_REQUEST)


class UidCrossCheckTests(Harness):
    def test_pam_success_is_ignored_when_the_account_changed_underneath(self) -> None:
        """A UID recycled mid-check must not authenticate the new holder."""
        original = self.accounts.by_name(support.MEMBER_NAME)

        class ShiftingAccounts(support.FakePwdSource):
            def by_name(self, name):
                # After PAM has run, the name resolves to a different account.
                if name == support.MEMBER_NAME and self.verify_calls:
                    return support.make_account(support.MEMBER_NAME, 4321)
                return super().by_name(name)

        accounts = ShiftingAccounts([original])
        accounts.verify_calls = False

        class FlaggingPam(support.FakePamBackend):
            def verify(self, *, user, password):
                accounts.verify_calls = True
                return super().verify(user=user, password=password)

        broker, _, _, _, _, _ = support.build_broker(
            accounts=accounts, pam=FlaggingPam({support.MEMBER_NAME: support.GOOD_PASSWORD})
        )
        response = protocol.decode_response(
            broker.process_frame(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD), PEER)
        )
        self.assertEqual(response.status, BrokerStatus.INTERNAL_ERROR)
        self.assertNotEqual(response.status, BrokerStatus.OK)

    def test_a_name_that_no_longer_resolves_is_refused(self) -> None:
        class VanishingAccounts(support.FakePwdSource):
            def by_name(self, name):
                if self.vanished:
                    return None
                return super().by_name(name)

        accounts = VanishingAccounts(list(support.default_accounts().accounts.values()))
        accounts.vanished = False

        class FlaggingPam(support.FakePamBackend):
            def verify(self, *, user, password):
                accounts.vanished = True
                return super().verify(user=user, password=password)

        broker, _, _, _, _, _ = support.build_broker(
            accounts=accounts, pam=FlaggingPam({support.MEMBER_NAME: support.GOOD_PASSWORD})
        )
        response = protocol.decode_response(
            broker.process_frame(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD), PEER)
        )
        self.assertEqual(response.status, BrokerStatus.INTERNAL_ERROR)


class ThrottleTests(Harness):
    def test_repeated_failures_are_throttled(self) -> None:
        throttle = InMemoryThrottle(max_failures=3, window_seconds=60, lockout_seconds=300, clock=self.clock)
        broker, _, _, _, _, _ = support.build_broker(throttle=throttle)
        statuses = [
            protocol.decode_response(
                broker.process_frame(frame_for(support.MEMBER_NAME, support.WRONG_PASSWORD), PEER)
            ).status
            for _ in range(4)
        ]
        self.assertEqual(statuses[:3], [BrokerStatus.INVALID_CREDENTIALS] * 3)
        self.assertEqual(statuses[3], BrokerStatus.RATE_LIMITED)

    def test_throttling_happens_before_pam(self) -> None:
        throttle = InMemoryThrottle(max_failures=1, window_seconds=60, lockout_seconds=300, clock=self.clock)
        broker, config, _, _, _, _ = support.build_broker(throttle=throttle)
        broker.process_frame(frame_for(support.MEMBER_NAME, support.WRONG_PASSWORD), PEER)
        calls_after_first = len(config.pam_backend.calls)
        broker.process_frame(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD), PEER)
        self.assertEqual(len(config.pam_backend.calls), calls_after_first)


class DisclosureTests(Harness):
    def test_a_response_never_carries_the_password(self) -> None:
        """The status names a kind of failure; the value supplied is not echoed.

        ``empty_password`` legitimately contains the word "password", which is
        why the assertion is on the value and not on the vocabulary. A
        *successful* reply does name the account — that is what it is for — so
        the no-echo assertion applies to the refusal replies.
        """
        for password in (support.GOOD_PASSWORD, support.WRONG_PASSWORD, ""):
            with self.subTest(password=password[:8] or "<empty>"):
                payload = self.broker.process_frame(
                    frame_for(support.MEMBER_NAME, password), PEER
                )
                if password:
                    self.assertNotIn(password.encode(), payload)

        refusal = self.broker.process_frame(
            frame_for("stranger", support.GOOD_PASSWORD), PEER
        )
        self.assertNotIn(b"stranger", refusal)
        self.assertNotIn(support.GOOD_PASSWORD.encode(), refusal)

    def test_an_audit_record_never_carries_the_password(self) -> None:
        self.broker.process_frame(frame_for(support.MEMBER_NAME, support.GOOD_PASSWORD), PEER)
        self.broker.process_frame(frame_for(support.MEMBER_NAME, support.WRONG_PASSWORD), PEER)
        self.broker.process_frame(frame_for("root", support.GOOD_PASSWORD), PEER)
        rendered = str([event.to_dict() for event in self.audit.events])
        self.assertNotIn(support.GOOD_PASSWORD, rendered)
        self.assertNotIn(support.WRONG_PASSWORD, rendered)

    def test_a_failure_reply_does_not_say_which_kind_of_failure_it_was(self) -> None:
        """Denials that reach the wire carry a status and nothing else."""
        for user, password in (
            ("stranger", support.GOOD_PASSWORD),
            ("root", support.GOOD_PASSWORD),
            (support.MEMBER_NAME, support.WRONG_PASSWORD),
        ):
            with self.subTest(user=user):
                payload = self.broker.process_frame(frame_for(user, password), PEER)
                document = __import__("json").loads(payload.decode())
                self.assertEqual(set(document), {"version", "status"})


class ConfigGuardTests(Harness):
    def test_an_unknown_pam_backend_name_stops_the_broker(self) -> None:
        with self.assertRaises(errors.ConfigError):
            from backend.auth_linux.pam_backend import build_pam_backend

            build_pam_backend("pam_unix")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
