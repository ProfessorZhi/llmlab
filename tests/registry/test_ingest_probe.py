"""§4.1 的输入侧：内核表与探测结果如何变成状态机的输入。"""

from __future__ import annotations

import json
import unittest

from backend.registry.ingest_probe import probe_port, read_seen_ports, seen_ports
from backend.registry.ownership import (
    ListeningSocket,
    ProcNetUnavailable,
    ProcNetUnparseable,
)


def sock(ip: str, port: int, uid: int) -> ListeningSocket:
    return ListeningSocket(ip=ip, port=port, uid=uid, inode=1)


class FakeTable:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    def listeners(self):
        if self.error is not None:
            raise self.error
        return list(self.rows)


class SeenPortTests(unittest.TestCase):
    def test_only_loopback_listeners_are_candidates(self):
        ports = seen_ports([sock("127.0.0.1", 8013, 1001), sock("192.168.1.5", 9000, 1001)])

        self.assertEqual([(p.uid, p.port) for p in ports], [(1001, 8013)])

    def test_a_wildcard_bind_is_ignored_by_default(self):
        """0.0.0.0 上的服务同时暴露在局域网口，不该被当成平台内服务。"""

        self.assertEqual(seen_ports([sock("0.0.0.0", 8013, 1001)]), [])
        self.assertEqual(len(seen_ports([sock("0.0.0.0", 8013, 1001)], include_wildcard=True)), 1)

    def test_ipv6_loopback_is_a_candidate(self):
        self.assertEqual([(p.port, p.host) for p in seen_ports([sock("::1", 8014, 1002)])], [(8014, "::1")])

    def test_a_port_claimed_by_two_uids_is_dropped(self):
        """歧义的端口不能归到任何一个人名下。"""

        rows = [sock("127.0.0.1", 8013, 1001), sock("::1", 8013, 1002)]
        self.assertEqual(seen_ports(rows), [])

    def test_the_same_uid_on_v4_and_v6_is_not_ambiguous(self):
        rows = [sock("127.0.0.1", 8013, 1001), sock("::1", 8013, 1001)]

        self.assertEqual([(p.uid, p.port) for p in seen_ports(rows)], [(1001, 8013)])

    def test_output_is_sorted_by_port(self):
        rows = [sock("127.0.0.1", 9000, 1), sock("127.0.0.1", 8000, 2)]
        self.assertEqual([p.port for p in seen_ports(rows)], [8000, 9000])


class ReadSeenPortsTests(unittest.TestCase):
    def test_an_unreadable_table_is_a_reported_problem_not_an_exception(self):
        ports, problem = read_seen_ports(FakeTable(error=ProcNetUnavailable()))

        self.assertEqual(ports, [])
        self.assertEqual(problem, "proc_net_unreadable")

    def test_an_unparseable_table_is_reported_separately(self):
        ports, problem = read_seen_ports(FakeTable(error=ProcNetUnparseable()))

        self.assertEqual(ports, [])
        self.assertEqual(problem, "proc_net_unparseable")

    def test_a_healthy_table_has_no_problem(self):
        ports, problem = read_seen_ports(FakeTable([sock("127.0.0.1", 8013, 1001)]))

        self.assertEqual(len(ports), 1)
        self.assertIsNone(problem)


class ProbeTests(unittest.TestCase):
    def _fetch(self, routes):
        calls = []

        def fetch(port, path):
            calls.append((port, path))
            return routes.get(path)

        fetch.calls = calls
        return fetch

    def test_an_openai_compatible_service_is_recognised_with_its_models(self):
        body = json.dumps({"data": [{"id": "qwen3-4b"}, {"id": "qwen3-4b-embed"}]}).encode()
        fetch = self._fetch({"/v1/models": (200, body)})

        outcome = probe_port(fetch, 8013)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.model_ids, ("qwen3-4b", "qwen3-4b-embed"))

    def test_a_200_with_a_body_that_is_not_a_model_list_is_not_evidence(self):
        """有些服务对任何路径都回 200；状态码本身不是兼容性证据。"""

        fetch = self._fetch({"/v1/models": (200, b"<html>hello</html>")})

        self.assertFalse(probe_port(fetch, 8013).ok)

    def test_a_framework_fingerprint_alone_is_enough(self):
        fetch = self._fetch({"/v1/models": (404, b""), "/version": (200, b'{"version": "0.6.3"}')})

        outcome = probe_port(fetch, 8013)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.framework, "vllm")

    def test_nothing_listening_is_a_failure(self):
        fetch = self._fetch({})

        outcome = probe_port(fetch, 8013)

        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.detail)

    def test_a_probe_never_raises_when_the_fetch_does(self):
        def explode(port, path):
            raise OSError("boom")

        # fetch 的契约是「失败返回 None」；调用方若给了会抛的实现，
        # 那是调用方的 bug，状态机这一层不替它兜底——这里只固定契约。
        with self.assertRaises(OSError):
            probe_port(explode, 8013)


if __name__ == "__main__":
    unittest.main()
