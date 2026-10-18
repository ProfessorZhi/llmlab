"""``GET /api/portal/events`` —— V1-11 的 logs/audit 面。

V1-11 的词是「fixed-scope operator services/logs/audit … bounded and redacted;
no arbitrary raw shell/PromQL/path proxy」。这一组逐条对应那几个词，而且每一条都
落在**行为**上，不是文档上：

* **fixed-scope operator** —— 成员 403，操作员 200。跨主体审计是这个 scope 唯一
  的用途，所以没有「顺便看看自己那份」的第二条路径。
* **bounded** —— ``limit`` 有闭区间上界，超界 400；一页就是上限那么多条。
* **redacted** —— 只有白名单字段；两个账本各自的私有列（含 ``detail``）**不在**
  下发内容里，而且不是「过滤掉了」，是根本不会被构造出来。
* **no arbitrary … proxy** —— ``?url`` / ``?path`` / ``?promql`` / ``?cmd`` /
  ``?source`` / ``?uid`` 全部在到达本模块之前就被 400。

两个来源都是仓库里**已经存在**的结构化账本（本层提权审计 + Registry audit_log），
没有一条是为了这条路由新造的数据。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone

from backend.bff import events as bff_events
from backend.bff import route_map as RM
from tests.bff import support as S


class EventSourceVocabularyTests(unittest.TestCase):
    """状态词只借用、不自创。"""

    def test_the_state_words_come_from_the_warehouse_vocabulary(self):
        """``ok`` / ``empty`` / ``unconfigured`` 是既有词，不是本模块发明的。

        审计账本刻意**没有** ``stale``：一台闲着的机器上，「最新一条很旧」是事实，
        不是故障。这条断言把「借词」写成可核对的关系，而不是一句注释。
        """
        from backend.evaluation.provenance import SOURCE_STATES

        for word in bff_events.STATES:
            with self.subTest(word=word):
                self.assertIn(word, SOURCE_STATES)

    def test_no_item_field_is_outside_the_declared_allowlists(self):
        self.assertEqual(
            set(bff_events.ELEVATION_FIELDS),
            {"at", "event", "outcome", "actor", "principal", "uid", "linux_user", "admin_mode"},
        )
        self.assertEqual(
            set(bff_events.REGISTRY_FIELDS),
            {"at", "action", "actor", "actor_uid", "object_type", "object_id"},
        )

    def test_the_route_is_declared_as_an_operator_read(self):
        spec = RM.ROUTES_BY_NAME["portal.events"]
        self.assertEqual(spec.path, "/api/portal/events")
        self.assertEqual(spec.auth, RM.AUTH_SESSION)
        self.assertFalse(spec.is_write)
        self.assertEqual(spec.source, RM.SOURCE_AUDIT)
        self.assertEqual(spec.scope, "portal:admin:read")
        # 新增的来源词也要落进那张封闭表，否则 `test_every_route_declares_closed_values`
        # 会在别处炸掉 —— 这条只是让失败出现在读得懂的地方。
        self.assertIn(RM.SOURCE_AUDIT, RM.SOURCES)


class EventRouteAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())
        S.register_baseline(cls.h)          # registry audit entries
        cls.operator = cls.h.admin_client("carol")   # elevated: portal:admin:read + an enter row

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_anonymous_is_401(self):
        anon = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = anon.run("GET", "/api/portal/events")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_a_member_is_403_not_a_filtered_view_of_other_people_activity(self):
        """成员拿不到一份「过滤过的别人活动」——它拿不到任何一份。"""
        for name in (S.MEMBER_NAME, S.OTHER_MEMBER_NAME):
            with self.subTest(name=name):
                response = self.h.client(name).run("GET", "/api/portal/events")
                self.assertEqual(response.status, 403, response.json())
                self.assertEqual(response.error_code(), "missing_required_scope")
                self.assertIsNone(response.json().get("data"))

    def test_an_operator_reads_both_real_sources(self):
        response = self.operator.run("GET", "/api/portal/events")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        by_source = {item["source"]: item for item in data["sources"]}
        self.assertEqual(set(by_source), {"elevation", "registry"})
        for source, block in by_source.items():
            with self.subTest(source=source):
                self.assertIn(block["state"], bff_events.STATES)
                self.assertGreater(block["count"], 0, f"{source} has no real entries")
                self.assertEqual(len(block["items"]), block["count"])


class EventRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())
        S.register_baseline(cls.h)
        cls.operator = cls.h.admin_client("carol")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_the_ledgers_private_columns_never_reach_the_browser(self):
        """两个账本都有 ``detail`` 列；它**不在**任何一条下发记录里。

        这是白名单的效果，不是黑名单：``detail`` 里可能放进任何东西，所以做法是
        「只有名单里的键存在」，而不是「把已知敏感的键删掉」。
        """
        data = self.operator.run("GET", "/api/portal/events").json()["data"]
        allowed = {
            "elevation": set(bff_events.ELEVATION_FIELDS),
            "registry": set(bff_events.REGISTRY_FIELDS),
        }
        for block in data["sources"]:
            for item in block["items"]:
                with self.subTest(source=block["source"]):
                    self.assertLessEqual(set(item), allowed[block["source"]])
                    self.assertNotIn("detail", item)

    def test_every_value_is_a_scalar_so_no_nested_blob_can_smuggle_a_secret(self):
        data = self.operator.run("GET", "/api/portal/events").json()["data"]
        for block in data["sources"]:
            for item in block["items"]:
                for key, value in item.items():
                    with self.subTest(source=block["source"], field=key):
                        self.assertNotIsInstance(value, (dict, list, tuple, set))

    def test_the_serialized_body_carries_no_path_credential_or_account_secret(self):
        body = self.operator.run("GET", "/api/portal/events").body.decode("utf-8")
        for needle in ("/home/", "/data/", "password", "Bearer", "BEGIN ", "detail"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, body)

    def test_timestamps_are_normalised_across_the_two_ledgers(self):
        """一个账本存 ISO、另一个存 epoch 浮点；下发之前必须归一。"""
        data = self.operator.run("GET", "/api/portal/events").json()["data"]
        for block in data["sources"]:
            with self.subTest(source=block["source"]):
                self.assertIsNotNone(block["newest_at"])
                self.assertTrue(block["newest_at"].endswith("Z"), block["newest_at"])
                for item in block["items"]:
                    self.assertTrue(item["at"].endswith("Z"), item["at"])


class EventBoundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())
        S.register_baseline(cls.h)
        cls.operator = cls.h.admin_client("carol")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_the_default_page_is_bounded(self):
        data = self.operator.run("GET", "/api/portal/events").json()["data"]
        self.assertEqual(data["limit"], bff_events.DEFAULT_LIMIT)
        for block in data["sources"]:
            self.assertLessEqual(block["count"], bff_events.DEFAULT_LIMIT)

    def test_an_out_of_range_limit_is_400_not_a_silent_truncation(self):
        for value in ("0", "-1", str(bff_events.MAX_LIMIT + 1), "abc", "1.5", ""):
            with self.subTest(limit=value):
                response = self.operator.run("GET", f"/api/portal/events?limit={value}")
                self.assertEqual(response.status, 400, response.json())
                self.assertEqual(response.error_code(), "bff.invalid_request")

    def test_a_small_limit_bounds_the_page_and_marks_it_truncated(self):
        data = self.operator.run("GET", "/api/portal/events?limit=1").json()["data"]
        self.assertEqual(data["limit"], 1)
        for block in data["sources"]:
            self.assertLessEqual(block["count"], 1)
        registry = next(b for b in data["sources"] if b["source"] == "registry")
        self.assertTrue(registry["truncated"], "four entries exist; a 1-row page is truncated")

    def test_there_is_no_parameter_that_can_point_the_reader_elsewhere(self):
        """V1-11 点名的那几类：上游 URL、文件路径、PromQL、shell、UID。"""
        for query in (
            "url=http://127.0.0.1:3000",
            "path=../../etc/passwd",
            "promql=up",
            "query=up",
            "cmd=id",
            "shell=1",
            "uid=0",
            "owner=x",
            "principal=x",
            "source=live",
        ):
            with self.subTest(query=query):
                response = self.operator.run("GET", f"/api/portal/events?{query}")
                self.assertEqual(response.status, 400, response.json())
                self.assertIn(
                    response.error_code(),
                    ("proxy_assertion_rejected", "source_assertion_rejected"),
                )


class EventSourceStateTests(unittest.TestCase):
    """来源没接 / 读不到 / 真的空 —— 三种结局必须分得开。"""

    class _RaisingService:
        def audit_log(self, requester, limit=200):  # noqa: ARG002
            raise RuntimeError("synthetic ledger failure at /secret/path")

    def _operator(self):
        """一个**真的**操作员会话。

        ``portal:admin:read`` 在这套模型里只由提权状态推出，而提权需要 admin mode，
        所以「操作员」与「admin mode 装着」是同一件事。想验「来源没装」时不能把
        admin mode 整个摘掉 —— 那会连操作员一起摘掉，得到的是 403 而不是
        ``unconfigured``。要验的失败在**来源**那一侧，不在身份那一侧。
        """
        h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(h.close)
        return h, h.admin_client("carol")

    def test_a_missing_registry_source_is_unconfigured_not_empty(self):
        h, operator = self._operator()
        h.app.service = None
        response = operator.run("GET", "/api/portal/events")
        self.assertEqual(response.status, 200, response.json())
        by_source = {b["source"]: b for b in response.json()["data"]["sources"]}
        self.assertEqual(by_source["registry"]["state"], bff_events.STATE_UNCONFIGURED)
        self.assertEqual(by_source["registry"]["reason"], bff_events.REASON_NOT_CONFIGURED)
        self.assertEqual(by_source["registry"]["count"], 0)
        # 另一条来源不受影响：这是「一条没装」，不是「整条路由没装」。
        self.assertEqual(by_source["elevation"]["state"], bff_events.STATE_OK)

    def test_a_ledger_error_is_unreachable_and_does_not_leak_the_exception(self):
        h, operator = self._operator()
        h.app.service = self._RaisingService()
        response = operator.run("GET", "/api/portal/events")
        self.assertEqual(response.status, 200, response.json())
        registry = next(
            b for b in response.json()["data"]["sources"] if b["source"] == "registry"
        )
        self.assertEqual(registry["state"], bff_events.STATE_UNREACHABLE)
        self.assertEqual(registry["reason"], bff_events.REASON_READ_FAILED)
        self.assertNotIn("/secret/path", response.body.decode())

    def test_an_empty_ledger_is_empty_not_unconfigured(self):
        h, operator = self._operator()
        # Registry 有审计表、但这一次什么都没写：state=empty 是**测量结果**。
        data = operator.run("GET", "/api/portal/events").json()["data"]
        registry = next(b for b in data["sources"] if b["source"] == "registry")
        self.assertEqual(registry["state"], bff_events.STATE_EMPTY)
        self.assertIsNone(registry["reason"])
        self.assertIsNone(registry["newest_at"])
        self.assertEqual(registry["count"], 0)


class EventBuilderUnitTests(unittest.TestCase):
    """纯函数面的两条不变式，不需要起 HTTP。"""

    NOW = datetime(2026, 10, 18, 0, 0, 0, tzinfo=timezone.utc)

    def test_an_out_of_range_limit_is_refused_by_the_builder_too(self):
        for value in (0, bff_events.MAX_LIMIT + 1, True, 1.5):
            with self.subTest(limit=value):
                with self.assertRaises(ValueError):
                    bff_events.build_events_dto(
                        admin_mode=None, service=None, requester=None, limit=value, now=self.NOW
                    )

    def test_the_dto_is_json_serialisable_and_carries_its_own_freshness_facts(self):
        dto = bff_events.build_events_dto(
            admin_mode=None, service=None, requester=None, now=self.NOW
        )
        json.dumps(dto)
        self.assertEqual(dto["observed_at"], "2026-10-18T00:00:00Z")
        self.assertTrue(bff_events.all_sources_unconfigured(dto))
        for block in dto["sources"]:
            self.assertEqual(block["state"], bff_events.STATE_UNCONFIGURED)
            self.assertIsNone(block["newest_at"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
