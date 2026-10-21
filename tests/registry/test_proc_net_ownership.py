"""``/proc/net/tcp(6)`` 所有权 prover：不需要 root 的 uid 证明。

**本文件的主体只用注入的 fixture 表。** 那些表的形状照抄真实内核的输出
（表头、``sl:`` 序号、按主机字节序打印的地址、``uid`` / ``inode`` 列、尾部
各种计时字段），覆盖：IPv4 ``127.0.0.1:18009`` 属 uid 1001、通配、IPv6 ``::1``、
IPv4-mapped、他人 uid 冲突、非 LISTEN 状态。

唯一碰真实内核的是 :class:`RealProcNetSmokeTest`：在**本进程自己**开的一个
回环监听 socket 上验证「读得到、认得出、属于我」。它证明的是解析器认得这台
机器上真实的表，**不是**「Dell 上的真实引擎已经验证过」—— 那条仍待做。
"""

from __future__ import annotations

import os
import socket
import unittest

from backend.registry.errors import InvalidRequest
from backend.registry.health import (
    PROTOCOL_UNKNOWN,
    PROTOCOL_UNVERIFIED,
    REASON_TRANSPORT_UNREACHABLE,
    NoProtocolProber,
    ProtocolObservation,
)
from backend.registry.ownership import (
    METHOD_PROC_NET,
    OWNERSHIP_PROVER_NONE,
    OWNERSHIP_PROVER_PROC_NET,
    OWNERSHIP_UID_VERIFIED,
    OWNERSHIP_UNVERIFIED,
    REASON_AMBIGUOUS,
    REASON_NO_OWNERSHIP_SOURCE,
    REASON_NOT_LISTENING,
    REASON_PROC_NET_UNPARSEABLE,
    REASON_PROC_NET_UNREADABLE,
    REASON_TARGET_NOT_LOOPBACK,
    REASON_UNKNOWN_USER,
    ListeningSocket,
    NoOwnershipProver,
    ProcNetListenerTable,
    ProcNetOwnershipProver,
    ProcNetUnparseable,
    StaticOwnershipProver,
    build_ownership_prover,
    decode_proc_net_address,
    listeners_serving,
    parse_proc_net_tcp,
    unverified_proof,
)
from backend.registry import preflight as preflight_mod
from backend.registry.registry import RegistryConfig
from backend.registry.store import count_rows
from backend.registry.urls import NormalizedTarget, validate_engine_target_url
from tests.registry.support import HEALTHY_PORT, UNREACHABLE_PORT, Harness, close_all

PREFIX = "/api/v2/registry"

#: fixture 里用到的端口（都不在缺省 allowlist 里，所以构造 target 时显式放开）。
FIXTURE_PORTS = frozenset(range(18009, 18020))

#: 真实形状的 ``/proc/net/tcp``。注意地址是**主机字节序**：127.0.0.1 -> 0100007F。
#:
#: * 18009 (4659) —— 127.0.0.1 LISTEN，uid 1001；另有一条**他人** uid 1002 的
#:   ESTABLISHED（01）连接也落在 18009 本地端口上 —— 它不是 LISTEN，必须被忽略；
#: * 18010 (465A) —— 0.0.0.0 LISTEN，uid 1001（通配）；
#: * 18011 (465B) —— 127.0.0.1 LISTEN，uid 1002（与 tcp6 里 uid 1003 的 :: 冲突）；
#: * 18012 (465C) —— 只有 TIME_WAIT（06），没有 LISTEN；
#: * 18016 (4660) —— 127.0.0.1 LISTEN，uid 1002；
#: * 18017 (4661) —— 两条 SO_REUSEPORT LISTEN，同为 uid 1001；
#: * 18018 (4662) —— 127.0.0.1 uid 1001，外加一条绑在网卡地址 192.168.1.10
#:   （0A01A8C0）上的 uid 1002 —— 后者接不到回环连接，不算。
PROC_NET_TCP_FIXTURE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:4659 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 41001 1 0000000000000000 100 0 0 10 0
   1: 00000000:465A 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 41002 1 0000000000000000 100 0 0 10 0
   2: 0100007F:465B 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1002        0 41003 1 0000000000000000 100 0 0 10 0
   3: 0100007F:4659 0100007F:C350 01 00000000:00000000 02:000A3B1C 00000000  1002        0 41004 2 0000000000000000 20 4 30 10 -1
   4: 0100007F:465C 0100007F:C351 06 00000000:00000000 03:00001770 00000000     0        0 0 3 0000000000000000
   5: 0100007F:4660 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1002        0 41006 1 0000000000000000 100 0 0 10 0
   6: 0100007F:4661 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 41007 1 0000000000000000 100 0 0 10 0
   7: 0100007F:4661 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 41008 1 0000000000000000 100 0 0 10 0
   8: 0100007F:4662 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 41009 1 0000000000000000 100 0 0 10 0
   9: 0A01A8C0:4662 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1002        0 41010 1 0000000000000000 100 0 0 10 0
