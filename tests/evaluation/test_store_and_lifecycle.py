"""持久层、状态机、幂等、配额与时间窗。

这一组测的是"同一个动作做两次会怎样"与"状态能不能乱跳"。评测任务的生命周期里，
幂等性不是优化而是正确性：一个重试按钮被双击，不应该产生两个任务；一个已经完成的
评测不该被"取消"。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.evaluation.errors import (
    Conflict,
    InvalidRequest,
    RateLimited,
    StorageError,
)
from backend.evaluation.limits import RateLimitPolicy, TimeWindow
from backend.evaluation.models import (
    ALLOWED_TRANSITIONS,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    can_transition,
    deterministic_job_id,
    validate_transition,
)
from backend.evaluation.store import AUDIT_ALLOWED_DETAIL_KEYS, SCHEMA_VERSION, Store
from tests.evaluation.support import (
    DEP_VINCE_PRIVATE,
    EPOCH,
    Clock,
    build_harness,
    close_all,
)

_LIVE = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


class StateMachineTests(unittest.TestCase):
    def test_the_terminal_states_are_terminal(self) -> None:
        for status in sorted(TERMINAL_STATUSES):
            with self.subTest(status=status):
                self.assertEqual(ALLOWED_TRANSITIONS[status], frozenset())

    def test_the_allowed_transitions_are_exactly_the_documented_ones(self) -> None:
        self.assertEqual(
            ALLOWED_TRANSITIONS[STATUS_QUEUED],
            frozenset({STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELED}),
        )
        self.assertEqual(
            ALLOWED_TRANSITIONS[STATUS_RUNNING],
            frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}),
        )

    def test_an_illegal_transition_is_refused(self) -> None:
        for current, target in (
            (STATUS_COMPLETED, STATUS_RUNNING),
            (STATUS_FAILED, STATUS_QUEUED),
            (STATUS_CANCELED, STATUS_RUNNING),
            (STATUS_QUEUED, STATUS_COMPLETED),  # 必须先经过 running
        ):
            with self.subTest(current=current, target=target):
                with self.assertRaises(Conflict):
                    validate_transition(current, target)

    def test_an_unknown_status_never_transitions(self) -> None:
        """未知状态一律 ``False``：fail closed，而不是"没规则就放行"。"""
        self.assertFalse(can_transition("bogus", STATUS_RUNNING))
        self.assertFalse(can_transition(STATUS_QUEUED, "bogus"))


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]
        self.clock = self.h.clock

    def test_the_same_idempotency_key_returns_the_same_job(self) -> None:
        first = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="dup-1")
        second = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="dup-1")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["replayed"])

    def test_the_job_id_is_derived_not_random(self) -> None:
        expected = deterministic_job_id(
            owner_principal="alice", project_id="proj-alpha", idempotency_key="dup-1"
        )
        first = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="dup-1")
        self.assertEqual(first["job_id"], expected)

    def test_reusing_a_key_for_a_different_request_is_a_conflict(self) -> None:
        self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="dup-2")
        status, _h, body = self.h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": "dep-shared-all", "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke", "idempotency_key": "dup-2"},
        )
        self.assertEqual(status, 409, msg=body)

    def test_a_replay_does_not_consume_quota(self) -> None:
        """否则"刷新一下页面"就能把一个人的配额烧光。"""
        clock = Clock()
        h = build_harness(
            clock=clock,
            config_overrides={
                "principal_submit_limit": RateLimitPolicy(limit=1, window_seconds=60)
            },
        )
        try:
            job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="replay")
            for _ in range(5):
                again = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="replay")
                self.assertEqual(again["job_id"], job["job_id"])
        finally:
            h.close()

    def test_the_same_key_in_two_projects_is_two_jobs(self) -> None:
        """摘要里包含 owner 与 project，因此键不会跨项目撞车。"""
        alice = deterministic_job_id(
            owner_principal="alice", project_id="proj-alpha", idempotency_key="k"
        )
        other = deterministic_job_id(
            owner_principal="alice", project_id="proj-beta", idempotency_key="k"
        )
        self.assertNotEqual(alice, other)

    def test_no_key_means_a_fresh_job_each_time(self) -> None:
        first = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        second = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.assertNotEqual(first["job_id"], second["job_id"])


class CancelLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]

    def test_cancelling_twice_is_idempotent(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="c-1")
        first = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        second = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertFalse(first[2]["data"]["already_canceled"])
        self.assertTrue(second[2]["data"]["already_canceled"])
        self.assertFalse(second[2]["data"]["changed"])
        self.assertEqual(second[2]["data"]["status"], STATUS_CANCELED)

    def test_cancelling_a_queued_job_moves_it_to_canceled(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        _s, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        self.assertEqual(body["data"]["status"], STATUS_CANCELED)
        self.assertTrue(body["data"]["terminal"])
        self.assertFalse(body["data"]["cancellable"])

    def test_cancelling_a_completed_job_is_a_conflict(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="c-done")
        self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice"
        )
        status, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        self.assertEqual(status, 409, msg=body)
        self.assertEqual(body["error"]["contract_code"], "evaluation.conflict")

    def test_a_cancel_requested_flag_is_visible_before_the_state_changes(self) -> None:
        """「请求取消」与「已经取消」是两件事，前者先于后者可见。"""
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.h.store.set_cancel_requested(job["job_id"], at=self.h.clock())
        _s, _h, body = self.h.request(
            "GET", f"/api/evaluation/jobs/{job['job_id']}", as_principal="alice"
        )
        self.assertTrue(body["data"]["cancel_requested"])
        self.assertEqual(body["data"]["status"], STATUS_QUEUED)


class ConcurrentTransitionTests(unittest.TestCase):
    """并发下的取消：状态迁移是**带条件写入**，不是读-改-写。

    这一组用真实线程去撞同一个任务。结论必须只有一个：**恰好一次**迁移生效，
    其余调用走幂等分支，且不抛异常、不产生第二条"已取消"的审计。
    """

    def test_two_threads_cancelling_at_once_transition_exactly_once(self) -> None:
        import threading

        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="race-store")
        barrier = threading.Barrier(2)
        results: list[object] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait(timeout=5)
            outcome = h.store.transition_job(
                job["job_id"],
                expected_status=STATUS_QUEUED,
                new_status=STATUS_CANCELED,
                at=h.clock(),
                finished_at=h.clock(),
            )
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        winners = [item for item in results if item is not None]
        losers = [item for item in results if item is None]
        self.assertEqual(len(winners), 1, msg=results)
        self.assertEqual(len(losers), 1, msg=results)
        self.assertEqual(h.store.get_job(job["job_id"]).status, STATUS_CANCELED)

    def test_two_threads_cancelling_through_the_service_both_succeed_once(self) -> None:
        import threading

        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="race-svc")
        barrier = threading.Barrier(2)
        payloads: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait(timeout=5)
            try:
                _s, _h, body = h.request(
                    "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel",
                    as_principal="alice",
                )
                with lock:
                    payloads.append(body["data"])
            except BaseException as exc:  # noqa: BLE001 - 测试要把任何异常都记下来
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [], msg=errors)
        self.assertEqual(len(payloads), 2, msg=payloads)
        # 两次调用都成功，但**只有一次**真的改变了状态。
        self.assertEqual(sum(1 for item in payloads if item["changed"]), 1, msg=payloads)
        self.assertEqual(sum(1 for item in payloads if item["already_canceled"]), 1, msg=payloads)
        self.assertTrue(all(item["status"] == STATUS_CANCELED for item in payloads))

        # 审计里"已取消"只出现一次：这就是"恰好一次迁移"的证据。
        cancelled = [
            entry
            for entry in h.store.list_audit(job_id=job["job_id"])
            if entry.action == "cancel" and entry.outcome == "canceled"
        ]
        self.assertEqual(len(cancelled), 1, msg=[e.to_dict() for e in cancelled])


class RetryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]

    def _fail_a_job(self, key: str) -> str:
        """把任务推进到 failed（用存储层的条件迁移，模拟一次失败的执行）。"""
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key=key)
        self.h.store.transition_job(
            job["job_id"], expected_status=STATUS_QUEUED, new_status=STATUS_FAILED,
            at=self.h.clock(), finished_at=self.h.clock(),
            failure_code="runner_error", failure_detail="synthetic failure",
        )
        return job["job_id"]

    def test_a_failed_job_can_be_retried_into_a_new_job(self) -> None:
        original = self._fail_a_job("r-1")
        _s, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{original}/retry", as_principal="alice"
        )
        retried = body["data"]
        self.assertNotEqual(retried["job_id"], original)
        self.assertEqual(retried["retry_of"], original)
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(retried["status"], STATUS_QUEUED)

    def test_retrying_twice_returns_one_new_job(self) -> None:
        original = self._fail_a_job("r-2")
        _s, _h, first = self.h.request(
            "POST", f"/api/evaluation/jobs/{original}/retry", as_principal="alice"
        )
        _s2, _h2, second = self.h.request(
            "POST", f"/api/evaluation/jobs/{original}/retry", as_principal="alice"
        )
        self.assertEqual(first["data"]["job_id"], second["data"]["job_id"])
        self.assertTrue(second["data"]["replayed"])

    def test_a_queued_job_cannot_be_retried(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        status, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
        )
        self.assertEqual(status, 409, msg=body)
        self.assertEqual(body["error"]["contract_code"], "evaluation.conflict")

    def test_a_running_job_cannot_be_retried(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.h.store.transition_job(
            job["job_id"], expected_status=STATUS_QUEUED, new_status=STATUS_RUNNING,
            at=self.h.clock(), started_at=self.h.clock(),
        )
        status, _h, _body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
        )
        self.assertEqual(status, 409)

    def test_a_canceled_job_can_be_retried(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="r-cancel")
        self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        _s, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
        )
        self.assertEqual(body["data"]["attempt"], 2)

    def test_the_original_job_survives_a_retry(self) -> None:
        """重试是**新任务**，原任务（含它的失败原因）不被改写。"""
        original = self._fail_a_job("r-keep")
        self.h.request("POST", f"/api/evaluation/jobs/{original}/retry", as_principal="alice")
        _s, _h, body = self.h.request(
            "GET", f"/api/evaluation/jobs/{original}", as_principal="alice"
        )
        self.assertEqual(body["data"]["status"], STATUS_FAILED)
        self.assertEqual(body["data"]["failure"]["code"], "runner_error")

    def test_a_retry_re_authorizes_against_the_current_target_directory(self) -> None:
        """共享被撤回后，**新的**任务不再生成——但已存在的任务不受影响。"""
        from backend.evaluation.targets import TargetCatalog, TargetRecord

        h = build_harness()
        try:
            job = h.submit("alice", deployment_id="dep-shared-users",
                           idempotency_key="revoke-1")
            h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/cancel",
                      as_principal="alice")

            # 目录变了：该目标不再共享给 alice（默认 private）。
            h.service.replace_targets(
                TargetCatalog(
                    [
                        TargetRecord(
                            "dep-shared-users", "proj-beta", "dave", 4002, ready=True,
                            model_ref="mdl-shared-synth",
                        )
                    ]
                )
            )
            status, _headers, body = h.request(
                "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
            )
            self.assertEqual(status, 404, msg=body)
            self.assertEqual(body["error"]["contract_code"], "evaluation.not_found")

            # 原任务仍然属于 alice，仍然读得到（撤回授权不改写历史）。
            _s, _h, own = h.request(
                "GET", f"/api/evaluation/jobs/{job['job_id']}", as_principal="alice"
            )
            self.assertEqual(own["data"]["status"], "canceled")
        finally:
            h.close()

    def test_a_retry_refuses_when_the_target_is_no_longer_verified(self) -> None:
        from backend.evaluation.targets import TargetCatalog, TargetRecord

        h = build_harness()
        try:
            job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="unver-1")
            h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/cancel",
                      as_principal="alice")
            h.service.replace_targets(
                TargetCatalog(
                    [
                        TargetRecord(
                            DEP_VINCE_PRIVATE, "proj-alpha", "alice", 4001, ready=False,
                            model_ref="mdl-qwen-synth",
                        )
                    ]
                )
            )
            status, _headers, body = h.request(
                "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
            )
            self.assertEqual(status, 409, msg=body)
            self.assertEqual(body["error"]["contract_code"], "evaluation.target_not_ready")
        finally:
            h.close()

    def test_a_retry_refuses_the_stress_type_after_a_grant_is_revoked(self) -> None:
        from backend.evaluation.targets import TargetCatalog, TargetRecord

        h = build_harness()
        try:
            job = h.submit(
                "alice", deployment_id="dep-shared-stress-granted", eval_type="throughput",
                dataset_slug="synthetic-gen-smoke", idempotency_key="stress-revoke",
            )
            h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/cancel",
                      as_principal="alice")
            # 压测许可被撤回（成员仍在 grantees 里）。
            h.service.replace_targets(
                TargetCatalog(
                    [
                        TargetRecord(
                            "dep-shared-stress-granted", "proj-beta", "dave", 4002,
                            ready=True, visibility="users", grantees=frozenset({"alice"}),
                            model_ref="mdl-stress-synth",
                        )
                    ]
                )
            )
            status, _headers, body = h.request(
                "POST", f"/api/evaluation/jobs/{job['job_id']}/retry", as_principal="alice"
            )
            self.assertEqual(status, 403, msg=body)
            self.assertEqual(
                body["error"]["contract_code"], "evaluation.shared_target_gpu_stress_refused"
            )
        finally:
            h.close()

    def test_a_retry_of_a_retry_increments_the_attempt(self) -> None:
        original = self._fail_a_job("r-chain")
        _s, _h, first = self.h.request(
            "POST", f"/api/evaluation/jobs/{original}/retry", as_principal="alice"
        )
        second_id = first["data"]["job_id"]
        self.h.store.transition_job(
            second_id, expected_status=STATUS_QUEUED, new_status=STATUS_FAILED,
            at=self.h.clock(), finished_at=self.h.clock(), failure_code="runner_error",
        )
        _s2, _h2, second = self.h.request(
            "POST", f"/api/evaluation/jobs/{second_id}/retry", as_principal="alice"
        )
        self.assertEqual(second["data"]["attempt"], 3)


class RateLimitTests(unittest.TestCase):
    def test_exceeding_the_principal_quota_is_429_with_retry_after(self) -> None:
        clock = Clock()
        h = build_harness(
            clock=clock,
            config_overrides={
                "principal_submit_limit": RateLimitPolicy(limit=2, window_seconds=60),
                "global_submit_limit": RateLimitPolicy(
                    limit=100, window_seconds=60, scope="evaluation.submit.global"
                ),
            },
        )
        try:
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            status, headers, body = h.request(
                "POST", "/api/evaluation/jobs", as_principal="alice",
                payload={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                         "dataset_slug": "synthetic-qa-smoke"},
            )
            self.assertEqual(status, 429, msg=body)
            self.assertEqual(body["error"]["contract_code"], "evaluation.rate_limited")
            self.assertIn("Retry-After", headers)
            self.assertGreater(int(headers["Retry-After"]), 0)
        finally:
            h.close()

    def test_the_window_slides_and_frees_the_quota(self) -> None:
        clock = Clock()
        h = build_harness(
            clock=clock,
            config_overrides={
                "principal_submit_limit": RateLimitPolicy(limit=1, window_seconds=60)
            },
        )
        try:
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            with self.assertRaises(RateLimited):
                h.service.submit(
                    requester=h.requester("alice"),
                    request={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                             "dataset_slug": "synthetic-qa-smoke"},
                )
            clock.advance(seconds=61)
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        finally:
            h.close()

    def test_the_quota_is_per_principal_not_global(self) -> None:
        clock = Clock()
        h = build_harness(
            clock=clock,
            config_overrides={
                "principal_submit_limit": RateLimitPolicy(limit=1, window_seconds=60),
                "global_submit_limit": RateLimitPolicy(
                    limit=100, window_seconds=60, scope="evaluation.submit.global"
                ),
            },
        )
        try:
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            h.submit("bob", deployment_id="dep-bob-private")
        finally:
            h.close()

    def test_the_global_ceiling_applies_across_principals(self) -> None:
        clock = Clock()
        h = build_harness(
            clock=clock,
            config_overrides={
                "principal_submit_limit": RateLimitPolicy(limit=10, window_seconds=60),
                "global_submit_limit": RateLimitPolicy(
                    limit=2, window_seconds=60, scope="evaluation.submit.global"
                ),
            },
        )
        try:
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            h.submit("bob", deployment_id="dep-bob-private")
            with self.assertRaises(RateLimited):
                h.service.submit(
                    requester=h.requester("alice"),
                    request={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                             "dataset_slug": "synthetic-qa-smoke"},
                )
        finally:
            h.close()

    def test_the_retry_after_is_bounded_by_the_window(self) -> None:
        policy = RateLimitPolicy(limit=1, window_seconds=30)
        decision = policy.evaluate(used=1, now=EPOCH, oldest_event=EPOCH)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_after, 30)

    def test_the_retry_after_accounts_for_the_oldest_event(self) -> None:
        policy = RateLimitPolicy(limit=1, window_seconds=60)
        decision = policy.evaluate(used=1, now=EPOCH, oldest_event=EPOCH - timedelta(seconds=50))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_after, 10)

    def test_a_bad_policy_is_refused_at_construction(self) -> None:
        for kwargs in ({"limit": 0}, {"limit": -1}, {"limit": True},
                       {"limit": 1, "window_seconds": 0},
                       {"limit": 1, "window_seconds": 999_999}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidRequest):
                    RateLimitPolicy(**kwargs)


class RetryRequestFieldTests(unittest.TestCase):
    """重试**不接受**任何请求字段。

    以前它接受一个 ``idempotency_key``，但 :meth:`retry_job` 总是自己推出键，于是那个
    字段被静默丢弃。一个"接受了但没有效果"的字段比不接受更糟：调用方会以为自己控制了
    幂等范围。现在它是未知字段 -> 400。
    """

    def setUp(self) -> None:
        self.h = _LIVE[0]

    def _canceled_job(self, key: str) -> str:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key=key)
        self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/cancel", as_principal="alice"
        )
        return job["job_id"]

    def test_a_retry_body_with_a_field_is_rejected(self) -> None:
        job_id = self._canceled_job("retry-field-1")
        for payload in ({"idempotency_key": "x"}, {"reason": "y"}, {"owner": "carol"}):
            with self.subTest(payload=payload):
                status, _headers, body = self.h.request(
                    "POST", f"/api/evaluation/jobs/{job_id}/retry",
                    as_principal="alice", payload=payload,
                )
                self.assertEqual(status, 400, msg=body)

    def test_a_bodyless_retry_works(self) -> None:
        job_id = self._canceled_job("retry-field-2")
        status, _headers, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job_id}/retry", as_principal="alice"
        )
        self.assertEqual(status, 200, msg=body)
        self.assertEqual(body["data"]["attempt"], 2)

    def test_the_retry_contract_declares_no_fields(self) -> None:
        from backend.evaluation.service import RETRY_REQUEST_FIELDS

        self.assertEqual(RETRY_REQUEST_FIELDS, frozenset())


class MonitoringConsistencyTests(unittest.TestCase):
    """计数与列表必须用同一组可见性过滤条件。"""

    def test_counts_match_the_visible_list(self) -> None:
        h = build_harness()
        try:
            for index in range(3):
                h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key=f"mon-{index}")
            h.submit("dave", deployment_id="dep-dave-private", idempotency_key="mon-z")

            for principal in ("alice", "dave", "carol"):
                with self.subTest(principal=principal):
                    _s, _h, mon = h.request(
                        "GET", "/api/evaluation/monitoring", as_principal=principal
                    )
                    _s2, _h2, listed = h.request(
                        "GET", "/api/evaluation/jobs", as_principal=principal
                    )
                    self.assertEqual(
                        mon["data"]["jobs_total"],
                        len(listed["data"]["jobs"]),
                        msg="panel total disagrees with the visible list",
                    )
                    self.assertEqual(
                        mon["data"]["jobs_total"], sum(mon["data"]["jobs_by_status"].values())
                    )
        finally:
            h.close()


class TimeWindowTests(unittest.TestCase):
    def test_an_unrestricted_window_always_contains_now(self) -> None:
        window = TimeWindow.unrestricted_window()
        self.assertTrue(window.contains(EPOCH))
        self.assertTrue(window.unrestricted)

    def test_a_closed_window_never_contains_now(self) -> None:
        window = TimeWindow.closed()
        self.assertFalse(window.contains(EPOCH))
        self.assertTrue(window.always_closed)
        self.assertEqual(window.describe(), "closed")

    def test_a_daytime_window_has_the_expected_boundaries(self) -> None:
        window = TimeWindow.parse("02:00", "06:00")
        self.assertTrue(window.contains(EPOCH.replace(hour=2, minute=0)))
        self.assertTrue(window.contains(EPOCH.replace(hour=5, minute=59)))
        self.assertFalse(window.contains(EPOCH.replace(hour=6, minute=0)))
        self.assertFalse(window.contains(EPOCH.replace(hour=1, minute=59)))

    def test_a_window_can_wrap_past_midnight(self) -> None:
        window = TimeWindow.parse("22:00", "06:00")
        self.assertTrue(window.contains(EPOCH.replace(hour=23, minute=30)))
        self.assertTrue(window.contains(EPOCH.replace(hour=1, minute=0)))
        self.assertFalse(window.contains(EPOCH.replace(hour=12, minute=0)))

    def test_a_window_needs_both_ends(self) -> None:
        with self.assertRaises(InvalidRequest):
            TimeWindow(start_minute=600, end_minute=None)

    def test_a_malformed_window_is_refused(self) -> None:
        for start, end in (("25:00", "06:00"), ("2:00", "06:00"), ("22:70", "06:00"),
                           ("", "06:00"), (None, "06:00")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(InvalidRequest):
                    TimeWindow.parse(start, end)

    def test_a_naive_datetime_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            TimeWindow.unrestricted_window().contains(datetime(2026, 10, 15, 12, 0, 0))


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]

    def test_the_whitelist_covers_the_operational_keys(self) -> None:
        for key in ("eval_type", "dataset_slug", "deployment_id", "target_owner",
                    "from_status", "to_status", "attempt", "synthetic", "runner_kind"):
            with self.subTest(key=key):
                self.assertIn(key, AUDIT_ALLOWED_DETAIL_KEYS)

    def test_content_like_keys_are_refused(self) -> None:
        h = self.h
        for key in ("prompt", "completion", "content", "report_body", "params",
                    "weight_path", "command"):
            with self.subTest(key=key):
                with self.assertRaises(InvalidRequest):
                    h.store.record_audit(
                        actor_principal="alice", actor_uid=4001, action="submit",
                        outcome="accepted", detail={key: "anything"},
                    )

    def test_the_lifecycle_is_auditable(self) -> None:
        h = self.h
        # 一条走到完成的路径：submit -> mock_run。
        completed = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="audit-1")
        h.request(
            "POST", f"/api/evaluation/jobs/{completed['job_id']}/mock-run", as_principal="alice"
        )
        actions = {entry.action for entry in h.store.list_audit(job_id=completed["job_id"])}
        self.assertTrue({"submit", "mock_run"} <= actions, msg=actions)

        # 一条走到取消再重试的路径。重试新建的那个任务自己也有一条 submit 记录。
        failed = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="audit-1b")
        h.request("POST", f"/api/evaluation/jobs/{failed['job_id']}/cancel", as_principal="alice")
        _s, _h, retried = h.request(
            "POST", f"/api/evaluation/jobs/{failed['job_id']}/retry", as_principal="alice"
        )
        origin_actions = {entry.action for entry in h.store.list_audit(job_id=failed["job_id"])}
        self.assertTrue({"submit", "cancel", "retry"} <= origin_actions, msg=origin_actions)
        new_actions = {
            entry.action for entry in h.store.list_audit(job_id=retried["data"]["job_id"])
        }
        self.assertIn("submit", new_actions, msg=new_actions)

    def test_every_audit_entry_records_the_actor_from_the_credential(self) -> None:
        h = self.h
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="audit-2")
        entries = h.store.list_audit(job_id=job["job_id"])
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(action=entry.action):
                self.assertEqual(entry.actor_principal, "alice")
                self.assertEqual(entry.actor_uid, 4001)

    def test_the_audit_detail_carries_no_content(self) -> None:
        h = self.h
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="audit-3")
        for entry in h.store.list_audit(job_id=job["job_id"]):
            payload = entry.to_dict()["detail"]
            self.assertTrue(set(payload) <= AUDIT_ALLOWED_DETAIL_KEYS, msg=payload)


class StoreTests(unittest.TestCase):
    def test_a_fresh_store_reports_the_current_schema_version(self) -> None:
        store = Store(":memory:")
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
        finally:
            store.close()

    def test_an_unknown_schema_version_refuses_to_run(self) -> None:
        """一个读错了列语义的评测库，产出的报告比没有报告更糟。"""
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            connection = sqlite3.connect(handle.name)
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
            connection.close()
            with self.assertRaises(StorageError):
                Store(handle.name)

    def test_listing_without_an_owner_or_the_global_flag_returns_nothing(self) -> None:
        h = _LIVE[0]
        self.assertEqual(h.store.list_jobs(owner_principal=None, all_owners=False), [])

    def test_the_list_limit_is_bounded(self) -> None:
        """上限必须**真的**被夹住。

        （这条测试的前一版写的是 ``list_jobs(limit=100_000)[:0] == []`` ——
        ``[:0]`` 对**任何**列表都是 ``[]``，所以它连"上限存在"都证明不了。
        现在把上限临时调到 3，插入 5 条，数一下真的回来了几条。）
        """
        from unittest import mock

        import backend.evaluation.store as store_module

        h = build_harness()
        try:
            for index in range(5):
                h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key=f"cap-{index}")

            with mock.patch.object(store_module, "MAX_LIST_LIMIT", 3):
                clamped = h.store.list_jobs(owner_principal="alice", limit=100_000)
                self.assertEqual(len(clamped), 3, msg="the cap was not applied")

            # 调用方声明的页大小仍然生效，并且同样受上限约束。
            self.assertEqual(len(h.store.list_jobs(owner_principal="alice", limit=2)), 2)
            self.assertEqual(len(h.store.list_jobs(owner_principal="alice", limit=1)), 1)

            # 非法页大小是错误，不是"收敛成 1"。
            with self.assertRaises(InvalidRequest):
                h.store.list_jobs(owner_principal="alice", limit=0)
            with self.assertRaises(InvalidRequest):
                h.store.list_jobs(owner_principal="alice", limit=True)
        finally:
            h.close()

    def test_an_unparseable_limit_over_http_is_400(self) -> None:
        status, _h, body = _LIVE[0].request(
            "GET", "/api/evaluation/jobs", as_principal="alice", query="limit=abc"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["contract_code"], "evaluation.invalid_request")

    def test_a_duplicated_query_parameter_is_refused(self) -> None:
        status, _h, _body = _LIVE[0].request(
            "GET", "/api/evaluation/jobs", as_principal="alice", query="limit=1&limit=2"
        )
        self.assertEqual(status, 400)

    def test_results_are_versioned_not_overwritten(self) -> None:
        """第二个版本必须**追加**，而不是覆盖第一个。

        （这条测试的前一版只做了一次 mock-run 再断言 ``[1]`` —— "不被覆盖"恰恰是它
        无法观测的事情。现在真的写一个第二版本，并逐版本读回来比对。）
        """
        from backend.evaluation.models import EvalResult, metric_known, new_result_id

        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, idempotency_key="ver-1")
        h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice")

        first = h.store.get_result(job["job_id"])
        assert first is not None
        self.assertEqual(first.version, 1)
        first_accuracy = first.metrics["accuracy"].value

        second = h.store.add_result(
            EvalResult(
                result_id=new_result_id(),
                job_id=job["job_id"],
                version=1,  # 库自己分配；这里故意传 1 以证明版本不由调用方决定
                synthetic=True,
                metrics={"accuracy": metric_known("accuracy", 0.75, unit="ratio")},
                report_ref="synthetic/rerun/attempt-2.json",
                created_at=h.clock(),
            )
        )
        self.assertEqual(second.version, 2, msg="the store must allocate the version")

        # 两个版本都还在，且各自的内容没有被改写。
        versions = [item.version for item in h.store.list_results(job["job_id"])]
        self.assertEqual(versions, [2, 1])
        self.assertEqual(h.store.get_result(job["job_id"]).version, 2)
        kept = h.store.get_result(job["job_id"], version=1)
        assert kept is not None
        self.assertEqual(kept.version, 1)
        self.assertEqual(kept.metrics["accuracy"].value, first_accuracy)
        self.assertNotEqual(kept.result_id, second.result_id)

        # 结果端点默认给最新版本，``?version=1`` 给第一版（这条曾经在 HTTP 面上失效）。
        _s, _h, latest = h.request(
            "GET", f"/api/evaluation/jobs/{job['job_id']}/result", as_principal="alice"
        )
        self.assertEqual(latest["data"]["result"]["version"], 2)
        self.assertEqual(latest["data"]["versions_available"], [2, 1])
        _s2, _h2, old_view = h.request(
            "GET", f"/api/evaluation/jobs/{job['job_id']}/result", as_principal="alice",
            query="version=1",
        )
        self.assertEqual(old_view["data"]["result"]["version"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
