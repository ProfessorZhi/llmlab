"""The client half of the broker contract — where fail-closed is enforced."""

from __future__ import annotations

import unittest

from backend.auth_linux import protocol
from backend.auth_linux.broker_client import BrokerClient, BrokerVerification
from backend.auth_linux.protocol import BrokerResponse, BrokerStatus, ProtocolError
from backend.auth_linux.reasons import DenialReason

from . import support


def client_for(responder) -> BrokerClient:
    return BrokerClient(support.InProcessTransport(responder), request_id_factory=lambda: "req-test")


def respond(response: BrokerResponse) -> BrokerClient:
    return client_for(lambda frame, timeout: protocol.encode_response(response))


class SuccessTests(unittest.TestCase):
    def test_a_complete_success_is_believed(self) -> None:
        client = respond(
            BrokerResponse(
                status=BrokerStatus.OK, uid=support.MEMBER_UID, name=support.MEMBER_NAME,
                account_valid=True,
            )
        )
        result = client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertTrue(result.authenticated)
        self.assertEqual(result.uid, support.MEMBER_UID)
        self.assertEqual(result.name, support.MEMBER_NAME)
        self.assertIsNone(result.reason)

    def test_a_success_with_an_invalid_account_is_not_a_login(self) -> None:
        """``pam_acct_mgmt`` failing must not be overridden by ``pam_authenticate``."""
        client = respond(
            BrokerResponse(
                status=BrokerStatus.OK, uid=support.MEMBER_UID, name=support.MEMBER_NAME,
                account_valid=False,
            )
        )
        result = client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.ACCOUNT_CHECK_FAILED)


class StatusMappingTests(unittest.TestCase):
    EXPECTED = {
        BrokerStatus.INVALID_CREDENTIALS: DenialReason.INVALID_CREDENTIALS,
        BrokerStatus.ACCOUNT_LOCKED: DenialReason.ACCOUNT_LOCKED,
        BrokerStatus.ACCOUNT_EXPIRED: DenialReason.ACCOUNT_EXPIRED,
        BrokerStatus.PASSWORD_EXPIRED: DenialReason.PASSWORD_EXPIRED,
        BrokerStatus.ACCOUNT_DISABLED: DenialReason.ACCOUNT_DISABLED,
        BrokerStatus.ACCOUNT_CHECK_FAILED: DenialReason.ACCOUNT_CHECK_FAILED,
        BrokerStatus.USER_NOT_ALLOWED: DenialReason.NOT_ALLOWLISTED,
        BrokerStatus.EMPTY_PASSWORD: DenialReason.EMPTY_PASSWORD,
        BrokerStatus.RATE_LIMITED: DenialReason.RATE_LIMITED,
        BrokerStatus.INVALID_REQUEST: DenialReason.BROKER_PROTOCOL_ERROR,
        BrokerStatus.PAM_UNAVAILABLE: DenialReason.BROKER_UNAVAILABLE,
        BrokerStatus.INTERNAL_ERROR: DenialReason.BROKER_UNAVAILABLE,
    }

    def test_every_failure_status_maps_to_a_refusal(self) -> None:
        for status, reason in self.EXPECTED.items():
            with self.subTest(status=status):
                result = respond(BrokerResponse(status=status)).verify("alice", support.GOOD_PASSWORD)
                self.assertFalse(result.authenticated)
                self.assertEqual(result.reason, reason)

    def test_every_status_in_the_protocol_is_covered(self) -> None:
        unmapped = (protocol.ALL_STATUSES - protocol.SUCCESS_STATUSES) - set(self.EXPECTED)
        self.assertEqual(unmapped, frozenset())

    def test_an_internal_error_is_not_reported_as_bad_credentials(self) -> None:
        result = respond(BrokerResponse(status=BrokerStatus.INTERNAL_ERROR)).verify(
            "alice", support.GOOD_PASSWORD
        )
        self.assertEqual(result.reason, DenialReason.BROKER_UNAVAILABLE)


class FailClosedTests(unittest.TestCase):
    """Every way the conversation can go wrong ends in a refusal."""

    def test_an_unreachable_broker_refuses(self) -> None:
        client = BrokerClient(
            support.RejectingTransport(protocol.BrokerUnreachable("no socket"))
        )
        result = client.verify("alice", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_UNAVAILABLE)

    def test_a_protocol_violation_refuses_with_its_own_reason(self) -> None:
        client = BrokerClient(support.RejectingTransport(ProtocolError("trailing data")))
        result = client.verify("alice", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_PROTOCOL_ERROR)

    def test_a_transport_oserror_refuses(self) -> None:
        client = BrokerClient(support.RejectingTransport(OSError("synthetic")))
        self.assertFalse(client.verify("alice", support.GOOD_PASSWORD).authenticated)

    def test_a_malformed_reply_refuses(self) -> None:
        for payload in (b"", b"not json", b"[]", b'{"version":1}', b'{"version":1,"status":"ok"}'):
            with self.subTest(payload=payload):
                client = client_for(lambda frame, timeout, p=payload: p)
                result = client.verify("alice", support.GOOD_PASSWORD)
                self.assertFalse(result.authenticated)

    def test_an_undefined_status_in_a_reply_refuses(self) -> None:
        payload = b'{"version":1,"status":"authenticated_for_sure"}'
        result = client_for(lambda frame, timeout: payload).verify("alice", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)

    def test_an_oversized_reply_refuses(self) -> None:
        payload = b'{"version":1,"status":"ok","padding":"' + b"x" * 9000 + b'"}'
        result = client_for(lambda frame, timeout: payload).verify("alice", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_PROTOCOL_ERROR)

    def test_a_timeout_refuses(self) -> None:
        client = BrokerClient(
            support.RejectingTransport(protocol.BrokerUnreachable("timed out"))
        )
        self.assertFalse(client.verify("alice", support.GOOD_PASSWORD).authenticated)


class PasswordHandlingTests(unittest.TestCase):
    def test_the_password_is_put_on_the_wire_exactly_once_and_left_there(self) -> None:
        transport = support.InProcessTransport(
            lambda frame, timeout: protocol.encode_response(BrokerResponse(status=BrokerStatus.INVALID_CREDENTIALS))
        )
        client = BrokerClient(transport, request_id_factory=lambda: "req-test")
        client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertEqual(len(transport.frames), 1)
        sent = protocol.decode_request(transport.frames[0])
        self.assertEqual(sent.password, support.GOOD_PASSWORD)
        self.assertEqual(sent.user, support.MEMBER_NAME)

    def test_the_outgoing_buffer_is_wiped_after_the_round_trip(self) -> None:
        """The one copy we control is overwritten before the reply is read."""
        captured: dict[str, bytearray] = {}

        class CapturingTransport:
            def roundtrip(self, frame, *, timeout):
                captured["frame"] = frame
                return protocol.encode_response(BrokerResponse(status=BrokerStatus.INVALID_CREDENTIALS))

        client = BrokerClient(CapturingTransport(), request_id_factory=lambda: "req-test")
        client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)

        frame = captured["frame"]
        self.assertTrue(frame, "the frame should have been captured")
        self.assertEqual(set(frame), {0}, "the request buffer still holds the password")

    def test_an_unauthenticated_result_never_carries_an_identity(self) -> None:
        for status in sorted(protocol.ALL_STATUSES - protocol.SUCCESS_STATUSES):
            with self.subTest(status=status):
                result: BrokerVerification = respond(BrokerResponse(status=status)).verify(
                    "alice", support.GOOD_PASSWORD
                )
                self.assertFalse(result.authenticated)
                self.assertIsNone(result.uid)
                self.assertIsNone(result.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