"""

#: 真实形状的 ``/proc/net/tcp6``。每个 32 位字各自按主机字节序打印。
#:
#: * 18013 (465D) —— ``::1`` LISTEN，uid 1001；
#: * 18014 (465E) —— ``::`` LISTEN，uid 1001（双栈通配）；
#: * 18015 (465F) —— ``::ffff:127.0.0.1`` LISTEN，uid 1001（mapped）；
#: * 18011 (465B) —— ``::`` LISTEN，uid 1003（与 tcp 里的 uid 1002 冲突 -> ambiguous）；
#: * 18019 (4663) —— ``::1`` 上一条 CLOSE_WAIT（08），不是 LISTEN。
PROC_NET_TCP6_FIXTURE = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:465D 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 61001 1 0000000000000000 100 0 0 10 0
   1: 00000000000000000000000000000000:465E 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 61002 1 0000000000000000 100 0 0 10 0
   2: 0000000000000000FFFF00000100007F:465F 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 61003 1 0000000000000000 100 0 0 10 0
   3: 00000000000000000000000000000000:465B 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1003        0 61004 1 0000000000000000 100 0 0 10 0
   4: 00000000000000000000000001000000:4663 00000000000000000000000001000000:D431 08 00000000:00000001 00:00000000 00000000  1001        0 61005 1 0000000000000000 20 4 0 10 -1
"""

#: 受信目录之外的**本机**账号表（``pwd`` 的替身）。刻意与 Harness 的合成 uid
#: （alice=4001 …）**不同**：证明 prover 用的是 ``pwd`` 的答案，而不是
#: ``RequesterContext.uid`` 或任何请求里的数。
HOST_ACCOUNTS = {"alice": 1001, "carol": 1002, "bob": 1003}


def resolver(username: str) -> int:
    return HOST_ACCOUNTS[username]


def fixture_table() -> ProcNetListenerTable:
    return ProcNetListenerTable.from_texts(tcp=PROC_NET_TCP_FIXTURE, tcp6=PROC_NET_TCP6_FIXTURE)


def fixture_prover() -> ProcNetOwnershipProver:
    return ProcNetOwnershipProver(table=fixture_table(), user_resolver=resolver)


def target(port: int, host: str = "127.0.0.1") -> NormalizedTarget:
    literal = f"[{host}]" if ":" in host else host
    return validate_engine_target_url(
        f"http://{literal}:{port}/v1", allowed_ports=FIXTURE_PORTS | {HEALTHY_PORT}
    )


def tearDownModule() -> None:
    close_all()


class DecodeAddressTest(unittest.TestCase):
    def test_ipv4_is_printed_in_host_byte_order(self) -> None:
        self.assertEqual(decode_proc_net_address("0100007F:4659"), ("127.0.0.1", 18009))
        self.assertEqual(decode_proc_net_address("00000000:465A"), ("0.0.0.0", 18010))
        self.assertEqual(decode_proc_net_address("0A01A8C0:4662"), ("192.168.1.10", 18018))

    def test_ipv6_loopback_wildcard_and_mapped(self) -> None:
        self.assertEqual(
            decode_proc_net_address("00000000000000000000000001000000:465D"), ("::1", 18013)
        )
        self.assertEqual(
            decode_proc_net_address("00000000000000000000000000000000:465E"), ("::", 18014)
        )
        self.assertEqual(
            decode_proc_net_address("0000000000000000FFFF00000100007F:465F"),
            ("::ffff:127.0.0.1", 18015),
        )

    def test_malformed_fields_are_refused(self) -> None:
        for bad in ("0100007F", "0100007F:46", "01007F:4659", "ZZ00007F:4659", "a:b:c"):
            with self.subTest(field=bad):
                with self.assertRaises(ValueError):
                    decode_proc_net_address(bad)


