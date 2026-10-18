"""GPU 准入与 runner 适配层。

两条主线：

**准入是一个合取。** 每一道闸门单独不成立都必须拒绝，而且拒绝原因要说清楚是**哪几道**。
本部署的默认装配里每一道都不成立，所以 ``allowed`` 恒为 ``False``。

**mock 永不冒充真实。** 这不是"mock 恰好返回了 True 的 synthetic 字段"，而是一串
结构性事实：mock 的 ``run()`` 里 ``synthetic`` 是写死的常量；一个 ``synthetic_only``
的 runner 不可能让准入变绿；即使有人注入乐观的 runner 事实，装了 mock 的部署仍然
拿不到真实派发。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.evaluation.admission import (
    GPU_NOT_CONFIGURED,
    REASON_ADAPTER_VERSION_UNKNOWN,
    REASON_ADMIN_DISPATCH_DISABLED,
    REASON_DISPATCH_SCOPE_NOT_GRANTED,
    REASON_GPU_FACTS_NOT_OBSERVED,
    REASON_INSUFFICIENT_GPU_HEADROOM,
    REASON_OUTSIDE_TIME_WINDOW,
    REASON_REAL_RUNNER_NOT_AVAILABLE,
    AdmissionPolicy,
    GpuFacts,
    RunnerFacts,
    all_reasons,
    evaluate_admission,
    summarize_reasons,
)
from backend.evaluation.errors import InvalidRequest, RunnerUnavailable
from backend.evaluation.limits import TimeWindow
from backend.evaluation.runner import (
    ADAPTER_NOT_CONFIGURED,
    ADAPTER_NOT_WIRED,
    ADAPTER_VERSION_UNKNOWN,
    EVALSCOPE_PRODUCTION_PORT,
    EvalScopeCliAdapter,
    EvalScopeHttpAdapter,
    MockRunner,
    RunnerSample,
    VersionGate,
    parse_runner_base_url,
    parse_version,
    redact_base_url,
    runner_facts_from,
)
from tests.evaluation.support import DEP_VINCE_PRIVATE, EPOCH, build_harness, close_all

_LIVE = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


def open_window() -> TimeWindow:
    return TimeWindow.unrestricted_window()


def good_runner() -> RunnerFacts:
    return RunnerFacts(
        real_runner_available=True, adapter_kind="evalscope_http", adapter_version="1.2.0"
    )


def good_gpu(free_mib: int = 20_000) -> GpuFacts:
    return GpuFacts(observed=True, free_mib=free_mib, total_mib=49_140, source="synthetic-probe")


class DefaultDeploymentTests(unittest.TestCase):
    def test_the_default_harness_never_allows_dispatch(self) -> None:
        decision = _LIVE[0].service.admission_decision()
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.synthetic_only)

    def test_every_gate_reports_its_own_reason(self) -> None:
        decision = _LIVE[0].service.admission_decision()
        self.assertEqual(
            set(decision.reasons),
            {
                REASON_ADMIN_DISPATCH_DISABLED,
                REASON_DISPATCH_SCOPE_NOT_GRANTED,
                REASON_REAL_RUNNER_NOT_AVAILABLE,
                REASON_ADAPTER_VERSION_UNKNOWN,
                REASON_OUTSIDE_TIME_WINDOW,
                REASON_GPU_FACTS_NOT_OBSERVED,
            },
        )
        # 主原因必须是清单里的第一个，供 UI 显示一句话。
        self.assertEqual(decision.reason, REASON_ADMIN_DISPATCH_DISABLED)

    def test_the_mock_version_is_not_reported_as_an_adapter_version(self) -> None:
        """mock 自称的 ``1.0.0`` 不是 EvalScope 适配器的版本。报出去会误导运维以为
        真有一个 EvalScope 1.0.0 在位。"""
        decision = _LIVE[0].service.admission_decision()
        self.assertIsNone(decision.runner.adapter_version)
        self.assertIsNone(decision.runner.adapter_kind)
        self.assertIn(REASON_ADAPTER_VERSION_UNKNOWN, decision.reasons)

    def test_the_mock_runner_is_still_described_where_it_belongs(self) -> None:
        """runner 自身的信息走 ``capabilities()``，不挤进"真实适配器"的位置。"""
        capabilities = _LIVE[0].service.capabilities()
        self.assertEqual(capabilities["adapter"]["kind"], "mock")
        self.assertTrue(capabilities["adapter"]["synthetic_only"])
        self.assertFalse(capabilities["runner_can_run_real"])

    def test_the_reason_order_is_stable(self) -> None:
        reasons = summarize_reasons(reversed(all_reasons()))
        self.assertEqual(reasons, list(all_reasons()))

    def test_the_submission_still_queues_and_records_the_refusal(self) -> None:
        job = _LIVE[0].submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["dispatch"]["state"], "refused")
        self.assertFalse(job["dispatch"]["dispatched"])
        self.assertTrue(job["dispatch"]["reason"])


class DispatchScopeGateTests(unittest.TestCase):
    """主体级派发许可：真实派发不是"部署开了就人人可派发"。

    这道门在文档里写过（"真实派发还需要 ``evaluation:dispatch:real``"），但实现里
    缺失过一段时间——一个只在文档里成立的模型，等于没有这个模型。
    """

    def _decide(self, *, granted: bool):
        return evaluate_admission(
            AdmissionPolicy(
                dispatch_enabled=True, window=open_window(), min_free_mib=1
            ),
            runner=good_runner(),
            gpu=good_gpu(),
            now=EPOCH,
            dispatch_scope_granted=granted,
        )

    def test_a_fully_open_deployment_still_needs_the_subject_grant(self) -> None:
        decision = self._decide(granted=False)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, (REASON_DISPATCH_SCOPE_NOT_GRANTED,))

    def test_granting_it_is_the_last_thing_needed(self) -> None:
        decision = self._decide(granted=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())

    def test_the_default_is_not_granted(self) -> None:
        """一个没声明自己持有它的调用者，不该被当作持有它。"""
        decision = evaluate_admission(
            AdmissionPolicy(dispatch_enabled=True, window=open_window(), min_free_mib=1),
            runner=good_runner(),
            gpu=good_gpu(),
            now=EPOCH,
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_DISPATCH_SCOPE_NOT_GRANTED, decision.reasons)

    def test_no_subject_in_this_deployment_holds_it(self) -> None:
        h = _LIVE[0]
        for principal in ("carol", "alice", "dave", "bob"):
            with self.subTest(principal=principal):
                self.assertFalse(
                    h.service.dispatch_scope_granted(h.requester(principal))
                )
        # 连匿名（无主体）也不例外。
        self.assertFalse(h.service.dispatch_scope_granted(None))

    def test_a_bogus_flag_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            evaluate_admission(
                AdmissionPolicy(dispatch_enabled=True, window=open_window()),
                runner=good_runner(),
                gpu=good_gpu(),
                now=EPOCH,
                dispatch_scope_granted="yes",  # type: ignore[arg-type]
            )


class AdmissionGateTests(unittest.TestCase):
    """逐道闸门：单独打开一道，仍然不通过。"""

    def _decide(
        self, *, policy: AdmissionPolicy, runner: RunnerFacts, gpu: GpuFacts, granted: bool = True
    ):
        """默认 ``granted=True``：这一组测的是除"主体级派发许可"之外的闸门。

        那道闸门有它自己的测试（:class:`DispatchScopeGateTests`）。
        """
        return evaluate_admission(
            policy, runner=runner, gpu=gpu, now=EPOCH, dispatch_scope_granted=granted
        )

    def test_only_the_admin_switch_is_enough_to_still_fail(self) -> None:
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=open_window()),
            runner=RunnerFacts.unavailable(reason="not wired"),
            gpu=GpuFacts.not_configured(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_REAL_RUNNER_NOT_AVAILABLE, decision.reasons)
        self.assertIn(REASON_GPU_FACTS_NOT_OBSERVED, decision.reasons)

    def test_an_unknown_adapter_version_blocks_dispatch(self) -> None:
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=open_window()),
            runner=RunnerFacts(real_runner_available=True, adapter_kind="evalscope_http"),
            gpu=good_gpu(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_ADAPTER_VERSION_UNKNOWN, decision.reasons)

    def test_a_closed_window_blocks_dispatch(self) -> None:
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=TimeWindow.closed()),
            runner=good_runner(),
            gpu=good_gpu(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_OUTSIDE_TIME_WINDOW, decision.reasons)
        self.assertFalse(decision.window_open)

    def test_insufficient_gpu_headroom_blocks_dispatch(self) -> None:
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=open_window(), min_free_mib=8192),
            runner=good_runner(),
            gpu=good_gpu(free_mib=1024),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_INSUFFICIENT_GPU_HEADROOM, decision.reasons)

    def test_unobserved_gpu_facts_are_not_treated_as_headroom(self) -> None:
        """没观测到不是"充足"，也不是 0。"""
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=open_window()),
            runner=good_runner(),
            gpu=GpuFacts.not_configured(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_GPU_FACTS_NOT_OBSERVED, decision.reasons)
        self.assertEqual(decision.gpu.state, GPU_NOT_CONFIGURED)
        self.assertIsNone(decision.gpu.free_mib)

    def test_every_gate_open_permits_dispatch(self) -> None:
        decision = self._decide(
            policy=AdmissionPolicy(dispatch_enabled=True, window=open_window(), min_free_mib=8192),
            runner=good_runner(),
            gpu=good_gpu(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())
        self.assertIsNone(decision.reason)
        self.assertFalse(decision.synthetic_only)

    def test_the_reasons_are_reported_in_full_not_one_at_a_time(self) -> None:
        """运维需要的是"要改哪几件事"，不是逐个试错的清单。

        这里刻意让**每一道**闸门都不成立：策略全关、runner 断线、版本未知、显存未观测，
        且不授予主体级派发许可。于是六条原因应当一次报全，而不是只报第一条。
        """
        decision = evaluate_admission(
            AdmissionPolicy.closed(),
            runner=RunnerFacts.unavailable(reason="not wired"),
            gpu=GpuFacts.not_configured(),
            now=EPOCH,
            dispatch_scope_granted=False,
        )
        self.assertEqual(
            set(decision.reasons),
            {
                REASON_ADMIN_DISPATCH_DISABLED,
                REASON_DISPATCH_SCOPE_NOT_GRANTED,
                REASON_REAL_RUNNER_NOT_AVAILABLE,
                REASON_ADAPTER_VERSION_UNKNOWN,
                REASON_OUTSIDE_TIME_WINDOW,
                REASON_GPU_FACTS_NOT_OBSERVED,
            },
        )
        # ``insufficient_gpu_headroom`` 不可能与 ``gpu_facts_not_observed`` 同时出现：
        # 前者是"测到了但不够"，后者是"没测到"。它们互斥，因此这里是六条而不是七条。
        self.assertNotIn(REASON_INSUFFICIENT_GPU_HEADROOM, decision.reasons)
        self.assertEqual(len(decision.reasons), 6)


class GpuFactsInvariantTests(unittest.TestCase):
    def test_unobserved_facts_must_not_carry_numbers(self) -> None:
        """把"没测到"写成 0 是这一步滑坡的第一级。"""
        for kwargs in ({"free_mib": 0}, {"total_mib": 0}, {"free_mib": 45469}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidRequest):
                    GpuFacts(observed=False, **kwargs)

    def test_negative_numbers_are_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            GpuFacts(observed=True, free_mib=-1, total_mib=100)

    def test_observed_facts_must_carry_numbers(self) -> None:
        """``observed=True`` 却没有数字是自相矛盾的，而且会开一个洞：
        准入里"显存够不够"那一条会被跳过，于是一份什么都没测到的"观测"反而放行。"""
        for kwargs in ({}, {"free_mib": 100}, {"total_mib": 100}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidRequest):
                    GpuFacts(observed=True, **kwargs)

    def test_a_numberless_observation_cannot_unlock_the_gpu_gate(self) -> None:
        from backend.evaluation.admission import evaluate_admission

        # 就算绕过构造器造出这样一份事实（这里用 object.__setattr__ 模拟），
        # 准入也**再**判一次，不会因为 free_mib 是 None 就跳过显存那一条。
        sneaky = GpuFacts(observed=False)
        object.__setattr__(sneaky, "observed", True)
        decision = evaluate_admission(
            AdmissionPolicy(dispatch_enabled=True, window=open_window(), min_free_mib=1),
            runner=good_runner(),
            gpu=sneaky,
            now=EPOCH,
        )
        self.assertFalse(decision.allowed)
        self.assertIn(REASON_GPU_FACTS_NOT_OBSERVED, decision.reasons)

    def test_observed_facts_are_rendered_with_their_state(self) -> None:
        payload = good_gpu().to_dict()
        self.assertEqual(payload["state"], "known")
        self.assertEqual(payload["free_mib"], 20_000)


class MockRunnerTests(unittest.TestCase):
    def test_the_mock_reports_itself_as_synthetic_only(self) -> None:
        capabilities = MockRunner().capabilities()
        self.assertTrue(capabilities.available)
        self.assertTrue(capabilities.synthetic_only)
        self.assertFalse(capabilities.wired is False and capabilities.available)

    def test_the_mock_is_never_a_real_runner(self) -> None:
        facts = runner_facts_from(MockRunner().capabilities())
        self.assertFalse(facts.real_runner_available)

    def test_the_mock_always_produces_a_synthetic_sample(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        _s, _h, body = h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice"
        )
        data = body["data"]
        self.assertTrue(data["synthetic"])
        self.assertTrue(data["result"]["synthetic"])
        self.assertEqual(data["executed_by"], "mock")

    def test_the_stored_result_row_is_marked_synthetic(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice")
        result = h.store.get_result(job["job_id"])
        assert result is not None
        self.assertTrue(result.synthetic)

    def test_the_mock_records_the_unknown_metrics_as_unknown(self) -> None:
        """合成样本不产生分位数：那是 unknown，不是 0 ms。"""
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        _s, _h, body = h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice"
        )
        metric = body["data"]["result"]["metrics"]["latency_p99_ms"]
        self.assertEqual(metric["state"], "unknown")
        self.assertIsNone(metric["value"])

    def test_a_sample_without_an_explicit_synthetic_flag_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            RunnerSample(metrics={})  # type: ignore[call-arg]

    def test_an_unsupported_metric_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            RunnerSample(synthetic=True, metrics={"invented": None})  # type: ignore[dict-item]

    def test_a_mock_deployment_cannot_report_a_real_runner_despite_injected_facts(self) -> None:
        """装了 mock 的部署，即使注入乐观事实，也拿不到真实派发。"""
        h = build_harness(runner=MockRunner())
        try:
            self.assertFalse(h.service.runner_can_run_real())
            h.service._runner_facts = good_runner()  # 模拟一次错误的接线/注入
            decision = h.service.admission_decision()
            self.assertFalse(decision.allowed)
            self.assertIn(REASON_REAL_RUNNER_NOT_AVAILABLE, decision.reasons)
        finally:
            h.close()

    def test_even_a_fully_open_policy_does_not_dispatch_with_a_mock(self) -> None:
        h = build_harness(
            runner=MockRunner(),
            admission=AdmissionPolicy(
                dispatch_enabled=True, window=TimeWindow.unrestricted_window(), min_free_mib=1
            ),
            gpu_facts=good_gpu(),
        )
        try:
            decision = h.service.admission_decision()
            self.assertFalse(decision.allowed)
            self.assertIn(REASON_REAL_RUNNER_NOT_AVAILABLE, decision.reasons)
        finally:
            h.close()


class AdapterStubTests(unittest.TestCase):
    def test_an_unwired_http_adapter_is_unavailable(self) -> None:
        capabilities = EvalScopeHttpAdapter().capabilities()
        self.assertFalse(capabilities.available)
        self.assertFalse(capabilities.wired)
        self.assertIn(ADAPTER_NOT_CONFIGURED, capabilities.reasons)
        self.assertIn(ADAPTER_NOT_WIRED, capabilities.reasons)
        self.assertIsNone(capabilities.detected_version)

    def test_an_unwired_cli_adapter_is_unavailable(self) -> None:
        capabilities = EvalScopeCliAdapter().capabilities()
        self.assertFalse(capabilities.available)
        self.assertIsNone(capabilities.detected_version)

    def test_both_stubs_refuse_to_run(self) -> None:
        for adapter in (EvalScopeHttpAdapter(), EvalScopeCliAdapter()):
            with self.subTest(kind=adapter.kind):
                with self.assertRaises(RunnerUnavailable):
                    adapter.run(object())

    def test_the_probe_paths_are_not_implemented(self) -> None:
        for adapter in (EvalScopeHttpAdapter(), EvalScopeCliAdapter()):
            with self.subTest(kind=adapter.kind):
                with self.assertRaises(RunnerUnavailable):
                    adapter.probe()

    def test_an_adapter_with_a_version_but_no_wiring_stays_unavailable(self) -> None:
        adapter = EvalScopeHttpAdapter(
            version="1.5.0", base_url="http://127.0.0.1:8090", wired=False
        )
        capabilities = adapter.capabilities()
        self.assertFalse(capabilities.available)
        self.assertEqual(capabilities.detected_version, "1.5.0")
        self.assertTrue(capabilities.version_supported)

    def test_a_fully_wired_adapter_reports_available(self) -> None:
        adapter = EvalScopeHttpAdapter(
            version="1.5.0", base_url="http://127.0.0.1:8090", wired=True,
            supported_eval_types=("correctness",),
        )
        capabilities = adapter.capabilities()
        self.assertTrue(capabilities.available)
        self.assertFalse(capabilities.synthetic_only)
        # 但 run() 仍然是桩：可用性不等于能力已经接线。
        with self.assertRaises(RunnerUnavailable):
            adapter.run(object())

    def test_an_older_version_is_reported_unsupported(self) -> None:
        adapter = EvalScopeHttpAdapter(
            version="0.9.0", base_url="http://127.0.0.1:8090", wired=True
        )
        capabilities = adapter.capabilities()
        self.assertFalse(capabilities.version_supported)
        self.assertFalse(capabilities.available)

    def test_the_description_declares_itself_a_stub(self) -> None:
        import json

        described = json.dumps(EvalScopeCliAdapter(version="1.5.0").describe(), sort_keys=True)
        self.assertIn("stub", described)
        self.assertIn("evalscope-adapter/1", described)

    def test_the_description_contains_no_command(self) -> None:
        """猜一条命令行是这里唯一能产生的、看起来像进展的错误动作。"""
        import json

        described = json.dumps(EvalScopeCliAdapter(version="1.5.0").describe(), sort_keys=True)
        for forbidden in ("eval ", "run ", "--dataset", "--model", "subprocess", "argv"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, described)


class BaseUrlTripwireTests(unittest.TestCase):
    def test_the_production_port_is_refused_outright(self) -> None:
        """绊线：带 9000 的地址一律拒绝，无论主机是什么。"""
        for url in (
            f"http://127.0.0.1:{EVALSCOPE_PRODUCTION_PORT}",
            f"http://127.0.0.1:{EVALSCOPE_PRODUCTION_PORT}/v1",
            f"http://localhost:{EVALSCOPE_PRODUCTION_PORT}/api",
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidRequest) as ctx:
                    parse_runner_base_url(url)
                self.assertIn("production_port_refused", str(ctx.exception))

    def test_a_non_loopback_host_is_refused(self) -> None:
        for url in ("http://10.0.0.5:8090", "http://evalscope.internal:8090",
                    "https://0.0.0.0:8090", "http://[2001:db8::1]:8090"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidRequest):
                    parse_runner_base_url(url)

    def test_credentials_in_the_url_are_refused(self) -> None:
        with self.assertRaises(InvalidRequest) as ctx:
            parse_runner_base_url("http://" + "user:secret@" + "127.0.0.1:8090")
        self.assertIn("userinfo_refused", str(ctx.exception))

    def test_a_non_http_scheme_is_refused(self) -> None:
        for url in ("ftp://127.0.0.1:8090", "file:///etc/passwd", "gopher://127.0.0.1:70",
                    "127.0.0.1:8090"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidRequest):
                    parse_runner_base_url(url)

    def test_a_loopback_url_on_another_port_is_accepted(self) -> None:
        for url in ("http://127.0.0.1:8090", "http://localhost:8090/v1",
                    "http://[::1]:8090"):
            with self.subTest(url=url):
                self.assertEqual(parse_runner_base_url(url), url)

    def test_the_redacted_form_does_not_echo_the_url(self) -> None:
        import json

        redacted = json.dumps(redact_base_url("http://127.0.0.1:8090/secret/path?a=1"), sort_keys=True)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("a=1", redacted)
        self.assertIn("loopback", redacted)

    def test_an_adapter_cannot_be_constructed_against_the_production_port(self) -> None:
        with self.assertRaises(InvalidRequest):
            EvalScopeHttpAdapter(
                base_url=f"http://127.0.0.1:{EVALSCOPE_PRODUCTION_PORT}", wired=True
            )


class VersionGateTests(unittest.TestCase):
    def test_a_plain_version_is_parsed(self) -> None:
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("10.0"), (10, 0, 0))

    def test_a_four_part_version_is_not_understood(self) -> None:
        self.assertIsNone(parse_version("1.2.3.4"))

    def test_garbage_is_not_understood(self) -> None:
        for value in ("latest", "1.2.3-rc1", "", None, 4, "v1.2.3", "1.2.x"):
            with self.subTest(value=value):
                self.assertIsNone(parse_version(value))

    def test_an_unknown_version_is_unknown_not_old(self) -> None:
        ok, version, reason = VersionGate().evaluate(None)
        self.assertFalse(ok)
        self.assertIsNone(version)
        self.assertEqual(reason, ADAPTER_VERSION_UNKNOWN)

    def test_a_below_floor_version_is_unsupported(self) -> None:
        ok, version, reason = VersionGate(minimum="1.0.0").evaluate("0.9.9")
        self.assertFalse(ok)
        self.assertEqual(version, "0.9.9")
        self.assertEqual(reason, "adapter_version_unsupported")

    def test_a_supported_version_passes(self) -> None:
        ok, version, reason = VersionGate(minimum="1.0.0").evaluate("2.1.0")
        self.assertTrue(ok)
        self.assertEqual(version, "2.1.0")
        self.assertIsNone(reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
