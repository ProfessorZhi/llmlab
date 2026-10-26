"""每用户代理上报：校验、入库、可见性。

这一层的价值全在**不相信客户端**上，所以测试也照这一点写：

* 一个端口不在本机 LISTEN 表里、或者那个 uid 不是上报者 -> 整条丢弃；
* 一个路径落在别人家（哪怕读不到）-> 整条丢弃；落在自己家且服务端读不到
  （``PermissionError``）-> 接受，这是唯一能覆盖「服务端看不见用户目录」的路；
* 客户端自带 ``uid`` / ``run_as_user`` / ``target_url`` / ``registered`` -> 整条丢弃；
* ``host_id`` 从主体名 ``linux:<host>:<uid>`` 推出，客户端自称的不算。

上报只登记**模型**，而且只登记在**上报者的平台账户**名下（``source=auto``，
只是路径引用）：端口那一半永远只写 ``agent_reports``——「看见一个端口」不等于
「我要对外提供这个服务」。linux 用户没有账户时不登记任何东西。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.auth_linux.handoff import linux_user_id
from backend.registry.agent_reports import (
    MAX_AGENTS_PER_PRINCIPAL,
    MAX_ENGINES,
    PRUNE_AFTER_SECONDS,
    REPORT_VERSION,
    STALE_AFTER_SECONDS,
    AgentReportValidator,
)
from backend.registry.errors import AccessDenied, Conflict, InvalidRequest, Unauthenticated
from backend.registry.identity import (
    PRINCIPAL_KIND_ACCOUNT,
    SCOPE_ADMIN_READ,
    SCOPE_AGENT_REPORT,
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenDirectory,
    principal_host_id,
)
from backend.registry.ownership import ProcNetListenerTable
from backend.registry.registry import RegistryConfig, RegistryService
from backend.registry.store import Store

HOST = "lab-01"
ALICE_UID, BOB_UID = 4001, 4003
ALICE = linux_user_id(HOST, ALICE_UID)
BOB = linux_user_id(HOST, BOB_UID)
NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)

HOMES = {ALICE_UID: "/home/alice", BOB_UID: "/home/bob"}


def _tcp(*listeners: tuple[int, int]) -> str:
    """``(port, uid)`` -> 一段 ``/proc/net/tcp`` 文本（只含 LISTEN 行）。"""

    lines = [
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode"
    ]
    for index, (port, uid) in enumerate(listeners):
        lines.append(
            f"   {index}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 "
            f"00:00000000 00000000 {uid:5d}        0 {index + 1} 1 0000000000000000 100 0 0 10 0"
        )
    return "\n".join(lines) + "\n"


class _Stat:
    """``os.stat`` 的替身：只认点名过的路径；点名可读的才给 uid。"""

    def __init__(self, owners: dict[str, int] | None = None, unreadable=()) -> None:
        self.owners = dict(owners or {})
        self.unreadable = set(unreadable)
        self.calls: list[str] = []

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path in self.unreadable:
            raise PermissionError(path)
        if path not in self.owners:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_uid=self.owners[path])


def _principals() -> PrincipalDirectory:
    return PrincipalDirectory(
        [
            Principal(ALICE, "alice", ALICE_UID),
            Principal(BOB, "bob", BOB_UID),
        ]
    )


class _AccountDirectory(PrincipalDirectory):
    """静态 Linux 目录 + 一张 ``linux_user -> 平台账户`` 的表。

    生产里这张表是账户库（``AccountAwareDirectory``）；这里只保留 Registry 用到的
    那一个问题：「这个人有没有账户」，账户本身的测试在 ``tests/bff``。
    """

    def __init__(self, entries, mapping) -> None:
        super().__init__(entries)
        self.mapping = dict(mapping)

    def account_for_linux_user(self, linux_user: str):
        return self.mapping.get(linux_user)


ALICE_ACCOUNT = "account:0123456789abcdef0123456789abcdef"
MODEL_PATH = "/home/alice/m.gguf"
LLAMA_PATH = "/usr/local/bin/llama-server"


def _installed_entry(**overrides) -> dict:
    entry = {
        "engine": "llama.cpp",
        "name": "llama.cpp",
        "found_as": "executable",
        "version": "b4120",
        "path": LLAMA_PATH,
        "env": None,
    }
    entry.update(overrides)
    return entry


def _model_entry(**overrides) -> dict:
    entry = {
        "path": MODEL_PATH,
        "format": "gguf",
        "size_bytes": 1234,
        "suggested_name": "alice-qwen",
        "architecture": None,
        "model_type": None,
    }
    entry.update(overrides)
    return entry


def _validator(*, listeners=(), allowed=(), owners=None, unreadable=()):
    return AgentReportValidator(
        allowed_ports=frozenset(allowed),
        listener_table=ProcNetListenerTable.from_texts(tcp=_tcp(*listeners)),
        stat_path=_Stat(owners, unreadable),
        homes=lambda: dict(HOMES),
    )


def _payload(**overrides) -> dict:
    payload = {
        "report_version": REPORT_VERSION,
        "agent_id": "lab-01",
        "full": True,
        "engines": [],
        "installed_engines": [],
        "models": [],
        "problems": [],
    }
    payload.update(overrides)
    return payload


class ValidatorTests(unittest.TestCase):
    def validate(self, payload, *, uid=ALICE_UID, linux_user="alice", validator=None):
        validator = validator or _validator()
        return validator.validate(
            principal=ALICE,
            uid=uid,
            linux_user=linux_user,
            host_id=HOST,
            payload=payload,
            received_at=NOW,
        )

    # -- 端口 --------------------------------------------------------------

    def test_a_port_nobody_listens_on_is_dropped(self) -> None:
        report = self.validate(_payload(engines=[{"port": 8000}]))

        self.assertEqual(report.engines, ())
        self.assertEqual(report.rejected["engines"], {"port_not_listening": 1})
        # 被丢掉的事实也要写进 problems，否则上报者只看到「收下 0 条」。
        self.assertIn("agent_report_engines_dropped:port_not_listening:1", report.problems)

    def test_a_port_listened_on_by_another_uid_is_dropped(self) -> None:
        """这是整条通道的核心反伪造检查：``/proc/net/tcp`` 说端口归谁，就归谁。"""

        validator = _validator(listeners=[(8000, BOB_UID)])
        report = self.validate(_payload(engines=[{"port": 8000}]), validator=validator)

        self.assertEqual(report.engines, ())
        self.assertEqual(report.rejected["engines"], {"port_uid_mismatch": 1})
        self.assertEqual(report.ownership_violations(), {"engines:port_uid_mismatch": 1})

    def test_accepted_engines_get_server_written_ownership(self) -> None:
        validator = _validator(listeners=[(8000, ALICE_UID)], allowed=(8000,))
        report = self.validate(
            _payload(engines=[{"port": 8000, "framework": "vllm", "models": ["qwen"]}]),
            validator=validator,
        )

        engine = report.engines[0]
        self.assertEqual(engine["run_as_uid"], ALICE_UID)
        self.assertEqual(engine["run_as_user"], "alice")
        self.assertEqual(engine["target_url"], "http://127.0.0.1:8000")
        self.assertTrue(engine["registrable"])
        self.assertTrue(engine["openai_compatible"])

    def test_a_port_outside_the_registration_allowlist_is_visible_but_not_registrable(self) -> None:
        validator = _validator(listeners=[(8000, ALICE_UID)], allowed=(9000,))
        report = self.validate(_payload(engines=[{"port": 8000}]), validator=validator)

        self.assertEqual(len(report.engines), 1)
        self.assertFalse(report.engines[0]["registrable"])

    def test_an_item_asserting_its_own_identity_is_dropped_whole(self) -> None:
        validator = _validator(listeners=[(8000, ALICE_UID)], allowed=(8000,))
        for field, value in (
            ("run_as_uid", ALICE_UID),
            ("run_as_user", "alice"),
            ("target_url", "http://127.0.0.1:8000"),
            ("registered", True),
            ("uid", ALICE_UID),
        ):
            with self.subTest(field=field):
                report = self.validate(
                    _payload(engines=[{"port": 8000, field: value}]), validator=validator
                )
                self.assertEqual(report.engines, ())
                self.assertEqual(report.rejected["engines"], {"identity_field_present": 1})

    def test_two_uids_on_one_port_is_ambiguous_not_a_mismatch(self) -> None:
        """同一个端口上两个 uid（回环与通配各一条）：说不清是谁的，就不猜。"""

        from backend.registry.ownership import ListeningSocket

        class _Double:
            def listeners(self):
                return [
                    ListeningSocket(ip="127.0.0.1", port=8000, uid=ALICE_UID, inode=1),
                    ListeningSocket(ip="0.0.0.0", port=8000, uid=BOB_UID, inode=2),
                ]

        validator = _validator()
        validator.listener_table = _Double()
        report = self.validate(_payload(engines=[{"port": 8000}]), validator=validator)
        self.assertEqual(report.engines, ())
        self.assertEqual(report.rejected["engines"], {"port_uid_ambiguous": 1})

    def test_an_unreadable_proc_table_means_nobody_is_believed(self) -> None:
        validator = _validator()
        validator.listener_table = ProcNetListenerTable.from_texts()
        report = self.validate(_payload(engines=[{"port": 8000}]), validator=validator)

        self.assertEqual(report.engines, ())
        self.assertEqual(report.rejected["engines"], {"proc_net_unavailable": 1})

    def test_engines_over_the_cap_are_truncated_and_said_so(self) -> None:
        ports = [(8000 + index, ALICE_UID) for index in range(MAX_ENGINES + 3)]
        validator = _validator(listeners=ports)
        report = self.validate(
            _payload(engines=[{"port": port} for port, _uid in ports]), validator=validator
        )

        self.assertEqual(len(report.engines), MAX_ENGINES)
        self.assertIn("agent_report_engines_capped:3", report.problems)

    # -- 路径 --------------------------------------------------------------

    def test_a_path_in_another_users_home_is_dropped_even_when_unreadable(self) -> None:
        validator = _validator(unreadable=("/home/bob/secret.gguf",))
        report = self.validate(
            _payload(models=[{"path": "/home/bob/secret.gguf", "format": "gguf", "size_bytes": 1}]),
            validator=validator,
        )

        self.assertEqual(report.models, ())
        self.assertEqual(report.rejected["models"], {"path_other_user_home": 1})

    def test_the_reporter_own_home_is_accepted_even_though_the_server_cannot_read_it(self) -> None:
        """``/home/alice`` 是 0700 时，平台以 fjd 运行读不到它 —— 这正是本通道的理由。"""

        validator = _validator(unreadable=("/home/alice/m.gguf",))
        report = self.validate(
            _payload(models=[{"path": "/home/alice/m.gguf", "format": "gguf", "size_bytes": 1}]),
            validator=validator,
        )

        self.assertEqual([item["path"] for item in report.models], ["/home/alice/m.gguf"])

    def test_an_unreadable_path_outside_any_known_home_is_unverifiable(self) -> None:
        validator = _validator(unreadable=("/srv/models/m.gguf",))
        report = self.validate(
            _payload(models=[{"path": "/srv/models/m.gguf", "format": "gguf", "size_bytes": 1}]),
            validator=validator,
        )

        self.assertEqual(report.rejected["models"], {"path_unverifiable": 1})

    def test_a_shared_path_is_accepted_only_when_stat_says_it_is_the_reporters(self) -> None:
        validator = _validator(owners={"/data/models/m.gguf": ALICE_UID})
        report = self.validate(
            _payload(models=[{"path": "/data/models/m.gguf", "format": "gguf", "size_bytes": 1}]),
            validator=validator,
        )
        self.assertEqual([item["path"] for item in report.models], ["/data/models/m.gguf"])

        other = _validator(owners={"/data/models/m.gguf": BOB_UID})
        report = self.validate(
            _payload(models=[{"path": "/data/models/m.gguf", "format": "gguf", "size_bytes": 1}]),
            validator=other,
        )
        self.assertEqual(report.rejected["models"], {"path_uid_mismatch": 1})

    def test_a_symlink_cannot_walk_out_of_the_reporters_home(self) -> None:
        """词法上在 ``/home/alice`` 下，realpath 之后却指向 bob 的文件。"""

        link = "/home/alice/bob-link.gguf"
        target = "/home/bob/secret.gguf"
        validator = _validator(owners={link: ALICE_UID})

        # 真文件系统上没有这条链接，所以只替换 realpath 这一步的结论。
        from backend.registry import agent_reports as module

        realpath = module.os.path.realpath
        module.os.path.realpath = lambda value: target if value == link else realpath(value)
        self.addCleanup(setattr, module.os.path, "realpath", realpath)

        report = self.validate(
            _payload(models=[{"path": link, "format": "gguf", "size_bytes": 1}]),
            validator=validator,
        )
        self.assertEqual(report.rejected["models"], {"path_other_user_home": 1})

    def test_a_duplicate_path_is_counted_once_and_the_second_is_dropped(self) -> None:
        validator = _validator(owners={"/data/models/m.gguf": ALICE_UID})
        item = {"path": "/data/models/m.gguf", "format": "gguf", "size_bytes": 1}
        report = self.validate(_payload(models=[item, dict(item)]), validator=validator)

        self.assertEqual(len(report.models), 1)
        self.assertEqual(report.rejected["models"], {"duplicate_path": 1})

    def test_an_exhausted_stat_budget_stops_further_lookups(self) -> None:
        """预算本身是兜底：分桶上限已经把一次上报的 ``stat`` 次数封在 328 以内。

        所以这里直接对着 ``_path`` 验：预算为 0 时它不碰文件系统，直接说「没预算了」。
        真有一条请求越过 600 次 ``stat`` 的那天，这条分支就是拦住它的地方。
        """

        from backend.registry.agent_reports import _Context

        stat = _Stat(owners={"/data/models/m.gguf": ALICE_UID})
        validator = _validator()
        validator.stat_path = stat
        ctx = _Context(uid=ALICE_UID, linux_user="alice", homes=dict(HOMES), stat_budget=0)

        self.assertEqual(validator._path("/data/models/m.gguf", ctx), (None, "stat_budget_exhausted"))
        self.assertEqual(stat.calls, [])

    def test_paths_that_are_not_absolute_or_contain_dotdot_are_invalid(self) -> None:
        validator = _validator(
            owners={"/home/alice/../bob/m.gguf": ALICE_UID, "relative/m.gguf": ALICE_UID}
        )
        report = self.validate(
            _payload(
                models=[
                    {"path": "/home/alice/../bob/m.gguf", "format": "gguf", "size_bytes": 1},
                    {"path": "relative/m.gguf", "format": "gguf", "size_bytes": 1},
                ]
            ),
            validator=validator,
        )
        self.assertEqual(report.rejected["models"], {"path_invalid": 2})

    # -- 信封 --------------------------------------------------------------

    def test_an_unknown_report_version_is_rejected_outright(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.validate(_payload(report_version=99))

    def test_a_naive_scanned_at_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.validate(_payload(scanned_at="2026-10-26T09:00:00"))

    def test_a_bad_agent_id_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.validate(_payload(agent_id="not a/valid id"))

    def test_full_must_be_a_boolean(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.validate(_payload(full="yes"))

    def test_the_validator_never_reads_a_client_supplied_host_or_uid(self) -> None:
        validator = _validator(listeners=[(8000, ALICE_UID)])
        report = self.validate(
            _payload(host_id="somewhere-else", uid=BOB_UID, engines=[{"port": 8000}]),
            validator=validator,
        )

        self.assertEqual(report.host_id, HOST)
        self.assertEqual(report.uid, ALICE_UID)
        self.assertEqual(report.engines[0]["run_as_uid"], ALICE_UID)


class _Channel:
    """通道替身：只提供 ``status`` 与 ``path``。"""

    def __init__(self, path: str = "/run/llmlab/agent.sock", listening: bool = True,
                 problem: str | None = None) -> None:
        self.path = path
        self.status = SimpleNamespace(
            to_dict=lambda: {
                "path": path,
                "listening": listening,
                "problem": problem,
                "connections": 1,
                "reports": 1,
            }
        )


class ReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "registry.sqlite")
        self.addCleanup(self.store.close)
        self.principals = _principals()
        self.validator = _validator(
            listeners=[(8000, ALICE_UID)],
            allowed=(8000,),
            owners={"/home/alice/m.gguf": ALICE_UID, LLAMA_PATH: ALICE_UID},
        )
        self.service = self.service_for(self.principals)
        self.now = NOW

    def service_for(self, principals) -> RegistryService:
        return RegistryService(
            store=self.store,
            tokens=TokenDirectory(()),
            principals=principals,
            config=RegistryConfig(mode="synthetic"),
            report_validator=self.validator,
            clock=lambda: self.now,
        )

    def service_with_account(self, *, linux_user="alice") -> RegistryService:
        """一个把 ``linux_user`` 解析到平台账户的目录。"""

        account = Principal(
            ALICE_ACCOUNT, "alice", None, kind=PRINCIPAL_KIND_ACCOUNT
        )
        return self.service_for(
            _AccountDirectory(
                [Principal(ALICE, "alice", ALICE_UID), Principal(BOB, "bob", BOB_UID)],
                {linux_user: account},
            )
        )

    def requester(self, principal=ALICE, uid=ALICE_UID, *, scopes=frozenset({SCOPE_AGENT_REPORT})):
        return RequesterContext(
            principal=principal,
            uid=uid,
            linux_user="alice",
            scopes=frozenset(scopes),
            roles=frozenset(),
            token_label="unix-socket",
        )

    # -- 写入 --------------------------------------------------------------

    def test_a_report_is_stored_with_the_server_derived_host(self) -> None:
        report = self.service.report_agent(
            self.requester(), _payload(host_id="liar", engines=[{"port": 8000}])
        )

        self.assertEqual(report.host_id, HOST)
        rows = self.store.list_agent_reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["principal"], ALICE)
        self.assertEqual(rows[0]["host_id"], HOST)
        self.assertEqual(rows[0]["linux_user"], "alice")
        self.assertEqual(rows[0]["uid"], ALICE_UID)
        self.assertEqual(rows[0]["report"]["engines"][0]["port"], 8000)

    def test_a_report_never_registers_an_engine(self) -> None:
        """端口只是「本机有人在听」，登记它需要一次带所有权证明的写请求。"""

        self.service.report_agent(
            self.requester(), _payload(engines=[{"port": 8000}], models=[_model_entry()])
        )

        self.assertEqual(self.store.list_engines(), [])

    def test_a_linux_user_without_an_account_registers_no_model(self) -> None:
        """没有可归属的所有者就不替谁做主：宁可库里少一行，也不能记错人。"""

        self.service.report_agent(self.requester(), _payload(models=[_model_entry()]))

        self.assertEqual(self.store.list_models(), [])

    def test_a_reported_model_lands_under_the_reporters_account(self) -> None:
        service = self.service_with_account()
        service.report_agent(self.requester(), _payload(models=[_model_entry()]))

        rows = self.store.list_models()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_principal, ALICE_ACCOUNT)
        self.assertEqual(rows[0].owner_uid, -1)
        self.assertEqual(rows[0].weight_path, MODEL_PATH)
        self.assertEqual(rows[0].source, "auto")

    def test_re_reporting_the_same_file_does_not_duplicate_it(self) -> None:
        service = self.service_with_account()
        service.report_agent(self.requester(), _payload(models=[_model_entry()]))
        service.report_agent(self.requester(), _payload(models=[_model_entry()]))

        self.assertEqual(len(self.store.list_models()), 1)
        adopted = [
            entry
            for entry in self.store.list_audit(limit=20)
            if entry.action == "agent_models_adopted"
        ]
        # 只有**新建**那一轮留痕：每 5 分钟一轮的上报不该把审计表刷成日志。
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].detail["owner"], ALICE_ACCOUNT)

    def test_a_reported_installed_engine_lands_in_the_reporters_engine_library(self) -> None:
        """「本机装了 llama.cpp」与「引擎库里有 llama.cpp」必须是同一件事。"""

        service = self.service_with_account()
        service.report_agent(
            self.requester(), _payload(installed_engines=[_installed_entry()])
        )

        (row,) = self.store.list_builds()
        self.assertEqual(row.owner_principal, ALICE_ACCOUNT)
        self.assertEqual(row.owner_uid, -1)
        self.assertEqual(row.framework, "llama.cpp")
        self.assertEqual(row.binary_path, LLAMA_PATH)
        self.assertEqual(row.found_as, "executable")

    def test_a_listening_port_never_becomes_a_build(self) -> None:
        """端口是进程，build 是文件：一条上报的不是同一个东西。"""

        self.service_with_account().report_agent(
            self.requester(), _payload(engines=[{"port": 8000}])
        )

        self.assertEqual(self.store.list_builds(), [])

    def test_a_linux_user_without_an_account_has_no_engine_library(self) -> None:
        self.service.report_agent(
            self.requester(), _payload(installed_engines=[_installed_entry()])
        )

        self.assertEqual(self.store.list_builds(), [])

    def test_re_reporting_the_same_build_does_not_duplicate_it_or_the_audit(self) -> None:
        service = self.service_with_account()
        for _ in range(2):
            service.report_agent(
                self.requester(), _payload(installed_engines=[_installed_entry()])
            )

        self.assertEqual(len(self.store.list_builds()), 1)
        adopted = [
            entry
            for entry in self.store.list_audit(limit=20)
            if entry.action == "agent_builds_adopted"
        ]
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].detail["owner"], ALICE_ACCOUNT)

    def test_the_account_can_read_back_the_builds_its_own_agent_adopted(self) -> None:
        service = self.service_with_account()
        service.report_agent(
            self.requester(), _payload(installed_engines=[_installed_entry()])
        )

        view = service.list_builds(self.account_requester())

        self.assertEqual(view["data"]["count"], 1)
        self.assertEqual(view["data"]["items"][0]["binary_path"], LLAMA_PATH)
        self.assertEqual(view["data"]["items"][0]["found_as"], "executable")

    def test_another_member_never_sees_that_library(self) -> None:
        service = self.service_with_account()
        service.report_agent(
            self.requester(), _payload(installed_engines=[_installed_entry()])
        )

        other = RequesterContext(
            principal="account:ffffffffffffffffffffffffffffffff",
            uid=None,
            linux_user="bob",
            scopes=frozenset({"registry:read"}),
            roles=frozenset(),
        )

        self.assertEqual(service.list_builds(other)["data"]["items"], [])

    def test_a_member_sees_the_report_of_the_linux_user_their_account_claims(self) -> None:
        """人以账户登录、代理以 linux 身份上报：看不见自己的上报才是错的。"""

        service = self.service_with_account()
        service.report_agent(self.requester(), _payload(engines=[{"port": 8000}]))

        view = service.discover(self.account_requester())["data"]

        self.assertEqual(view["agent_reports_scope"], "self")
        self.assertEqual([row["linux_user"] for row in view["agent_reports"]], ["alice"])

    def test_a_report_from_a_linux_user_nobody_claims_stays_invisible(self) -> None:
        service = self.service_with_account()
        service.report_agent(
            self.requester(principal=BOB, uid=BOB_UID), _payload(agent_id="bob-01")
        )

        view = service.discover(self.account_requester())["data"]

        self.assertEqual(view["agent_reports"], [])

    def account_requester(self) -> RequesterContext:
        return RequesterContext(
            principal=ALICE_ACCOUNT,
            uid=None,
            linux_user="alice",
            scopes=frozenset({"registry:read"}),
            roles=frozenset(),
        )

    def test_the_report_view_marks_the_adopted_model_as_registered(self) -> None:
        """自动登记之后，「代理上报」表里的那一行不能再显示「未登记」。"""

        service = self.service_with_account()
        service.report_agent(
            self.requester(), _payload(engines=[{"port": 8000}], models=[_model_entry()])
        )

        view = service.discover(
            self.requester(scopes=frozenset({"registry:read", SCOPE_ADMIN_READ}))
        )
        report = view["data"]["agent_reports"][0]

        self.assertTrue(report["models"][0]["registered"])
        # 端口那一半没有被这次改动带偏：它仍然没有登记。
        self.assertFalse(report["engines"][0]["registered"])

    def test_the_socket_scope_is_required(self) -> None:
        """网页会话与 CLI 令牌拿不到这条 scope：它只由内核证明的 uid 产生。"""

        with self.assertRaises(AccessDenied):
            self.service.report_agent(
                self.requester(scopes=frozenset({"registry:read", "registry:write"})),
                _payload(),
            )
        self.assertEqual(self.store.list_agent_reports(), [])

    def test_a_uid_that_does_not_match_the_directory_is_refused(self) -> None:
        with self.assertRaises(AccessDenied):
            self.service.report_agent(self.requester(uid=BOB_UID), _payload())

    def test_an_unmapped_principal_is_refused(self) -> None:
        with self.assertRaises(Unauthenticated):
            self.service.report_agent(
                self.requester(principal=linux_user_id(HOST, 4999), uid=4999), _payload()
            )

    def test_top_level_identity_fields_are_rejected_not_ignored(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.service.report_agent(self.requester(), _payload(uid=ALICE_UID))

    def test_a_new_agent_beyond_the_cap_is_a_conflict(self) -> None:
        for index in range(MAX_AGENTS_PER_PRINCIPAL):
            self.service.report_agent(self.requester(), _payload(agent_id=f"a{index}"))

        with self.assertRaises(Conflict):
            self.service.report_agent(self.requester(), _payload(agent_id="one-too-many"))

        # 覆盖已有的 agent_id 仍然可以 —— 否则闹到上限就再也报不上来了。
        self.service.report_agent(self.requester(), _payload(agent_id="a0"))

    def test_reports_are_overwritten_not_appended(self) -> None:
        self.service.report_agent(self.requester(), _payload(agent_id="lab-01"))
        self.service.report_agent(self.requester(), _payload(agent_id="lab-01", full=False))

        rows = self.store.list_agent_reports()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["full"])

    def test_ownership_violations_are_audited(self) -> None:
        self.validator.listener_table = ProcNetListenerTable.from_texts(
            tcp=_tcp((8000, BOB_UID))
        )
        self.service.report_agent(self.requester(), _payload(engines=[{"port": 8000}]))

        audit = self.store.list_audit(limit=10)
        self.assertIn("agent_report_ownership_violation", [entry.action for entry in audit])

    def test_an_accepted_report_writes_no_audit_row(self) -> None:
        self.service.report_agent(self.requester(), _payload(engines=[{"port": 8000}]))
        self.assertEqual(self.store.list_audit(limit=10), [])

    def test_old_reports_are_pruned_on_the_write_path(self) -> None:
        self.service.report_agent(self.requester(), _payload(agent_id="old"))
        # 拨到很久以后：那条老报告该在这次写入时被清掉。
        self.now = NOW + timedelta(seconds=PRUNE_AFTER_SECONDS + 60)
        self.service.report_agent(
            self.requester(principal=BOB, uid=BOB_UID), _payload(agent_id="new")
        )

        rows = self.store.list_agent_reports()
        self.assertEqual([row["agent_id"] for row in rows], ["new"])

    def test_a_ghost_database_with_an_agent_row_survives_the_schema(self) -> None:
        """迁移 5 是纯增量：一个 v4 库升上来之后，老表一行不少。"""

        legacy = Path(self.tmp.name) / "legacy.sqlite"
        self.store.close()
        connection = sqlite3.connect(legacy, isolation_level=None)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            from backend.registry.store import MIGRATIONS, _split_statements

            for version, script in MIGRATIONS[:4]:
                for statement in _split_statements(script):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, "2026-10-01T00:00:00Z"),
                )
            connection.execute("PRAGMA user_version = 4")
        finally:
            connection.close()

        upgraded = Store(legacy)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.applied_migrations(), (1, 2, 3, 4, 5, 6))
        self.assertEqual(upgraded.list_agent_reports(), [])

    # -- 可见性 ------------------------------------------------------------

    def test_a_report_is_visible_to_its_reporter_and_to_an_admin_only(self) -> None:
        self.service.report_agent(self.requester(), _payload(engines=[{"port": 8000}]))

        mine = self.service.discover(self.requester(scopes=frozenset({"registry:read"})))
        self.assertEqual(mine["data"]["agent_reports_scope"], "self")
        self.assertEqual(len(mine["data"]["agent_reports"]), 1)
        self.assertEqual(mine["data"]["agent_reports"][0]["uid"], ALICE_UID)

        other = self.service.discover(
            self.requester(
                principal=BOB, uid=BOB_UID, scopes=frozenset({"registry:read"})
            )
        )
        self.assertEqual(other["data"]["agent_reports"], [])

        admin = self.service.discover(
            self.requester(
                principal=ALICE,
                scopes=frozenset({"registry:read", "registry:admin:read"}),
            )
        )
        self.assertEqual(admin["data"]["agent_reports_scope"], "all")
        self.assertEqual(len(admin["data"]["agent_reports"]), 1)

    def test_staleness_comes_from_the_server_clock(self) -> None:
        self.service.report_agent(self.requester(), _payload())
        view = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]["agent_reports"][0]
        self.assertFalse(view["stale"])
        self.assertEqual(view["age_seconds"], 0)

        self.now = NOW + timedelta(seconds=STALE_AFTER_SECONDS + 1)
        view = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]["agent_reports"][0]
        self.assertTrue(view["stale"])

    def test_the_views_mark_what_the_reporter_already_registered(self) -> None:
        self.service.report_agent(
            self.requester(),
            _payload(
                engines=[{"port": 8000}],
                models=[{"path": "/home/alice/m.gguf", "format": "gguf", "size_bytes": 1}],
            ),
        )
        from backend.registry.models import MODEL_REGISTERED, Engine, Model

        self.store.insert_engine(
            Engine(
                id="e1",
                owner_principal=ALICE,
                owner_uid=ALICE_UID,
                name="mine",
                target={"scheme": "http", "host": "127.0.0.1", "port": 8000, "path": "/v1"},
                protocol="openai_compatible",
                status="verified",
                health={},
                source="agent",
                created_at=NOW,
                updated_at=NOW,
                last_seen_at=NOW,
            ),
            idempotency_key=None,
            fingerprint="fp-e1",
        )
        self.store.insert_model(
            Model(
                id="m1",
                owner_principal=ALICE,
                owner_uid=ALICE_UID,
                name="mine",
                weight_path="/home/alice/m.gguf",
                weight_path_sha256=None,
                format="gguf",
                parameter_count=None,
                status=MODEL_REGISTERED,
                health={},
                source="agent",
                created_at=NOW,
                updated_at=NOW,
                last_seen_at=NOW,
            ),
            idempotency_key=None,
            fingerprint="fp-m1",
        )

        view = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]["agent_reports"][0]
        self.assertTrue(view["engines"][0]["registered"])
        self.assertTrue(view["models"][0]["registered"])

    def test_the_channel_status_is_part_of_the_envelope(self) -> None:
        data = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]
        self.assertFalse(data["agent_channel"]["configured"])

        self.service.agent_channel = _Channel()
        data = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]
        self.assertTrue(data["agent_channel"]["configured"])
        self.assertTrue(data["agent_channel"]["listening"])
        self.assertEqual(data["agent_channel"]["path"], "/run/llmlab/agent.sock")

    def test_the_channel_is_reported_off_with_a_reason_when_it_never_started(self) -> None:
        self.service.agent_channel = _Channel(
            listening=False, problem="agent_socket_dir_missing:/run/llmlab"
        )
        data = self.service.discover(
            self.requester(scopes=frozenset({"registry:read"}))
        )["data"]
        self.assertFalse(data["agent_channel"]["listening"])
        self.assertEqual(
            data["agent_channel"]["problem"], "agent_socket_dir_missing:/run/llmlab"
        )


class PrincipalHostIdTests(unittest.TestCase):
    def test_the_host_is_read_back_from_the_principal_name(self) -> None:
        self.assertEqual(principal_host_id(linux_user_id("dell-01", 1000)), "dell-01")
        self.assertEqual(principal_host_id("linux:lab-01:4001"), "lab-01")

    def test_anything_else_has_no_host(self) -> None:
        for value in ("account:abc", "alice", "linux:lab-01", "linux::1", "linux:lab-01:x", None):
            with self.subTest(value=value):
                self.assertIsNone(principal_host_id(value))


if __name__ == "__main__":
    unittest.main()
