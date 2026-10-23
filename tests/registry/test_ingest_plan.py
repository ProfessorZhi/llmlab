"""§4.1 状态机的单元测试。

状态机是纯函数，所以「起一个假引擎、30 秒后出现、停掉 60 秒后 offline」这条
验收故事可以用一次次 ``ingest_round`` 精确复现，不需要真的等 60 秒。
"""

from __future__ import annotations

import unittest

from backend.registry.ingest import (
    ATTACH,
    ATTACHED,
    DROP,
    IGNORE,
    OFFLINE,
    OFFLINE_ACTION,
    OBSERVE,
    RECOVER,
    SEEN,
    ProbeOutcome,
    SeenPort,
    TrackedService,
    ingest_round,
    normalize_model_id,
    route_name_for,
    service_key,
)

KNOWN = {1001}
ALICE = SeenPort(uid=1001, port=8013)
KEY = service_key(ALICE.uid, ALICE.port)


def ok_round(tracked, seen, probe=None, *, known=KNOWN):
    return ingest_round(
        tracked,
        seen,
        {service_key(s.uid, s.port): (probe or ProbeOutcome(True, "vllm", ("qwen3-4b",))) for s in seen},
        known_uids=known,
        now="2026-10-23T00:00:00Z",
    )


class AttachTests(unittest.TestCase):
    def test_a_new_port_with_a_successful_probe_is_attached_in_the_same_round(self):
        round_result = ok_round({}, [ALICE])

        self.assertEqual(len(round_result.of_kind(ATTACH)), 1)
        self.assertEqual(round_result.tracked[KEY].state, ATTACHED)
        self.assertEqual(round_result.tracked[KEY].model_ids, ("qwen3-4b",))
        self.assertEqual(round_result.tracked[KEY].framework, "vllm")

    def test_hostile_uid_is_ignored_and_never_tracked(self):
        """D2：不在映射里的 uid 不是平台用户，不能进账本。"""

        round_result = ok_round({}, [SeenPort(uid=4242, port=9000)])

        self.assertEqual(len(round_result.of_kind(IGNORE)), 1)
        self.assertEqual(round_result.tracked, {})

    def test_a_port_that_never_probes_successfully_is_dropped_after_three_rounds(self):
        round_result = ok_round({}, [ALICE], ProbeOutcome(False, detail="超时"))
        self.assertEqual(round_result.tracked[KEY].state, SEEN)

        round_result = ok_round(round_result.tracked, [ALICE], ProbeOutcome(False))
        self.assertEqual(round_result.tracked[KEY].probe_failures, 2)

        round_result = ok_round(round_result.tracked, [ALICE], ProbeOutcome(False))
        self.assertEqual(round_result.of_kind(DROP)[0].reason, "连续探测失败")
        self.assertNotIn(KEY, round_result.tracked)

    def test_a_missing_probe_is_not_a_failure(self):
        """慢一拍的探测不该被当成三次失败中的一次。"""

        round_result = ingest_round(
            {}, [ALICE], {}, known_uids=KNOWN, now="2026-10-23T00:00:00Z"
        )
        self.assertEqual(round_result.tracked[KEY].state, SEEN)
        self.assertEqual(round_result.tracked[KEY].probe_failures, 0)

        round_result = ok_round(round_result.tracked, [ALICE])
        self.assertEqual(round_result.tracked[KEY].state, ATTACHED)


