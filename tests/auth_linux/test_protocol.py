"""The broker wire contract: framing, strict schema, and the password's path."""

from __future__ import annotations

import json
import unittest

from backend.auth_linux import protocol
from backend.auth_linux.protocol import (
    MAX_FRAME_BYTES,
    BrokerRequest,
    BrokerResponse,
    BrokerStatus,
    ProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    read_frame,
)

from . import support


def request(**overrides) -> BrokerRequest:
    base = {
        "user": support.MEMBER_NAME,
        "password": support.GOOD_PASSWORD,
        "request_id": "req-0001",
    }
    base.update(overrides)
    return BrokerRequest(**base)


class RequestRoundTripTests(unittest.TestCase):
    def test_a_well_formed_request_survives_a_round_trip(self) -> None:
        decoded = decode_request(encode_request(request()))
        self.assertEqual(decoded.user, support.MEMBER_NAME)
        self.assertEqual(decoded.password, support.GOOD_PASSWORD)
        self.assertEqual(decoded.request_id, "req-0001")

    def test_the_password_is_not_in_the_repr(self) -> None:
        """The likeliest way a password escapes is a traceback or a log line."""
        printed = repr(request())
        self.assertNotIn(support.GOOD_PASSWORD, printed)
        self.assertIn("<redacted>", printed)
        self.assertNotIn(support.GOOD_PASSWORD, str(request()))

    def test_the_frame_is_one_newline_terminated_line(self) -> None:
        frame = encode_request(request())
        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(frame.count(b"\n"), 1)


