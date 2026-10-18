"""End-to-end over a real Unix domain socket.

This is the closest thing in the suite to the deployed shape: a listening
socket, kernel-reported peer credentials, a frame written and read across the
boundary, and a decision coming back. It runs unprivileged, and it uses the
running user as the authorised peer — which is exactly the check the deployment
will perform against the portal's service account.

What it does not do, and cannot do here, is involve PAM. The backend behind the
socket is a synthetic one, so a green run says the *plumbing* and the *policy*
work; it says nothing about whether any real Linux password would be accepted.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from backend.auth_linux import errors
from backend.auth_linux.audit import ListAuditSink
from backend.auth_linux.broker_client import BrokerClient, UnixSocketTransport
from backend.auth_linux.broker_server import PamBroker, socket_peer_creds
from backend.auth_linux.protocol import BrokerStatus
from backend.auth_linux.reasons import DenialReason

from . import support


class SocketPermissionTests(unittest.TestCase):
    """The checks that run before the socket exists."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "broker.sock"
        self.uid = os.getuid()
        self.account_source = support.default_accounts()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_broker(self, *, path: Path | None = None, dir_owner: int | None = None) -> PamBroker:
        broker, _, _, _, _, _ = support.build_broker(
            socket_path=str(path or self.path),
            accounts=self.account_source,
        )
        config = broker._config
        updated = replace(
            config,
            require_socket_owner_uid=self.uid,
            require_socket_dir_owner_uid=self.uid if dir_owner is None else dir_owner,
        )
        return PamBroker(updated, audit=ListAuditSink())

    def test_a_socket_in_a_private_directory_is_created_with_a_private_mode(self) -> None:
        broker = self.make_broker()
        try:
            broker.prepare_listener()
            mode = os.stat(self.path).st_mode & 0o777
            self.assertEqual(mode, 0o600, f"socket mode is {mode:04o}")
        finally:
            broker.stop()

    def test_a_world_writable_directory_is_refused(self) -> None:
        os.chmod(self.dir, 0o777)
        try:
            with self.assertRaises(errors.ConfigError):
                self.make_broker().prepare_listener()
        finally:
            os.chmod(self.dir, 0o700)

    def test_a_directory_owned_by_someone_else_is_refused(self) -> None:
        with self.assertRaises(errors.ConfigError):
            self.make_broker(dir_owner=self.uid + 1).prepare_listener()

    def test_an_existing_non_socket_path_is_refused_rather_than_deleted(self) -> None:
        """The broker must not remove a file it did not create."""
        self.path.write_text("an important file", encoding="utf-8")
        with self.assertRaises(errors.ConfigError):
            self.make_broker().prepare_listener()
        self.assertTrue(self.path.exists())

    def test_a_stale_socket_is_replaced(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        broker = self.make_broker()
        try:
            broker.prepare_listener()
            self.assertTrue(self.path.exists())
        finally:
            broker.stop()
            stale.close()


class PeerCredentialTests(unittest.TestCase):
    def test_real_peer_credentials_report_the_connecting_user(self) -> None:
        """``SO_PEERCRED`` is the reason the socket can be restricted at all."""
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            creds = socket_peer_creds(left)
            self.assertEqual(creds.uid, os.getuid())
            self.assertEqual(creds.gid, os.getgid())
            self.assertGreater(creds.pid, 0)
        finally:
            left.close()
            right.close()


class LiveSocketTests(unittest.TestCase):
    """A broker actually listening, talked to by a real client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "broker.sock"
        self.uid = os.getuid()

        broker, self.config, self.accounts, self.audit, _, _ = support.build_broker(
            socket_path=str(self.path)
        )
        self.pam = self.config.pam_backend
        self.broker = PamBroker(
            replace(
                self.config,
                allowed_peer_uids=frozenset({self.uid}),
                require_socket_owner_uid=self.uid,
                require_socket_dir_owner_uid=self.uid,
            ),
            audit=self.audit,
        )
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=lambda: self.broker.serve_forever(on_ready=lambda listener: self.ready.set()),
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.ready.wait(5.0), "the broker never started listening")

        self.client = BrokerClient(UnixSocketTransport(self.path), timeout=5.0)

    def tearDown(self) -> None:
        self.broker.stop()
        self.thread.join(5.0)
        self._tmp.cleanup()

    def test_a_correct_password_authenticates_across_the_socket(self) -> None:
        result = self.client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertTrue(result.authenticated)
        self.assertEqual(result.uid, support.MEMBER_UID)
        self.assertEqual(result.name, support.MEMBER_NAME)

    def test_a_wrong_password_is_refused_across_the_socket(self) -> None:
        result = self.client.verify(support.MEMBER_NAME, support.WRONG_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.INVALID_CREDENTIALS)

    def test_an_empty_password_is_refused_across_the_socket(self) -> None:
        result = self.client.verify(support.MEMBER_NAME, "")
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.EMPTY_PASSWORD)

    def test_an_account_outside_the_allowlist_is_refused_across_the_socket(self) -> None:
        self.accounts.add(support.make_account("dana-unlisted", 1600))
        result = self.client.verify("dana-unlisted", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.NOT_ALLOWLISTED)

    def test_root_is_refused_across_the_socket(self) -> None:
        result = self.client.verify("root", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.NOT_ALLOWLISTED)

    def test_the_password_never_appears_in_the_broker_audit(self) -> None:
        self.client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.client.verify(support.MEMBER_NAME, support.WRONG_PASSWORD)
        rendered = str([event.to_dict() for event in self.audit.events])
        self.assertNotIn(support.GOOD_PASSWORD, rendered)
        self.assertNotIn(support.WRONG_PASSWORD, rendered)

    def test_many_attempts_do_not_desynchronise_the_protocol(self) -> None:
        """One frame each way, every time — no drift across a hundred calls."""
        for _ in range(100):
            result = self.client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
            self.assertTrue(result.authenticated)
        self.assertEqual(self.pam.users_seen, [support.MEMBER_NAME] * 100)


class AbsentBrokerTests(unittest.TestCase):
    """The state this deployment is actually in until the broker is installed."""

    def test_an_absent_broker_is_unavailable_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = BrokerClient(UnixSocketTransport(Path(tmp) / "nothing-here.sock"))
            result = client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_UNAVAILABLE)
        self.assertEqual(result.status, BrokerStatus.PAM_UNAVAILABLE)

    def test_an_absent_broker_refuses_even_the_most_privileged_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = BrokerClient(UnixSocketTransport(Path(tmp) / "nothing-here.sock"))
            result = client.verify("carol", support.GOOD_PASSWORD)
        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_UNAVAILABLE)

    def test_a_path_that_is_a_directory_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = BrokerClient(UnixSocketTransport(Path(tmp)))
            self.assertFalse(client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD).authenticated)


class LivenessTests(unittest.TestCase):
    """A serial, privileged component must not be stallable by one client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "broker.sock"
        self.uid = os.getuid()
        broker, self.config, _, self.audit, _, _ = support.build_broker(
            socket_path=str(self.path)
        )
        self.broker = PamBroker(
            replace(
                self.config,
                allowed_peer_uids=frozenset({self.uid}),
                require_socket_owner_uid=self.uid,
                require_socket_dir_owner_uid=self.uid,
                accept_poll_seconds=0.1,
                connection_timeout_seconds=0.4,
            ),
            audit=self.audit,
        )
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=lambda: self.broker.serve_forever(on_ready=lambda listener: self.ready.set()),
            daemon=True,
        )
        self.thread.start()
        self.assertTrue(self.ready.wait(5.0))

    def tearDown(self) -> None:
        self.broker.stop()
        self.thread.join(5.0)
        self._tmp.cleanup()

    def test_stop_shuts_the_loop_down_promptly(self) -> None:
        import time as time_module

        start = time_module.perf_counter()
        self.broker.stop()
        self.thread.join(5.0)
        elapsed = time_module.perf_counter() - start
        self.assertFalse(self.thread.is_alive(), "the accept loop outlived stop()")
        self.assertLess(elapsed, 3.0, f"shutdown took {elapsed:.1f}s")

    def test_a_client_that_says_nothing_does_not_stall_the_next_one(self) -> None:
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.connect(str(self.path))
        try:
            # Hold the connection open without sending a frame. The broker
            # should give up on it and serve the next caller.
            client = BrokerClient(UnixSocketTransport(self.path), timeout=5.0)
            result = client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
            self.assertTrue(result.authenticated, "a silent peer blocked a real login")
        finally:
            silent.close()


class SlowPeerTests(unittest.TestCase):
    def test_a_peer_that_answers_with_nonsense_is_refused_not_hung(self) -> None:
        """A socket that accepts and then misbehaves must not stall a login."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "liar.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            try:
                def serve():
                    conn, _ = listener.accept()
                    with conn:
                        conn.recv(4096)
                        conn.sendall(b"this is not a frame\n")

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                client = BrokerClient(UnixSocketTransport(path), timeout=5.0)
                result = client.verify(support.MEMBER_NAME, support.GOOD_PASSWORD)
                thread.join(5.0)
            finally:
                listener.close()

        self.assertFalse(result.authenticated)
        self.assertEqual(result.reason, DenialReason.BROKER_PROTOCOL_ERROR)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
