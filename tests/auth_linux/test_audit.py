"""Audit records that refuse to hold credential material."""

from __future__ import annotations

import unittest

from backend.auth_linux.audit import AuditEvent, ListAuditSink, NullAuditSink
from backend.auth_linux.reasons import DenialReason

from . import support


class GuardTests(unittest.TestCase):
    def test_a_field_named_like_a_credential_is_refused(self) -> None:
        """The accident this prevents is a well-meaning debugging field."""
        for key in (
            "password",
            "Password",
            "admin_password",
            "passwd",
            "pwhash",
            "hash",
            "password_hash",
            "shadow",
            "secret",
            "client_secret",
            "token",
            "session_token",
            "credential",
            "credentials",
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    AuditEvent(event="linux_login", outcome="failure", fields={key: "x"})

    def test_ordinary_fields_are_accepted(self) -> None:
        event = AuditEvent(
            event="linux_login",
            outcome="rejected",
            reason=DenialReason.INVALID_CREDENTIALS,
            actor=support.MEMBER_NAME,
            host_id=support.SYNTHETIC_HOST_ID,
            fields={"client_key": "203.0.113.7", "peer_uid": 4001},
        )
        self.assertEqual(event.fields["peer_uid"], 4001)

    def test_the_record_is_immutable_once_built(self) -> None:
        event = AuditEvent(event="e", outcome="o", fields={"a": 1})
        with self.assertRaises(TypeError):
            event.fields["a"] = 2  # type: ignore[index]

    def test_the_record_does_not_keep_a_live_reference_to_the_input_mapping(self) -> None:
        source = {"a": 1}
        event = AuditEvent(event="e", outcome="o", fields=source)
        source["a"] = 2
        source["b"] = 3
        self.assertEqual(dict(event.fields), {"a": 1})


class SerialisationTests(unittest.TestCase):
    def test_to_dict_is_plain_json_shaped(self) -> None:
        event = AuditEvent(
            event="linux_login",
            outcome="rejected",
            reason=DenialReason.NOT_ALLOWLISTED,
            actor="stranger",
            host_id=support.SYNTHETIC_HOST_ID,
            fields={"client_key": "c"},
        )
        payload = event.to_dict()
        self.assertEqual(
            set(payload), {"event", "outcome", "reason", "actor", "host_id", "fields"}
        )
        self.assertEqual(payload["reason"], "not_allowlisted")
        self.assertIsInstance(payload["fields"], dict)

    def test_a_record_of_a_real_attempt_contains_no_password(self) -> None:
        event = AuditEvent(
            event="linux_login",
            outcome="rejected",
            reason=DenialReason.INVALID_CREDENTIALS,
            actor=support.MEMBER_NAME,
            fields={"client_key": "c"},
        )
        self.assertNotIn(support.GOOD_PASSWORD, str(event.to_dict()))


class SinkTests(unittest.TestCase):
    def test_the_list_sink_keeps_what_it_is_given(self) -> None:
        sink = ListAuditSink()
        sink.record(AuditEvent(event="a", outcome="b", reason=DenialReason.UNKNOWN_USER))
        sink.record(AuditEvent(event="c", outcome="d", reason=None))
        self.assertEqual(len(sink.events), 2)
        self.assertEqual(sink.reasons(), [DenialReason.UNKNOWN_USER, None])

    def test_the_null_sink_swallows_everything(self) -> None:
        sink = NullAuditSink()
        self.assertIsNone(sink.record(AuditEvent(event="a", outcome="b")))

    def test_both_sinks_satisfy_the_sink_protocol(self) -> None:
        for sink in (ListAuditSink(), NullAuditSink()):
            with self.subTest(sink=type(sink).__name__):
                self.assertTrue(callable(getattr(sink, "record")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
