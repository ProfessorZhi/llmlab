"""平台定期复查本机已登记的服务：表要新鲜，且不能假装看见过。

这一组测的是三件互不相同的事，别混着读：

1. **写回** —— 探一次，今天的结果要落进 ``status`` / ``health`` / ``verification``。
2. **``last_seen_at`` 的语义** —— 它是「看见了」，不是「探过了」。探不通时**不动**它，
   否则 ``presence.service_state`` 判断代理停止上报的那条路就永远不触发。
3. **范围** —— 只探回环目标。别的主机的行探不通只说明这台机器连不上。

审计署名单独断言：``system:engine-reverify`` 与用户手动触发的 ``verify`` 必须能分开读。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.registry import support as S
from backend.registry.errors import InvalidRequest
from backend.registry.health import ProbeResult
from backend.registry.models import (
    ENGINE_RUNTIME_ATTACHED,
    ENGINE_UNAVAILABLE,
    ENGINE_VERIFIED,
    SOURCE_CLIENT_REGISTERED,
    Engine,
    isoformat,
)
from backend.registry.reverify import EngineReverifier

OLD = datetime.now(timezone.utc) - timedelta(hours=1)


def tearDownModule() -> None:
    S.close_all()


class EngineReverifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = S.Harness()
        self.addCleanup(self.h.close)

    def age_engine(self, engine_id: str) -> None:
        """把一行的「最后看见」拨到一小时前，模拟一个停了很久的服务。"""
        self.h.store.update_engine_runtime(engine_id, last_seen_at=OLD)

    def test_a_stale_engine_is_probed_and_the_conclusion_is_written_back(self):
        engine_id = self.h.make_engine("alice", "svc")
        self.age_engine(engine_id)
        # 服务停了：同一个端口，探测源这次说连不上。
        self.h.prober.set(S.HEALTHY_PORT, ProbeResult(reachable=False, detail="connection refused"))

        round_result = self.h.service.reverify_local_engines()

        self.assertEqual(round_result["probed"], 1, round_result)
        engine = self.h.store.engine_by_id(engine_id)
        self.assertEqual(engine.status, ENGINE_UNAVAILABLE)
        self.assertEqual(engine.health["state"], "unreachable")
        self.assertFalse(engine.verification["verified"])
        # 探不通**不是**看见过：这一列保持原值，presence 的 stale 判定才有意义。
        # 存储只有秒级精度，所以比的是落库后的那个时刻。
        self.assertEqual(isoformat(engine.last_seen_at), isoformat(OLD))

    def test_a_successful_reverify_bumps_last_seen_at(self):
        engine_id = self.h.make_engine("alice", "svc")
        self.age_engine(engine_id)

        self.h.service.reverify_local_engines()

        engine = self.h.store.engine_by_id(engine_id)
        self.assertEqual(engine.status, ENGINE_VERIFIED)
        self.assertGreater(engine.last_seen_at, OLD)
        # 结论与「看见」是同一时刻的两个字段，不该互相对不上。
        self.assertEqual(engine.health["observed_at"], isoformat(engine.last_seen_at))

    def test_the_audit_names_the_platform_not_the_owner(self):
        engine_id = self.h.make_engine("alice", "svc")
        self.age_engine(engine_id)

        self.h.service.reverify_local_engines()

        entries = [
            e for e in self.h.store.list_audit(object_type="engine", object_id=engine_id)
            if e.action == "reverify"
        ]
        self.assertEqual(len(entries), 1)
        # 不是 alice，也不是某个管理员：是平台自己按周期探的这一下。
        self.assertEqual(entries[0].actor_principal, "system:engine-reverify")
        self.assertEqual(entries[0].actor_uid, 0)
        self.assertEqual(entries[0].detail["status"], ENGINE_VERIFIED)

    def test_a_fresh_engine_is_left_alone(self):
        self.h.make_engine("alice", "svc")  # 刚刚登记，刚看见过

        round_result = self.h.service.reverify_local_engines()

        self.assertEqual(round_result["due"], 0, round_result)
        self.assertEqual(round_result["probed"], 0)
        self.assertEqual(
            [e for e in self.h.store.list_audit(object_type="engine") if e.action == "reverify"],
            [],
        )

    def test_only_loopback_targets_are_probed(self):
        # 回环登记不了这台机器之外的东西（urls 的 allowlist），所以这里直接把一行
        # 别处主机的记录写进账本 —— 这是唯一能测到「范围」这条路的方式。
        remote = Engine(
            id="eng_remote_fixture",
            owner_principal="alice",
            owner_uid=4001,
            name="elsewhere",
            target={
                "scheme": "http", "host": "10.0.0.5", "port": 8000,
                "path": "/v1", "query": "", "url": "http://10.0.0.5:8000/v1",
                "operator_blessed": False, "operator_label": None,
            },
            protocol="openai_compatible",
            status=ENGINE_VERIFIED,
            health={"state": "unknown"},
            source=SOURCE_CLIENT_REGISTERED,
            created_at=OLD,
            updated_at=OLD,
            last_seen_at=OLD,
            state=ENGINE_RUNTIME_ATTACHED,
        )
        self.h.store.insert_engine(remote, idempotency_key=None, fingerprint="fixture")

        round_result = self.h.service.reverify_local_engines()

        self.assertEqual(round_result["probed"], 0, round_result)
        self.assertEqual(round_result["not_local"], 1)
        # 原样保留：这台机器对它的连通性不是一个关于它的事实。
        self.assertEqual(self.h.store.engine_by_id("eng_remote_fixture").status, ENGINE_VERIFIED)

    def test_without_an_observation_source_nothing_is_probed_or_written(self):
        h = S.Harness(prober=None)
        self.addCleanup(h.close)
        engine_id = h.make_engine("alice", "svc")
        h.store.update_engine_runtime(engine_id, last_seen_at=OLD)
        before = h.store.engine_by_id(engine_id)

        round_result = h.service.reverify_local_engines()

        self.assertEqual(round_result["skipped"], "no_observation_source")
        after = h.store.engine_by_id(engine_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.last_seen_at, before.last_seen_at)

    def test_one_failing_row_does_not_stop_the_round(self):
        broken = self.h.make_engine("alice", "broken", port=S.DEGRADED_PORT)
        healthy = self.h.make_engine("alice", "healthy", port=S.HEALTHY_PORT)
        self.age_engine(broken)
        self.age_engine(healthy)

        original = self.h.service._probe_and_record
        def probe(engine, **kwargs):
            if engine.id == broken:
                raise InvalidRequest("boom")
            return original(engine, **kwargs)
        self.h.service._probe_and_record = probe  # type: ignore[method-assign]
        self.addCleanup(setattr, self.h.service, "_probe_and_record", original)

        round_result = self.h.service.reverify_local_engines()

        self.assertEqual(round_result["probed"], 2, round_result)
        errors = [row for row in round_result["engines"] if "error" in row]
        self.assertEqual([row["engine_id"] for row in errors], [broken])
        self.assertEqual(self.h.store.engine_by_id(healthy).status, ENGINE_VERIFIED)


class EngineReverifierTickTests(unittest.TestCase):
    """调度器只看一个 ``tick()``：这一层薄到没有逻辑，但它决定了「一轮」是什么。"""

    def setUp(self) -> None:
        self.h = S.Harness()
        self.addCleanup(self.h.close)

    def test_tick_is_one_reverify_round(self):
        engine_id = self.h.make_engine("alice", "svc")
        self.h.store.update_engine_runtime(engine_id, last_seen_at=OLD)

        round_result = EngineReverifier(self.h.service).tick()

        self.assertEqual(round_result["probed"], 1)
        self.assertEqual(round_result["engines"][0]["engine_id"], engine_id)


if __name__ == "__main__":
    unittest.main()