class RequestRejectionTests(unittest.TestCase):
    def test_unknown_keys_are_refused(self) -> None:
        """``service`` matters most: a caller must not choose the PAM stack."""
        frame = json.dumps(
            {
                "version": 1,
                "op": "verify",
                "user": support.MEMBER_NAME,
                "password": support.GOOD_PASSWORD,
                "request_id": "req-1",
                "service": "login",
            }
        ).encode()
        with self.assertRaises(ProtocolError):
            decode_request(frame)

    def test_missing_keys_are_refused(self) -> None:
        for dropped in ("version", "op", "user", "password", "request_id"):
            payload = {
                "version": 1,
                "op": "verify",
                "user": support.MEMBER_NAME,
                "password": support.GOOD_PASSWORD,
                "request_id": "req-1",
            }
            del payload[dropped]
            with self.subTest(dropped=dropped):
                with self.assertRaises(ProtocolError):
                    decode_request(json.dumps(payload).encode())

    def test_unsupported_versions_and_operations_are_refused(self) -> None:
        for payload in (
            {"version": 2, "op": "verify", "user": "alice", "password": "x", "request_id": "r"},
            {"version": 1, "op": "shell", "user": "alice", "password": "x", "request_id": "r"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError):
                    decode_request(json.dumps(payload).encode())

    def test_wrong_field_types_are_refused(self) -> None:
        for payload in (
            {"version": 1, "op": "verify", "user": 1, "password": "x", "request_id": "r"},
            {"version": 1, "op": "verify", "user": "alice", "password": None, "request_id": "r"},
            {"version": 1, "op": "verify", "user": "alice", "password": "x", "request_id": ""},
            {"version": 1, "op": "verify", "user": "", "password": "x", "request_id": "r"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError):
                    decode_request(json.dumps(payload).encode())

    def test_non_json_and_non_objects_are_refused(self) -> None:
        for frame in (b"", b"not json", b"[1,2,3]", b'"a string"', b"\xff\xfe"):
            with self.subTest(frame=frame):
                with self.assertRaises(ProtocolError):
                    decode_request(frame)

    def test_an_oversized_frame_is_refused(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_request(b"x" * (MAX_FRAME_BYTES + 1))

    def test_an_oversized_password_is_refused(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_request(request(password="x" * (protocol.MAX_PASSWORD_BYTES + 1)))

    def test_an_oversized_name_is_refused(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_request(request(user="a" * (protocol.MAX_LOGIN_NAME_LENGTH + 1)))


class ResponseTests(unittest.TestCase):
    def test_a_success_response_round_trips(self) -> None:
        response = BrokerResponse(
            status=BrokerStatus.OK, uid=support.MEMBER_UID, name=support.MEMBER_NAME,
            account_valid=True,
        )
        decoded = decode_response(encode_response(response))
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.uid, support.MEMBER_UID)
        self.assertEqual(decoded.name, support.MEMBER_NAME)
        self.assertIs(decoded.account_valid, True)

    def test_a_success_response_must_be_complete(self) -> None:
        for response in (
            BrokerResponse(status=BrokerStatus.OK, uid=None, name="alice", account_valid=True),
            BrokerResponse(status=BrokerStatus.OK, uid=4001, name=None, account_valid=True),
            BrokerResponse(status=BrokerStatus.OK, uid=4001, name="alice", account_valid=None),
            BrokerResponse(status=BrokerStatus.OK, uid=True, name="alice", account_valid=True),
        ):
            with self.subTest(response=response):
                with self.assertRaises(ProtocolError):
                    encode_response(response)

    def test_a_success_response_missing_a_field_will_not_decode(self) -> None:
        frame = json.dumps({"version": 1, "status": "ok", "uid": 4001}).encode()
        with self.assertRaises(ProtocolError):
            decode_response(frame)

    def test_a_failure_response_carries_nothing_but_a_status(self) -> None:
        """No field on a failure reply in which a credential could be volunteered."""
        for status in sorted(protocol.ALL_STATUSES - protocol.SUCCESS_STATUSES):
            with self.subTest(status=status):
                decoded = decode_response(encode_response(BrokerResponse(status=status)))
                self.assertFalse(decoded.ok)
                self.assertIsNone(decoded.uid)
                self.assertIsNone(decoded.name)

    def test_a_failure_response_with_extra_fields_is_refused(self) -> None:
        frame = json.dumps(
            {"version": 1, "status": "invalid_credentials", "detail": "user exists"}
        ).encode()
        with self.assertRaises(ProtocolError):
            decode_response(frame)

    def test_an_undefined_status_is_refused(self) -> None:
        frame = json.dumps({"version": 1, "status": "definitely_authenticated"}).encode()
        with self.assertRaises(ProtocolError):
            decode_response(frame)
        with self.assertRaises(ProtocolError):
            encode_response(BrokerResponse(status="not_a_status"))

    def test_a_response_cannot_smuggle_a_hash_or_a_token(self) -> None:
        frame = json.dumps(
            {
                "version": 1,
                "status": "ok",
                "uid": 4001,
                "name": "alice",
                "account_valid": True,
                "hash": "$6$abcdef",
            }
        ).encode()
        with self.assertRaises(ProtocolError):
            decode_response(frame)


class FramingTests(unittest.TestCase):
    def test_a_complete_frame_is_read(self) -> None:
        self.assertEqual(read_frame(support.FakeSocket(b'{"a":1}\n')), b'{"a":1}')

    def test_a_frame_split_across_reads_is_reassembled(self) -> None:
        sock = support.FakeSocket(b'{"a"')
        sock.inbound.extend(b':1}\n')
        self.assertEqual(read_frame(sock), b'{"a":1}')

    def test_a_peer_that_closes_early_is_unreachable(self) -> None:
        from backend.auth_linux.protocol import BrokerUnreachable

        with self.assertRaises(BrokerUnreachable):
            read_frame(support.FakeSocket(b'{"a":1}'))

    def test_trailing_data_after_the_frame_is_refused(self) -> None:
        """Otherwise a peer could append a second message we never look at."""
        with self.assertRaises(ProtocolError):
            read_frame(support.FakeSocket(b'{"a":1}\n{"b":2}\n'))

    def test_an_oversized_line_is_refused_without_buffering_it_all(self) -> None:
        sock = support.FakeSocket(b"x" * (MAX_FRAME_BYTES + 50))
        with self.assertRaises(ProtocolError):
            read_frame(sock)

    def test_a_read_error_is_reported_as_unreachable(self) -> None:
        from backend.auth_linux.protocol import BrokerUnreachable

        class Broken:
            def recv(self, bufsize):
                raise OSError("synthetic socket failure")

        with self.assertRaises(BrokerUnreachable):
            read_frame(Broken())

    def test_a_non_bytes_frame_is_refused(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_response("a string")  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