class ParseTableTest(unittest.TestCase):
    def test_only_listen_rows_survive_and_columns_are_read(self) -> None:
        sockets = parse_proc_net_tcp(PROC_NET_TCP_FIXTURE)
        self.assertIn(ListeningSocket("127.0.0.1", 18009, 1001, 41001), sockets)
        self.assertIn(ListeningSocket("0.0.0.0", 18010, 1001, 41002), sockets)
        # ESTABLISHED（41004）与 TIME_WAIT（inode 0）都不是 LISTEN。
        self.assertNotIn(41004, [item.inode for item in sockets])
        self.assertNotIn(18012, [item.port for item in sockets])
        self.assertEqual(len(sockets), 8)

    def test_a_row_is_a_plain_tuple_of_ip_port_uid_inode(self) -> None:
        first = parse_proc_net_tcp(PROC_NET_TCP_FIXTURE)[0]
        ip, port, uid, inode = first
        self.assertEqual((ip, port, uid, inode), ("127.0.0.1", 18009, 1001, 41001))

    def test_ipv6_table(self) -> None:
        sockets = parse_proc_net_tcp(PROC_NET_TCP6_FIXTURE)
        self.assertEqual(
            sorted((item.ip, item.port, item.uid) for item in sockets),
            [
                ("::", 18011, 1003),
                ("::", 18014, 1001),
                ("::1", 18013, 1001),
                ("::ffff:127.0.0.1", 18015, 1001),
            ],
        )

    def test_header_and_blank_lines_are_ignored(self) -> None:
        self.assertEqual(parse_proc_net_tcp(""), [])
        header_only = PROC_NET_TCP_FIXTURE.splitlines()[0] + "\n\n"
        self.assertEqual(parse_proc_net_tcp(header_only), [])

    def test_an_unreadable_row_fails_closed_instead_of_being_skipped(self) -> None:
        # 被跳过的那一行恰好可能是另一个 uid 的监听者。
        for bad in (
            "   0: 0100007F:4659 00000000:0000 0A 00000000:00000000\n",
            "   0: 0100007F:4659 00000000:0000 0A 00000000:00000000 00:00000000 00000000  -1 0 41001\n",
            "   0: 0100007F:4659 00000000:0000 XYZ 00000000:00000000 00:00000000 00000000  1001 0 41001\n",
            "   0: nonsense:4659 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001 0 41001\n",
        ):
            with self.subTest(row=bad):
                with self.assertRaises(ProcNetUnparseable):
                    parse_proc_net_tcp(PROC_NET_TCP_FIXTURE + bad)


class ListenerTableTest(unittest.TestCase):
    def test_both_tables_are_merged(self) -> None:
        sockets = fixture_table().listeners()
        self.assertEqual(len(sockets), 8 + 4)

    def test_a_missing_tcp6_means_ipv6_is_off_not_unreadable(self) -> None:
        sockets = ProcNetListenerTable.from_texts(tcp=PROC_NET_TCP_FIXTURE).listeners()
        self.assertEqual(len(sockets), 8)

    def test_both_missing_is_unreadable(self) -> None:
        proof = ProcNetOwnershipProver(
            table=ProcNetListenerTable.from_texts(), user_resolver=resolver
        ).prove(target(18009), owner_linux_user="alice")
        self.assertEqual(proof.reason, REASON_PROC_NET_UNREADABLE)
        self.assertFalse(proof.verified)

    def test_a_permission_error_is_unreadable_not_empty(self) -> None:
        def reader(path: str) -> str:
            raise PermissionError(13, "Permission denied", path)

        proof = ProcNetOwnershipProver(
            table=ProcNetListenerTable(reader), user_resolver=resolver
        ).prove(target(18009), owner_linux_user="alice")
        self.assertEqual(proof.state, OWNERSHIP_UNVERIFIED)
        self.assertEqual(proof.reason, REASON_PROC_NET_UNREADABLE)

    def test_a_garbled_table_is_unparseable(self) -> None:
        table = ProcNetListenerTable.from_texts(tcp=PROC_NET_TCP_FIXTURE + "   9: garbage\n")
        proof = ProcNetOwnershipProver(table=table, user_resolver=resolver).prove(
            target(18009), owner_linux_user="alice"
        )
        self.assertEqual(proof.reason, REASON_PROC_NET_UNPARSEABLE)

    def test_the_reader_is_asked_for_the_real_paths_by_default(self) -> None:
        asked: list[str] = []

        def reader(path: str) -> str:
            asked.append(path)
            return ""

        ProcNetListenerTable(reader).listeners()
        self.assertEqual(asked, ["/proc/net/tcp", "/proc/net/tcp6"])


