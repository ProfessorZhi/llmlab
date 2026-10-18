"""主机面板刷新契约的回归（issue #38）。

两部分：

1. **fixture 一致性** —— ``tests/observability/fixtures/host_summary/`` 里每一个
   用例都被判一次，断言结果与文件里的 ``expect`` 逐字相同。这些文件同时是给 UI
   Lane 的交接物，所以这里过了，那边的 JS 测试就有同一套期望值可对。
2. **状态机边界** —— 30 秒节流、同时最多一个在途请求、切后台停/切回来立刻刷、
   过期清空、失败清旧值、迟到回包不覆盖、时间回拨。

两条纪律值得单独说：**迟到回包不许覆盖更新的结果**，以及**过期是每次渲染都重算的**。
后者是「挂起一夜」场景的解药 —— 它不依赖任何一个新响应到达。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability.host_projection import validate_host_summary  # noqa: E402
from backend.observability.host_refresh import (  # noqa: E402
    DISPLAY_LIVE,
    DISPLAY_UNKNOWN,
    REASON_CLOCK_ROLLBACK,
    REASON_CONTRACT,
    REASON_EXPIRED,
    REASON_NOT_FETCHED,
    REASON_REQUEST_FAILED,
    HostRefreshMachine,
    HostRefreshPolicy,
)

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "host_summary"


def _now():
    return datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def live_payload(sample_age_s: int = 8, **over):
    """一份全部新鲜的投影响应。``over`` 可覆盖信封字段。"""
    moment = _now() - timedelta(seconds=sample_age_s)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    units = {
        "gpu_utilization_ratio": "ratio",
        "gpu_temperature_celsius": "celsius",
        "gpu_power_watts": "watts",
        "strata_up": "bool",
        "gpu_memory_used_mib": "mib",
        "gpu_memory_total_mib": "mib",
    }
    payload = {
        "version": 1,
        "source": "prometheus-readonly",
        "synthetic": False,
        "readonly": True,
        "state": "known",
        "observed_at": stamp,
        "metrics": {
            key: {
                "state": "known",
                "value": 1,
                "unit": unit,
                "source": "prometheus-readonly",
                "metric": "llmlab_strata_" + key,
                "series": 1,
                "age_s": sample_age_s,
                "sample_at": stamp,
            }
            for key, unit in units.items()
        },
        "services": {"prometheus": {"state": "known", "up": True, "reason": None}},
    }
    payload.update(over)
    return payload


class FixtureConformanceTest(unittest.TestCase):
    """契约 fixture 必须与校验器的判定一致 —— 这就是交接的凭据。"""

    def test_every_fixture_file_is_well_formed(self) -> None:
        files = sorted(FIXTURE_DIR.glob("*.json"))
        self.assertGreaterEqual(len(files), 10, "the boundary set shrank")
        for path in files:
            with self.subTest(fixture=path.name):
                doc = json.loads(path.read_text(encoding="utf-8"))
                for key in ("note", "now", "expect", "payload"):
                    self.assertIn(key, doc)
                for key in (
                    "accepted",
                    "any_live",
                    "live_keys",
                    "envelope_reason",
                    "metric_reasons",
                    "display_state",
                ):
                    self.assertIn(key, doc["expect"])

    def test_every_fixture_reaches_the_documented_verdict(self) -> None:
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            with self.subTest(fixture=path.name):
                doc = json.loads(path.read_text(encoding="utf-8"))
                now = datetime.fromisoformat(doc["now"].replace("Z", "+00:00"))
                verdict = validate_host_summary(doc["payload"], now=now)
                expect = doc["expect"]
                self.assertEqual(verdict.accepted, expect["accepted"])
                self.assertEqual(verdict.any_live, expect["any_live"])
                self.assertEqual(list(verdict.live_metric_keys), expect["live_keys"])
                self.assertEqual(verdict.reason, expect["envelope_reason"])
                reasons = sorted(
                    {item.reason for item in verdict.metrics.values() if not item.live}
                )
                self.assertEqual(reasons, expect["metric_reasons"])

    def test_no_fixture_lets_a_synthetic_or_untrusted_body_be_live(self) -> None:
        # 冗余但重要：这是整个契约里最不能出错的一条，值得单独再钉一次。
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            payload = doc["payload"]
            if isinstance(payload, dict) and (
                payload.get("synthetic") is not False
                or payload.get("source") != "prometheus-readonly"
            ):
                with self.subTest(fixture=path.name):
                    self.assertFalse(doc["expect"]["any_live"])


class PollingCadenceTest(unittest.TestCase):
    def test_the_first_tick_requests_immediately(self) -> None:
        machine = HostRefreshMachine(now=_now())
        self.assertEqual(machine.tick(_now()), 1)
        self.assertEqual(machine.in_flight, 1)

    def test_a_second_tick_inside_the_interval_does_not_request(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        self.assertIsNone(machine.tick(_now() + timedelta(seconds=29)))

    def test_the_interval_opens_a_new_request(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        self.assertEqual(machine.tick(_now() + timedelta(seconds=30)), 2)

    def test_the_interval_is_configurable(self) -> None:
        machine = HostRefreshMachine(
            now=_now(), policy=HostRefreshPolicy(interval_seconds=5)
        )
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        self.assertEqual(machine.tick(_now() + timedelta(seconds=5)), 2)

    def test_a_non_positive_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostRefreshPolicy(interval_seconds=0)

    def test_a_non_positive_in_flight_cap_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostRefreshPolicy(max_in_flight=0)


class SingleInFlightTest(unittest.TestCase):
    def test_a_slow_response_never_gets_a_second_request(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        # 响应迟迟不来，定时器已经又响了好几轮 —— 一次都不许再发。
        for seconds in (30, 60, 90, 120):
            with self.subTest(offset=seconds):
                self.assertIsNone(machine.tick(_now() + timedelta(seconds=seconds)))
        self.assertEqual(machine.in_flight, 1)
        self.assertEqual(machine.sent_seq, 1)

    def test_a_late_response_does_not_overwrite_a_newer_one(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        machine.tick(_now() + timedelta(seconds=30))  # seq 2，还没回来
        # seq 1 的重复回包迟到：必须被忽略，当前显示保持不被它改写。
        before = machine.display(_now() + timedelta(seconds=31))
        machine.response(_now() + timedelta(seconds=31), 1, live_payload(sample_age_s=80))
        after = machine.display(_now() + timedelta(seconds=31))
        self.assertEqual(before, after)

    def test_a_response_for_the_newest_request_is_applied(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        machine.tick(_now() + timedelta(seconds=30))
        machine.response(_now() + timedelta(seconds=30), 2, live_payload(sample_age_s=1))
        self.assertEqual(machine.display(_now() + timedelta(seconds=30))["state"], DISPLAY_LIVE)


class VisibilityTest(unittest.TestCase):
    def test_a_hidden_tab_does_not_poll(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.visibility(_now(), hidden=True)
        for seconds in (30, 60, 300):
            with self.subTest(offset=seconds):
                self.assertIsNone(machine.tick(_now() + timedelta(seconds=seconds)))
        self.assertEqual(machine.sent_seq, 0)

    def test_resuming_refreshes_immediately_once_the_interval_has_passed(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        machine.visibility(_now() + timedelta(seconds=300), hidden=True)
        # 恢复可见：越过间隔 -> 立刻刷，而不是再等满一个周期。
        self.assertEqual(
            machine.visibility(_now() + timedelta(seconds=301), hidden=False), 2
        )

    def test_resuming_inside_the_interval_waits(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        machine.visibility(_now() + timedelta(seconds=5), hidden=True)
        self.assertIsNone(machine.visibility(_now() + timedelta(seconds=6), hidden=False))


class ClearOnFailureTest(unittest.TestCase):
    def _live_machine(self):
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        self.assertEqual(machine.display(_now())["state"], DISPLAY_LIVE)
        return machine

    def test_a_failed_request_clears_a_previously_live_number(self) -> None:
        machine = self._live_machine()
        machine.tick(_now() + timedelta(seconds=30))
        machine.failure(_now() + timedelta(seconds=31), 2, REASON_REQUEST_FAILED)
        shown = machine.display(_now() + timedelta(seconds=31))
        self.assertEqual(shown["state"], DISPLAY_UNKNOWN)
        self.assertEqual(shown["reason"], REASON_REQUEST_FAILED)
        self.assertIsNone(shown["summary"])

    def test_a_contract_violation_clears_the_number(self) -> None:
        machine = self._live_machine()
        machine.tick(_now() + timedelta(seconds=30))
        machine.response(
            _now() + timedelta(seconds=31), 2, live_payload(synthetic=True)
        )
        shown = machine.display(_now() + timedelta(seconds=31))
        self.assertEqual(shown["state"], DISPLAY_UNKNOWN)
        self.assertTrue(shown["reason"].startswith(REASON_CONTRACT))

    def test_a_late_failure_does_not_clear_a_newer_success(self) -> None:
        machine = self._live_machine()
        machine.tick(_now() + timedelta(seconds=30))  # seq 2
        machine.response(_now() + timedelta(seconds=31), 2, live_payload(sample_age_s=1))
        machine.failure(_now() + timedelta(seconds=32), 1, REASON_REQUEST_FAILED)
        self.assertEqual(machine.display(_now() + timedelta(seconds=32))["state"], DISPLAY_LIVE)

    def test_before_the_first_response_the_panel_says_not_yet_fetched(self) -> None:
        machine = HostRefreshMachine(now=_now())
        shown = machine.display(_now())
        self.assertEqual(shown["state"], DISPLAY_UNKNOWN)
        self.assertEqual(shown["reason"], REASON_NOT_FETCHED)


class ExpiryTest(unittest.TestCase):
    def test_a_suspended_tab_expires_without_any_new_response(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload(sample_age_s=0))
        self.assertEqual(machine.display(_now())["state"], DISPLAY_LIVE)
        # 挂起一夜：没有新响应，只有时间在走。
        shown = machine.display(_now() + timedelta(hours=8))
        self.assertEqual(shown["state"], DISPLAY_UNKNOWN)
        self.assertEqual(shown["reason"], REASON_EXPIRED)
        self.assertIsNone(shown["summary"])

    def test_a_tick_also_expires_before_deciding_whether_to_poll(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload(sample_age_s=0))
        machine.tick(_now() + timedelta(seconds=100))
        self.assertEqual(machine.display(_now() + timedelta(seconds=100))["state"], DISPLAY_UNKNOWN)

    def test_a_sample_at_the_age_boundary_is_still_live(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload(sample_age_s=0))
        self.assertEqual(
            machine.display(_now() + timedelta(seconds=89))["state"], DISPLAY_LIVE
        )


class ClockRollbackTest(unittest.TestCase):
    def test_a_backwards_clock_clears_the_number_and_refetches(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload(sample_age_s=0))
        # 本地时钟倒退 10 分钟。
        seq = machine.tick(_now() - timedelta(minutes=10))
        self.assertEqual(seq, 2, "a rolled-back clock must force a fresh request")
        shown = machine.display(_now() - timedelta(minutes=10))
        self.assertEqual(shown["state"], DISPLAY_UNKNOWN)
        self.assertEqual(shown["reason"], REASON_CLOCK_ROLLBACK)

    def test_a_naive_clock_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HostRefreshMachine(now=datetime(2026, 10, 16, 12, 0, 0))

    def test_a_naive_tick_is_rejected(self) -> None:
        machine = HostRefreshMachine(now=_now())
        with self.assertRaises(ValueError):
            machine.tick(datetime(2026, 10, 16, 12, 0, 0))


class SnapshotAtomicityTest(unittest.TestCase):
    def test_the_display_carries_the_whole_summary_from_one_response(self) -> None:
        machine = HostRefreshMachine(now=_now())
        payload = live_payload()
        machine.tick(_now())
        machine.response(_now(), 1, payload)
        shown = machine.display(_now())
        self.assertEqual(shown["summary"]["services"]["prometheus"]["up"], True)
        self.assertEqual(set(shown["summary"]["metrics"]), set(payload["metrics"]))

    def test_a_cleared_display_never_keeps_a_stale_summary(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.response(_now(), 1, live_payload())
        machine.failure(_now(), 1, REASON_REQUEST_FAILED)
        self.assertIsNone(machine.display(_now())["summary"])


class AuditTrailTest(unittest.TestCase):
    def test_the_machine_records_why_it_skipped(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.tick(_now())
        machine.tick(_now() + timedelta(seconds=1))
        reasons = [event["detail"] for event in machine.events if event["event"] == "skip"]
        self.assertIn("in-flight", reasons)

    def test_a_hidden_tab_records_a_skip_rather_than_a_silent_no_op(self) -> None:
        machine = HostRefreshMachine(now=_now())
        machine.visibility(_now(), hidden=True)
        machine.tick(_now() + timedelta(seconds=1))
        self.assertIn(
            "hidden", [e["detail"] for e in machine.events if e["event"] == "skip"]
        )


if __name__ == "__main__":
    unittest.main()