class LivenessTests(unittest.TestCase):
    def _attached(self) -> dict[str, TrackedService]:
        return ok_round({}, [ALICE]).tracked

    def test_one_missing_round_does_not_take_the_service_offline(self):
        round_result = ok_round(self._attached(), [])

        self.assertEqual(round_result.of_kind(OFFLINE_ACTION), [])
        self.assertEqual(round_result.tracked[KEY].state, ATTACHED)
        self.assertEqual(round_result.tracked[KEY].misses, 1)

    def test_two_missing_rounds_take_it_offline_and_keep_the_identity(self):
        first = ok_round(self._attached(), [])
        second = ok_round(first.tracked, [])

        offline = second.of_kind(OFFLINE_ACTION)
        self.assertEqual(len(offline), 1)
        self.assertEqual(offline[0].service.uid, ALICE.uid)
        self.assertEqual(offline[0].service.port, ALICE.port)
        self.assertEqual(second.tracked[KEY].state, OFFLINE)
        # offline 不是 drop：路由保留，调用方拿到 503 而不是 404。
        self.assertEqual(second.of_kind(DROP), [])

    def test_the_same_model_coming_back_recovers(self):
        offline = ok_round(ok_round(self._attached(), []).tracked, []).tracked
        self.assertEqual(offline[KEY].state, OFFLINE)

        round_result = ok_round(offline, [ALICE])

        recovered = round_result.of_kind(RECOVER)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(round_result.tracked[KEY].state, ATTACHED)

    def test_a_different_model_on_the_same_port_is_a_new_service(self):
        offline = ok_round(ok_round(self._attached(), []).tracked, []).tracked

        round_result = ok_round(
            offline, [ALICE], ProbeOutcome(True, "vllm", ("llama-3-8b",))
        )

        attach = round_result.of_kind(ATTACH)
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0].reason, "端口复用但模型已更换")
        self.assertEqual(round_result.tracked[KEY].model_ids, ("llama-3-8b",))

    def test_an_offline_port_that_stays_silent_is_kept_for_the_next_round(self):
        offline = ok_round(ok_round(self._attached(), []).tracked, []).tracked

        round_result = ok_round(offline, [])

        self.assertEqual(round_result.tracked[KEY].state, OFFLINE)
        self.assertEqual(round_result.of_kind(OFFLINE_ACTION), [])


class AcceptanceStoryTests(unittest.TestCase):
    """S2 的验收故事：起假引擎 ⇒ 出现；停掉 ⇒ 60 s 内 offline。"""

    def test_fake_engine_lifecycle(self):
        tracked: dict[str, TrackedService] = {}
        rounds = 0

        # 引擎起来之前：连续几轮什么都没有。
        for _ in range(3):
            tracked = ok_round(tracked, []).tracked
            rounds += 1
        self.assertEqual(tracked, {})

        # 引擎起来：下一轮接入（30 s 一轮，验收要求 30 s 内）。
        result = ok_round(tracked, [ALICE])
        tracked = result.tracked
        rounds += 1
        self.assertEqual(result.of_kind(ATTACH)[0].service.port, ALICE.port)

        # 引擎停掉：第一轮还在，第二轮 offline（≤60 s）。
        result = ok_round(tracked, [])
        tracked = result.tracked
        rounds += 1
        self.assertEqual(result.of_kind(OFFLINE_ACTION), [])

        result = ok_round(tracked, [])
        tracked = result.tracked
        rounds += 1
        self.assertEqual(len(result.of_kind(OFFLINE_ACTION)), 1)

        # 再起来：恢复而不是新建。
        result = ok_round(tracked, [ALICE])
        self.assertEqual(len(result.of_kind(RECOVER)), 1)


class RouteNameTests(unittest.TestCase):
    def test_model_id_is_used_verbatim_when_free(self):
        self.assertEqual(route_name_for("qwen3-4b", "alice", set()), "qwen3-4b")

    def test_conflict_appends_the_owner(self):
        self.assertEqual(route_name_for("qwen3-4b", "alice", {"qwen3-4b"}), "qwen3-4b@alice")

    def test_repeated_conflict_gets_a_stable_suffix(self):
        taken = {"qwen3-4b", "qwen3-4b@alice"}
        self.assertEqual(route_name_for("qwen3-4b", "alice", taken), "qwen3-4b@alice-2")

    def test_same_inputs_give_the_same_route_every_round(self):
        taken = {"qwen3-4b", "qwen3-4b@alice"}
        first = route_name_for("qwen3-4b", "alice", taken)
        self.assertEqual(first, route_name_for("qwen3-4b", "alice", taken))

    def test_unsafe_characters_are_stripped(self):
        self.assertEqual(normalize_model_id("/models/qwen 3/"), "models/qwen3")
        self.assertIsNone(normalize_model_id("模型"))

    def test_an_unnormalizable_id_falls_back_to_model(self):
        self.assertEqual(route_name_for("模型", "alice", set()), "model")


if __name__ == "__main__":
    unittest.main()