class ListenersServingTest(unittest.TestCase):
    def test_exact_mapped_and_wildcard_matches(self) -> None:
        sockets = fixture_table().listeners()
        exact = listeners_serving("127.0.0.1", 18009, sockets)
        self.assertEqual([(item.inode, wild) for item, wild in exact], [(41001, False)])
        wildcard = listeners_serving("127.0.0.1", 18010, sockets)
        self.assertEqual([(item.inode, wild) for item, wild in wildcard], [(41002, True)])
        mapped = listeners_serving("127.0.0.1", 18015, sockets)
        self.assertEqual([(item.inode, wild) for item, wild in mapped], [(61003, False)])
        dual_stack = listeners_serving("127.0.0.1", 18014, sockets)
        self.assertEqual([(item.inode, wild) for item, wild in dual_stack], [(61002, True)])

    def test_other_addresses_do_not_serve_the_target(self) -> None:
        sockets = fixture_table().listeners()
        # ::1 不接 127.0.0.1；0.0.0.0 不接 ::1；网卡地址不接回环。
        self.assertEqual(listeners_serving("127.0.0.1", 18013, sockets), [])
        self.assertEqual(listeners_serving("::1", 18010, sockets), [])
        self.assertEqual(
            [item.inode for item, _ in listeners_serving("127.0.0.1", 18018, sockets)], [41009]
        )


class ProcNetOwnershipProverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prover = fixture_prover()

    def prove(self, port: int, user: str | None, host: str = "127.0.0.1"):
        return self.prover.prove(target(port, host), owner_linux_user=user)

    def test_the_registrants_own_listener_is_uid_verified(self) -> None:
        proof = self.prove(18009, "alice")
        self.assertEqual(proof.state, OWNERSHIP_UID_VERIFIED)
        self.assertTrue(proof.verified)
        self.assertEqual(proof.uid, 1001)
        self.assertEqual(proof.method, METHOD_PROC_NET)
        self.assertIsNone(proof.reason)
        self.assertIn("127.0.0.1:18009 uid=1001 inode=41001", proof.detail)
        self.assertEqual(proof.source, "/proc/net/tcp,/proc/net/tcp6")

    def test_someone_elses_listener_is_an_owner_mismatch(self) -> None:
        proof = self.prove(18009, "bob")
        self.assertEqual(proof.state, OWNERSHIP_UNVERIFIED)
        self.assertEqual(proof.reason, "owner_mismatch:1001")
        self.assertIsNone(proof.uid)
        self.assertEqual(self.prove(18016, "alice").reason, "owner_mismatch:1002")

    def test_a_wildcard_listener_matches_and_is_recorded(self) -> None:
        proof = self.prove(18010, "alice")
        self.assertTrue(proof.verified)
        self.assertIn("(wildcard)", proof.detail)
        self.assertIn("0.0.0.0:18010", proof.detail)

    def test_ipv6_loopback_and_dual_stack_wildcard(self) -> None:
        self.assertTrue(self.prove(18013, "alice", host="::1").verified)
        self.assertIn("[::1]:18013", self.prove(18013, "alice", host="::1").detail)
        self.assertTrue(self.prove(18014, "alice", host="::1").verified)
        both = self.prove(18014, "alice")
        self.assertTrue(both.verified)
        self.assertIn("(wildcard)", both.detail)

    def test_ipv4_mapped_listener_counts_for_127_0_0_1(self) -> None:
        proof = self.prove(18015, "alice")
        self.assertTrue(proof.verified)
        self.assertIn("[::ffff:127.0.0.1]:18015", proof.detail)

    def test_listeners_with_different_uids_are_ambiguous(self) -> None:
        # 127.0.0.1:18011 属 1002，[::]:18011 属 1003 —— 哪个接到连接从表上看不出。
        for user in ("alice", "carol", "bob"):
            with self.subTest(user=user):
                proof = self.prove(18011, user)
                self.assertFalse(proof.verified)
                self.assertEqual(proof.reason, REASON_AMBIGUOUS)
                self.assertIn("[1002, 1003]", proof.detail)

    def test_reuseport_siblings_of_one_uid_are_not_ambiguous(self) -> None:
        proof = self.prove(18017, "alice")
        self.assertTrue(proof.verified)
        self.assertIn("41007", proof.detail)
        self.assertIn("41008", proof.detail)

    def test_non_listen_states_never_count(self) -> None:
        # 18012 只有 TIME_WAIT；18019 只有 CLOSE_WAIT；18009 上他人的 ESTABLISHED 不算。
        self.assertEqual(self.prove(18012, "alice").reason, REASON_NOT_LISTENING)
        self.assertEqual(self.prove(18019, "alice", host="::1").reason, REASON_NOT_LISTENING)
        self.assertTrue(self.prove(18009, "alice").verified)

    def test_a_port_with_no_listener_is_not_listening(self) -> None:
        proof = self.prove(18013, "alice")  # 只有 ::1 在听，127.0.0.1 没有
        self.assertEqual(proof.reason, REASON_NOT_LISTENING)
        self.assertEqual(proof.state, OWNERSHIP_UNVERIFIED)

    def test_an_unknown_or_missing_user_is_unknown_user(self) -> None:
        self.assertEqual(self.prove(18009, "dana").reason, REASON_UNKNOWN_USER)
        self.assertEqual(self.prove(18009, None).reason, REASON_UNKNOWN_USER)
        self.assertEqual(self.prove(18009, "  ").reason, REASON_UNKNOWN_USER)

    def test_unknown_user_is_decided_before_the_table_is_read(self) -> None:
        def reader(path: str) -> str:  # pragma: no cover - 不该被调用
            raise AssertionError("table must not be read for an unknown user")

        prover = ProcNetOwnershipProver(table=ProcNetListenerTable(reader), user_resolver=resolver)
        self.assertEqual(
            prover.prove(target(18009), owner_linux_user="dana").reason, REASON_UNKNOWN_USER
        )

    def test_a_non_loopback_target_is_refused(self) -> None:
        rogue = NormalizedTarget(
            scheme="http", host="10.0.0.5", port=18009, path="/", query="", url="http://10.0.0.5:18009/"
        )
        proof = self.prover.prove(rogue, owner_linux_user="alice")
        self.assertEqual(proof.reason, REASON_TARGET_NOT_LOOPBACK)
        with self.assertRaises(InvalidRequest):
            self.prover.prove("http://127.0.0.1:18009/", owner_linux_user="alice")  # type: ignore[arg-type]

    def test_the_default_user_resolver_is_pwd(self) -> None:
        # 不注入 resolver 时走 pwd：一个必然不存在的用户名得到 unknown_user。
        prover = ProcNetOwnershipProver(table=fixture_table())
        proof = prover.prove(target(18009), owner_linux_user="no-such-user-llmlab-zz")
        self.assertEqual(proof.reason, REASON_UNKNOWN_USER)

    def test_proof_dict_carries_the_reason(self) -> None:
        payload = self.prove(18009, "bob").to_dict()
        self.assertEqual(payload["reason"], "owner_mismatch:1001")
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["method"], METHOD_PROC_NET)


class ExistingProversStayCompatibleTest(unittest.TestCase):
    def test_existing_provers_accept_and_ignore_the_owner(self) -> None:
        t = target(HEALTHY_PORT)
        self.assertFalse(NoOwnershipProver().prove(t, owner_linux_user="alice").verified)
        self.assertTrue(
            StaticOwnershipProver({("127.0.0.1", HEALTHY_PORT): 4001})
            .prove(t, owner_linux_user="bob")
            .verified
        )

    def test_unverified_proof_now_names_its_reason(self) -> None:
        proof = unverified_proof()
        self.assertEqual(proof.reason, REASON_NO_OWNERSHIP_SOURCE)
        self.assertEqual(proof.method, REASON_NO_OWNERSHIP_SOURCE)


