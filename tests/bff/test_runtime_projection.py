"""``runtime`` 投影的校验器：引擎 / 模型 / 网关 / 路由四格读数的真伪判定。

``validate_runtime_summary`` 是 BFF 在把「引擎正在服务什么」下发给浏览器之前的
最后一道门。它要挡住的是四种**看起来正常、其实不成立**的读数：

* 来源不对或声明了 ``synthetic`` 的载荷；
* 时间戳太旧 —— 一个十分钟前的「healthy」对当下没有意义；
* 声称 ``loaded`` 却说不清是哪个模型；
* 声称路由 ``applied`` 却没有落到某个目标模型上。

被挡下来的格子回 ``unknown`` 且 ``checked_at`` 为 ``null``：既然不知道，就不能
在「未知」旁边并排放一个看起来很新鲜的时间。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.bff import host_summary as H

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)


def _iso(seconds_ago=0):
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(**overrides):
    base = {
        "schema_version": H.RUNTIME_SCHEMA_VERSION,
        "source": H.RUNTIME_PROJECTION_SOURCE,
        "synthetic": False,
        "readonly": True,
        "observed_at": _iso(1),
        "engine": {"state": "healthy", "checked_at": _iso(1)},
        "model": {"state": "loaded", "checked_at": _iso(1), "id": "qwen-test", "vision": True},
        "gateway": {"state": "healthy", "checked_at": _iso(1)},
        "route": {"state": "applied", "checked_at": _iso(1),
                  "alias": "strata-vision", "target_model": "qwen-test"},
    }
    base.update(overrides)
    return base


class ShapeTests(unittest.TestCase):
    def test_a_fresh_well_formed_payload_keeps_every_state(self):
        out = H.validate_runtime_summary(_payload(), now=NOW)
        self.assertEqual(out["state"], "known")
        self.assertEqual(out["engine"]["state"], "healthy")
        self.assertEqual(out["model"]["state"], "loaded")
        self.assertEqual(out["model"]["id"], "qwen-test")
        self.assertTrue(out["model"]["vision"])
        self.assertEqual(out["gateway"]["state"], "healthy")
        self.assertEqual(out["route"]["state"], "applied")
        self.assertEqual(out["route"]["target_model"], "qwen-test")
        self.assertIsNone(out["engine"]["reason"])

    def test_the_declaration_stays_honest(self):
        out = H.validate_runtime_summary(_payload(), now=NOW)
        self.assertFalse(out["synthetic"])
        self.assertTrue(out["readonly"])
        self.assertEqual(out["source"], H.RUNTIME_PROJECTION_SOURCE)


class DeclarationTests(unittest.TestCase):
    def test_another_source_is_not_accepted(self):
        out = H.validate_runtime_summary(_payload(source="prometheus-readonly"), now=NOW)
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["engine"]["reason"], "invalid_response")

    def test_a_synthetic_payload_is_not_served_as_live(self):
        out = H.validate_runtime_summary(_payload(synthetic=True), now=NOW)
        self.assertEqual(out["state"], "unknown")

    def test_a_writable_claim_is_refused(self):
        out = H.validate_runtime_summary(_payload(readonly=False), now=NOW)
        self.assertEqual(out["state"], "unknown")

    def test_a_non_object_is_unknown_not_an_exception(self):
        for payload in (None, [], "runtime", 7):
            with self.subTest(payload=payload):
                out = H.validate_runtime_summary(payload, now=NOW)
                self.assertEqual(out["state"], "unknown")


class FreshnessTests(unittest.TestCase):
    def test_a_stale_projection_is_unknown(self):
        out = H.validate_runtime_summary(
            _payload(observed_at=_iso(H.RUNTIME_MAX_SAMPLE_AGE_S + 5)), now=NOW
        )
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["engine"]["reason"], "stale")

    def test_one_stale_component_does_not_take_down_the_others(self):
        payload = _payload()
        payload["model"] = {"state": "loaded", "checked_at": _iso(H.RUNTIME_MAX_SAMPLE_AGE_S + 5),
                            "id": "qwen-test", "vision": True}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["model"]["state"], "unknown")
        self.assertEqual(out["model"]["reason"], "stale")
        self.assertIsNone(out["model"]["checked_at"], "未知旁边不能放一个新鲜时间")
        self.assertEqual(out["engine"]["state"], "healthy")
        self.assertEqual(out["state"], "known")

    def test_a_timestamp_from_the_future_is_refused(self):
        out = H.validate_runtime_summary(
            _payload(observed_at=_iso(-H.DEFAULT_FUTURE_TOLERANCE_S - 10)), now=NOW
        )
        self.assertEqual(out["state"], "unknown")

    def test_a_missing_timestamp_is_unknown_not_now(self):
        out = H.validate_runtime_summary(_payload(observed_at=None), now=NOW)
        self.assertEqual(out["state"], "unknown")


class ModelClaimTests(unittest.TestCase):
    def test_loaded_without_a_model_id_is_not_a_loaded_model(self):
        payload = _payload()
        payload["model"] = {"state": "loaded", "checked_at": _iso(1), "id": None}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["model"]["state"], "unknown")
        self.assertEqual(out["model"]["reason"], "invalid_response")

    def test_a_model_id_with_odd_characters_is_not_echoed(self):
        payload = _payload()
        payload["model"] = {"state": "loaded", "checked_at": _iso(1), "id": "x\n<script>"}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertIsNone(out["model"]["id"])
        self.assertEqual(out["model"]["state"], "unknown")

    def test_unloaded_is_a_legitimate_state(self):
        payload = _payload()
        payload["model"] = {"state": "unloaded", "checked_at": _iso(1), "id": None}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["model"]["state"], "unloaded")


class RouteClaimTests(unittest.TestCase):
    def test_applied_without_a_target_model_is_not_applied(self):
        payload = _payload()
        payload["route"] = {"state": "applied", "checked_at": _iso(1),
                            "alias": "strata-vision", "target_model": None}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["route"]["state"], "unknown")

    def test_another_alias_is_refused_and_reset(self):
        payload = _payload()
        payload["route"] = {"state": "applied", "checked_at": _iso(1),
                            "alias": "somebody-elses-route", "target_model": "qwen-test"}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["route"]["state"], "unknown")
        self.assertEqual(out["route"]["alias"], "strata-vision")


class StateVocabularyTests(unittest.TestCase):
    def test_a_state_outside_the_vocabulary_is_unknown(self):
        payload = _payload()
        payload["engine"] = {"state": "probably-fine", "checked_at": _iso(1)}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["engine"]["state"], "unknown")

    def test_all_unknown_is_an_unknown_overall_state(self):
        payload = _payload()
        for name in ("engine", "model", "gateway", "route"):
            payload[name] = {"state": "unknown", "checked_at": _iso(1),
                             "alias": "strata-vision", "target_model": "qwen-test"}
        out = H.validate_runtime_summary(payload, now=NOW)
        self.assertEqual(out["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