class ProverSelectionTest(unittest.TestCase):
    def test_the_default_stays_fail_closed(self) -> None:
        self.assertEqual(RegistryConfig().ownership_prover_kind, OWNERSHIP_PROVER_NONE)
        self.assertEqual(
            RegistryConfig.production_posture().ownership_prover_kind, OWNERSHIP_PROVER_NONE
        )
        h = Harness(ownership_prover=None)
        self.assertIsInstance(h.service.ownership_prover, NoOwnershipProver)

    def test_proc_net_is_opt_in(self) -> None:
        h = Harness(
            ownership_prover=None,
            config=RegistryConfig(ownership_prover_kind=OWNERSHIP_PROVER_PROC_NET),
        )
        self.assertIsInstance(h.service.ownership_prover, ProcNetOwnershipProver)
        status = h.request("GET", f"{PREFIX}/status", principal="carol")[2]
        self.assertEqual(status["data"]["ownership"]["prover"], "proc_net_tcp")

    def test_an_injected_prover_wins_over_the_config(self) -> None:
        injected = fixture_prover()
        h = Harness(
            ownership_prover=injected,
            config=RegistryConfig(ownership_prover_kind=OWNERSHIP_PROVER_NONE),
        )
        self.assertIs(h.service.ownership_prover, injected)

    def test_unknown_kinds_are_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            RegistryConfig(ownership_prover_kind="trust_me")
        with self.assertRaises(InvalidRequest):
            build_ownership_prover("trust_me")


#: 把夹具里 Harness 的健康端口（8000 = 1F40）判成 alice(1001) 的。
SERVICE_TCP_FIXTURE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 51001 1 0000000000000000 100 0 0 10 0
"""


def service_prover() -> ProcNetOwnershipProver:
    return ProcNetOwnershipProver(
        table=ProcNetListenerTable.from_texts(tcp=SERVICE_TCP_FIXTURE), user_resolver=resolver
    )


class RegistryIntegrationTest(unittest.TestCase):
    """服务层把**受信目录**里的用户名交给 prover，而不是任何请求里的数。"""

    def test_registering_ones_own_engine_is_uid_verified(self) -> None:
        h = Harness(ownership_prover=service_prover())
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "mine", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"},
        )
        self.assertEqual(status, 200, payload)
        ownership = payload["data"]["ownership"]
        self.assertEqual(ownership["state"], OWNERSHIP_UID_VERIFIED)
        # 1001 来自 pwd 替身，不是 Harness 里 alice 的合成 uid 4001。
        self.assertEqual(ownership["uid"], 1001)
        self.assertNotEqual(ownership["uid"], h.requester("alice").uid)

    def test_registering_someone_elses_port_is_an_owner_mismatch(self) -> None:
        h = Harness(ownership_prover=service_prover())
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="bob",
            body={"name": "not-mine", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["ownership"]["reason"], "owner_mismatch:1001")
        self.assertFalse(payload["data"]["ownership"]["verified"])

    def test_a_self_reported_uid_is_rejected_not_used(self) -> None:
        h = Harness(ownership_prover=service_prover())
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="bob",
            body={
                "name": "liar",
                "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1",
                "uid": 1001,
            },
        )
        self.assertEqual(status, 400, payload)

    def test_verify_reproves_with_the_owners_directory_user(self) -> None:
        h = Harness(ownership_prover=service_prover())
        engine_id = h.make_engine("alice", "mine")
        status, _h, payload = h.request(
            "POST", f"{PREFIX}/engines/{engine_id}/verify", principal="alice"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["ownership"]["state"], OWNERSHIP_UID_VERIFIED)

    def test_enforced_policy_admits_a_proc_net_proven_engine(self) -> None:
        h = Harness(
            ownership_prover=service_prover(),
            config=RegistryConfig(mode="synthetic", ownership_policy="enforced"),
        )
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 200, payload)


def _always(ok: bool, detail: str):
    calls: list[NormalizedTarget] = []

    def checker(t: NormalizedTarget) -> tuple[bool, str]:
        calls.append(t)
        return ok, detail

    checker.calls = calls  # type: ignore[attr-defined]
    return checker


class PreflightEndpointTest(unittest.TestCase):
    """``POST /engines/preflight``：只读，四个检查，各自 ``{ok, detail}``。"""

    def preflight(self, h: Harness, principal: str, body: object):
        return h.request("POST", f"{PREFIX}/engines/preflight", principal=principal, body=body)

    def test_all_checks_pass_for_ones_own_model_service(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.service.port_checker = _always(True, "connected")
        status, _h, payload = self.preflight(
            h, "alice", {"name": "qwen", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["schema"], "registry-engine-preflight/1")
        self.assertTrue(data["read_only"])
        self.assertEqual(set(data["checks"]), set(preflight_mod.CHECKS))
        for name, check in data["checks"].items():
            with self.subTest(check=name):
                self.assertIs(check["ok"], True)
                self.assertIn("detail", check)
        self.assertEqual(data["checks"]["ownership"]["evidence"]["uid"], 1001)
        self.assertTrue(data["all_ok"])
        self.assertEqual(data["target"]["port"], HEALTHY_PORT)

    def test_it_writes_nothing(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.service.port_checker = _always(True, "connected")
        before = (count_rows(h.store, "engines"), count_rows(h.store, "audit_log"))
        self.preflight(
            h, "alice", {"name": "qwen", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )
        after = (count_rows(h.store, "engines"), count_rows(h.store, "audit_log"))
        self.assertEqual(before, after)

    def test_failures_are_reported_per_check(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.make_engine("bob", "taken", port=UNREACHABLE_PORT)
        h.service.port_checker = _always(False, "connection_refused")
        status, _h, payload = self.preflight(
            h, "bob", {"name": "taken", "target_url": f"http://127.0.0.1:{UNREACHABLE_PORT}/v1"}
        )
        self.assertEqual(status, 200, payload)
        checks = payload["data"]["checks"]
        self.assertEqual(checks["port_reachable"], {"ok": False, "detail": "connection_refused"})
        self.assertIs(checks["ownership"]["ok"], False)
        self.assertEqual(checks["ownership"]["detail"], REASON_NOT_LISTENING)
        self.assertEqual(
            checks["openai_compat"], {"ok": False, "detail": REASON_TRANSPORT_UNREACHABLE}
        )
        self.assertEqual(checks["name_unique"], {"ok": False, "detail": "engine_name_taken"})
        self.assertFalse(payload["data"]["all_ok"])

    def test_owner_mismatch_reaches_the_form(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.service.port_checker = _always(True, "connected")
        payload = self.preflight(
            h, "bob", {"name": "x", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )[2]
        self.assertEqual(
            {k: payload["data"]["checks"]["ownership"][k] for k in ("ok", "detail")},
            {"ok": False, "detail": "owner_mismatch:1001"},
        )

    def test_name_uniqueness_is_per_owner(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.make_engine("bob", "shared-name")
        h.service.port_checker = _always(True, "connected")
        payload = self.preflight(
            h, "alice", {"name": "shared-name", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )[2]
        self.assertEqual(payload["data"]["checks"]["name_unique"]["ok"], True)

    def test_missing_sources_are_null_not_false(self) -> None:
        # 缺省部署：没有所有权来源、没有协议来源、没有观测源 -> 三个 ok=None。
        h = Harness(prober=None, protocol_prober=NoProtocolProber(), ownership_prover=None)
        self.assertIsNone(h.service.port_checker)
        payload = self.preflight(
            h, "alice", {"name": "qwen", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )[2]
        checks = payload["data"]["checks"]
        self.assertEqual(
            checks["port_reachable"], {"ok": None, "detail": "not_configured_in_this_deployment"}
        )
        self.assertIsNone(checks["ownership"]["ok"])
        self.assertEqual(checks["ownership"]["detail"], REASON_NO_OWNERSHIP_SOURCE)
        self.assertIsNone(checks["openai_compat"]["ok"])
        self.assertIs(checks["name_unique"]["ok"], True)
        self.assertFalse(payload["data"]["all_ok"])

    def test_a_deployment_with_a_prober_gets_a_real_connect_checker(self) -> None:
        h = Harness()
        self.assertIsNotNone(h.service.port_checker)

    def test_identity_fields_and_bad_targets_are_refused(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.service.port_checker = _always(True, "connected")
        url = f"http://127.0.0.1:{HEALTHY_PORT}/v1"
        for body in (
            {"name": "x", "target_url": url, "uid": 1001},
            {"name": "x", "target_url": url, "linux_user": "alice"},
            {"name": "x", "target_url": "http://10.0.0.5:8000/v1"},
            {"name": "x", "target_url": "http://127.0.0.1:8080/v1"},
            {"target_url": url},
        ):
            with self.subTest(body=body):
                status, _h, payload = self.preflight(h, "bob", body)
                self.assertEqual(status, 400, payload)
        self.assertEqual(h.service.port_checker.calls, [])  # type: ignore[attr-defined]

    def test_read_only_principals_cannot_preflight(self) -> None:
        h = Harness(ownership_prover=service_prover())
        h.with_scopes("dave", frozenset({"registry:read"}))
        status, _h, payload = self.preflight(
            h, "dave", {"name": "x", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )
        self.assertEqual(status, 403, payload)

    def test_preflight_is_not_an_engine_id(self) -> None:
        h = Harness()
        status, _h, _payload = h.request("GET", f"{PREFIX}/engines/preflight", principal="alice")
        self.assertEqual(status, 404)


class TcpConnectCheckTest(unittest.TestCase):
    def test_injected_outcomes(self) -> None:
        t = target(18009)

        def refused(address, timeout):
            raise ConnectionRefusedError(111, "refused")

        def timed_out(address, timeout):
            raise socket.timeout("timed out")

        def unreachable(address, timeout):
            raise OSError(101, "unreachable")

        self.assertEqual(
            preflight_mod.tcp_connect_check(t, connect=refused), (False, "connection_refused")
        )
        self.assertEqual(preflight_mod.tcp_connect_check(t, connect=timed_out), (False, "timeout"))
        self.assertEqual(
            preflight_mod.tcp_connect_check(t, connect=unreachable),
            (False, "connect_failed:OSError"),
        )

    def test_a_success_closes_the_connection_and_uses_the_timeout(self) -> None:
        seen: dict = {}

        class Conn:
            closed = False

            def close(self) -> None:
                Conn.closed = True

        def ok(address, timeout):
            seen["address"], seen["timeout"] = address, timeout
            return Conn()

        result = preflight_mod.tcp_connect_check(target(18009), timeout_seconds=0.25, connect=ok)
        self.assertEqual(result, (True, "connected"))
        self.assertTrue(Conn.closed)
        self.assertEqual(seen, {"address": ("127.0.0.1", 18009), "timeout": 0.25})

    def test_non_loopback_and_bad_timeouts_are_refused(self) -> None:
        rogue = NormalizedTarget(
            scheme="http", host="10.0.0.5", port=8000, path="/", query="", url="http://10.0.0.5:8000/"
        )
        with self.assertRaises(InvalidRequest):
            preflight_mod.tcp_connect_check(rogue)
        for timeout in (0, -1, 30):
            with self.subTest(timeout=timeout):
                with self.assertRaises(InvalidRequest):
                    preflight_mod.tcp_connect_check(target(18009), timeout_seconds=timeout)

    def test_protocol_check_maps_three_states(self) -> None:
        self.assertIsNone(
            preflight_mod.protocol_check(
                ProtocolObservation(state=PROTOCOL_UNKNOWN, reason="x", protocol="p")
            )["ok"]
        )
        self.assertIs(
            preflight_mod.protocol_check(
                ProtocolObservation(state=PROTOCOL_UNVERIFIED, reason="y", protocol="p")
            )["ok"],
            False,
        )


@unittest.skipUnless(
    os.path.exists("/proc/net/tcp") and hasattr(os, "getuid"), "needs Linux /proc/net/tcp"
)
class RealProcNetSmokeTest(unittest.TestCase):
    """真实内核：本进程自己开一个回环监听，然后从真实的 ``/proc/net/tcp`` 认出它。

    这证明解析器认得**这台机器**上真实的表格式、且 ``uid`` 列确实是 socket 属主；
    它**不**等于「Dell 上的真实引擎已验证」。
    """

    def test_the_real_table_parses(self) -> None:
        sockets = ProcNetListenerTable().listeners()
        for item in sockets:
            self.assertIsInstance(item, ListeningSocket)

    def test_our_own_loopback_listener_is_uid_verified(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            own = NormalizedTarget(
                scheme="http",
                host="127.0.0.1",
                port=port,
                path="/",
                query="",
                url=f"http://127.0.0.1:{port}/",
            )
            me = os.getuid()
            prover = ProcNetOwnershipProver(user_resolver=lambda _name: me)
            proof = prover.prove(own, owner_linux_user="me")
            self.assertEqual(proof.state, OWNERSHIP_UID_VERIFIED, proof)
            self.assertEqual(proof.uid, me)
            # 同一个监听者，换一个期望 uid -> owner_mismatch:<真实 uid>。
            other = ProcNetOwnershipProver(user_resolver=lambda _name: me + 1)
            self.assertEqual(
                other.prove(own, owner_linux_user="someone").reason, f"owner_mismatch:{me}"
            )
            self.assertEqual(
                preflight_mod.tcp_connect_check(own, timeout_seconds=1.0), (True, "connected")
            )
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
