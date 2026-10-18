"""DTO 契约：JobStatus / ResultSummary / 监控面 / 模块门 / OpenAPI。

这一组的立场：**DTO 是契约，不是实现的副产品**。因此它检查的是形状与不变量，
而不是"某个字段此刻恰好等于几"：

* 每条任务状态都有中文标签，且相互一致（``terminal`` / ``cancellable`` / ``retryable``）；
* 结果为空时给 ``empty_state``，其 ``value`` 是 ``None``——**永远不是 0**；
* 任何 ``state != known`` 的块都不得携带数值（``dto.validate_dto`` 每次构建时自检）；
* 模块门（V6 的"EvalScope 入口授权"面板）对每一项能力都给出 ``state`` + ``reason``；
* OpenAPI 与路由表**双向**一致，且请求体 schema 与 service 的 allowlist 完全一致。
"""

from __future__ import annotations

import json
import unittest

from backend.evaluation import dto
from backend.evaluation.admission import AdmissionPolicy, GpuFacts, RunnerFacts
from backend.evaluation.datasets import EVAL_TYPES
from backend.evaluation.dto import (
    EMPTY_STATE_LABEL,
    FORBIDDEN_DTO_KEYS,
    MODULE_AVAILABLE,
    MODULE_DISABLED,
    MODULE_STATES,
    MODULE_UNAVAILABLE,
    REASON_DISPATCH_FORBIDDEN,
    REASON_MISSING_SCOPE,
    REASON_NO_SESSION,
    REASON_PREVIEW_NO_BACKEND,
    SCHEMA_VERSION,
    STATUS_LABELS,
    DtoError,
    empty_state_block,
    envelope,
    render_module_gate,
    validate_dto,
)
from backend.evaluation.models import JOB_STATUSES, STATUS_COMPLETED, STATUS_QUEUED
from backend.evaluation.openapi import (
    OPENAPI_VERSION,
    build_openapi_document,
    validate_openapi,
)
from backend.evaluation.service import REQUEST_FIELDS
from tests.evaluation.support import DEP_VINCE_PRIVATE, build_harness, close_all

_LIVE = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


def walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


class EnvelopeTests(unittest.TestCase):
    def test_a_synthetic_envelope_declares_itself(self) -> None:
        payload = envelope({"x": 1}, mode="synthetic", source="test")
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertTrue(any("SYNTHETIC" in hint for hint in payload["display_hints"]))

    def test_a_live_envelope_carries_no_synthetic_hint(self) -> None:
        payload = envelope({"x": 1}, mode="live", source="test")
        self.assertFalse(payload["synthetic"])
        self.assertEqual(payload["display_hints"], [])

    def test_the_synthetic_flag_has_no_default(self) -> None:
        with self.assertRaises(TypeError):
            envelope({"x": 1}, source="test")  # type: ignore[call-arg]

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(DtoError):
            envelope({"x": 1}, mode="pretend", source="test")

    def test_a_missing_source_is_refused(self) -> None:
        with self.assertRaises(DtoError):
            envelope({"x": 1}, mode="synthetic", source="")

    def test_every_live_response_validates(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice")
        for path in (
            "/api/evaluation/jobs",
            f"/api/evaluation/jobs/{job['job_id']}",
            f"/api/evaluation/jobs/{job['job_id']}/result",
            "/api/evaluation/datasets",
            "/api/evaluation/admission",
            "/api/evaluation/modules",
            "/api/evaluation/monitoring",
        ):
            with self.subTest(path=path):
                _s, _headers, body = h.request("GET", path, as_principal="alice")
                validate_dto(body)


class ValidateDtoTests(unittest.TestCase):
    def test_a_well_formed_dto_passes(self) -> None:
        validate_dto(envelope({"a": 1}, mode="synthetic", source="test"))

    def test_a_missing_top_level_field_is_refused(self) -> None:
        payload = envelope({"a": 1}, mode="synthetic", source="test")
        del payload["scope"]
        with self.assertRaises(DtoError):
            validate_dto(payload)

    def test_a_non_boolean_synthetic_is_refused(self) -> None:
        payload = envelope({"a": 1}, mode="synthetic", source="test")
        payload["synthetic"] = "yes"
        with self.assertRaises(DtoError):
            validate_dto(payload)

    def test_an_unmeasured_value_must_be_null(self) -> None:
        """这是"没测到"与"等于 0"之间唯一的程序化防线。"""
        for state in ("unknown", "not_configured", "unavailable"):
            with self.subTest(state=state):
                payload = envelope(
                    {"metric": {"state": state, "value": 0}}, mode="synthetic", source="test"
                )
                with self.assertRaises(DtoError):
                    validate_dto(payload)

    def test_a_measured_value_may_be_zero(self) -> None:
        """0 是一个合法的测量结果——被禁止的是用它表示缺失。"""
        payload = envelope(
            {"metric": {"state": "known", "value": 0}}, mode="synthetic", source="test"
        )
        validate_dto(payload)

    def test_a_content_like_key_is_refused_anywhere(self) -> None:
        for key in sorted(FORBIDDEN_DTO_KEYS):
            with self.subTest(key=key):
                payload = envelope(
                    {"nested": {"deeper": {key: "x"}}}, mode="synthetic", source="test"
                )
                with self.assertRaises(DtoError):
                    validate_dto(payload)


class InternalContractViolationTests(unittest.TestCase):
    """一份违反自身字段清单的 DTO 是**我们的**缺陷，不是调用方的错误。"""

    def test_a_dto_contract_violation_is_500_not_400(self) -> None:
        from unittest import mock

        from backend.evaluation.errors import InternalError
        from backend.evaluation.service import EvaluationService

        h = _LIVE[0]
        with mock.patch.object(
            EvaluationService, "datasets", side_effect=dto.DtoError("synthetic contract violation")
        ):
            status, _headers, body = h.request(
                "GET", "/api/evaluation/datasets", as_principal="alice"
            )
        self.assertEqual(status, 500, msg=body)
        self.assertEqual(
            body["error"]["contract_code"], InternalError.contract_code
        )
        # 内部细节不外发。
        self.assertIsNone(body["error"]["message"])

    def test_the_internal_error_has_no_exposed_message(self) -> None:
        from backend.evaluation.errors import InternalError

        self.assertFalse(InternalError.expose_message)
        self.assertNotEqual(
            InternalError.contract_code, dto.SCHEMA_VERSION
        )


class JobStatusShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]

    def test_every_status_has_a_label(self) -> None:
        self.assertEqual(set(STATUS_LABELS), set(JOB_STATUSES))
        for label in STATUS_LABELS.values():
            self.assertTrue(label)

    def test_a_freshly_queued_job_has_the_documented_shape(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        for key in (
            "job_id", "owner", "owner_uid", "project_id", "status", "status_label",
            "terminal", "active", "cancellable", "retryable", "synthetic", "runner_kind",
            "dispatch", "cancel_requested", "attempt", "retry_of", "target", "result",
            "created_at", "updated_at",
        ):
            with self.subTest(key=key):
                self.assertIn(key, job)

    def test_the_status_flags_are_mutually_consistent(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.assertEqual(job["status"], STATUS_QUEUED)
        self.assertTrue(job["active"])
        self.assertFalse(job["terminal"])
        self.assertTrue(job["cancellable"])
        self.assertFalse(job["retryable"])

    def test_a_completed_job_is_terminal_and_not_cancellable(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        _s, _h, body = self.h.request(
            "POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice"
        )
        data = body["data"]
        self.assertEqual(data["status"], STATUS_COMPLETED)
        self.assertTrue(data["terminal"])
        self.assertFalse(data["active"])
        self.assertFalse(data["cancellable"])
        self.assertFalse(data["retryable"])
        self.assertTrue(data["result"]["available"])

    def test_the_status_label_is_chinese_for_every_state(self) -> None:
        expected = {
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "canceled": "已取消",
        }
        self.assertEqual(STATUS_LABELS, expected)

    def test_the_job_id_has_the_documented_shape(self) -> None:
        job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.assertRegex(job["job_id"], r"^evj-[0-9a-f]{24}$")


class EmptyStateTests(unittest.TestCase):
    def test_an_empty_state_is_never_zero(self) -> None:
        block = empty_state_block(what="evaluation result")
        self.assertIsNone(block["value"])
        self.assertEqual(block["state"], "unknown")
        self.assertEqual(block["label"], EMPTY_STATE_LABEL)
        self.assertIn("不是", block["note"])

    def test_a_job_without_a_result_carries_an_empty_state(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        self.assertFalse(job["result"]["available"])
        self.assertIsNone(job["result"]["empty_state"]["value"])
        self.assertEqual(job["result"]["metrics"], {})
        self.assertIsNone(job["result"]["synthetic"])

    def test_the_result_endpoint_gives_an_empty_state_before_a_result_exists(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        _s, _h, body = h.request(
            "GET", f"/api/evaluation/jobs/{job['job_id']}/result", as_principal="alice"
        )
        result = body["data"]["result"]
        self.assertFalse(result["available"])
        self.assertIsNone(result["empty_state"]["value"])

    def test_an_empty_job_list_carries_an_empty_state(self) -> None:
        h = build_harness()
        try:
            _s, _h, body = h.request("GET", "/api/evaluation/jobs", as_principal="dave")
            self.assertEqual(body["data"]["jobs"], [])
            self.assertIsNone(body["data"]["empty_state"]["value"])
        finally:
            h.close()


class ResultSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _LIVE[0]
        self.job = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, eval_type="latency",
                                 dataset_slug="synthetic-qa-smoke")
        self.h.request(
            "POST", f"/api/evaluation/jobs/{self.job['job_id']}/mock-run", as_principal="alice"
        )

    def result(self) -> dict:
        _s, _h, body = self.h.request(
            "GET", f"/api/evaluation/jobs/{self.job['job_id']}/result", as_principal="alice"
        )
        return body["data"]["result"]

    def test_the_summary_carries_reproducibility_metadata(self) -> None:
        reproducibility = self.result()["reproducibility"]
        for key in ("eval_type", "dataset_slug", "dataset_version", "params_hash",
                    "target_deployment_id", "runner_kind", "attempt"):
            with self.subTest(key=key):
                self.assertIn(key, reproducibility)

    def test_the_params_hash_is_stable_for_the_same_request(self) -> None:
        again = self.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, eval_type="latency",
                              dataset_slug="synthetic-qa-smoke")
        _s, _h, body = self.h.request(
            "GET", f"/api/evaluation/jobs/{again['job_id']}/result", as_principal="alice"
        )
        # 两次提交参数相同，但第二次还没有结果；用两次同样的结果来比对摘要。
        self.h.request(
            "POST", f"/api/evaluation/jobs/{again['job_id']}/mock-run", as_principal="alice"
        )
        _s2, _h2, body2 = self.h.request(
            "GET", f"/api/evaluation/jobs/{again['job_id']}/result", as_principal="alice"
        )
        self.assertEqual(
            body2["data"]["result"]["reproducibility"]["params_hash"],
            self.result()["reproducibility"]["params_hash"],
        )

    def test_every_metric_declares_a_state(self) -> None:
        for name, metric in self.result()["metrics"].items():
            with self.subTest(metric=name):
                self.assertIn(metric["state"], ("known", "unknown", "not_configured"))
                if metric["state"] != "known":
                    self.assertIsNone(metric["value"])

    def test_the_latency_quantiles_are_measured_for_a_latency_eval(self) -> None:
        metrics = self.result()["metrics"]
        self.assertEqual(metrics["latency_p50_ms"]["state"], "known")
        self.assertIsNotNone(metrics["latency_p50_ms"]["value"])

    def test_the_result_is_marked_synthetic(self) -> None:
        self.assertTrue(self.result()["synthetic"])

    def test_the_report_reference_is_present_for_the_owner(self) -> None:
        report = self.result()["report"]
        self.assertIsNotNone(report["ref"])
        self.assertFalse(report["content_read"])


class MonitoringTests(unittest.TestCase):
    def test_every_job_status_is_counted_including_zeros(self) -> None:
        h = build_harness()
        try:
            _s, _h, body = h.request(
                "GET", "/api/evaluation/monitoring", as_principal="alice"
            )
            counts = body["data"]["jobs_by_status"]
            self.assertEqual(set(counts), set(JOB_STATUSES))
            # 这些 0 是**数过**的 0，与"没测到"不同，因此它们是合法的数字。
            self.assertTrue(all(isinstance(value, int) for value in counts.values()))
        finally:
            h.close()

    def test_the_gpu_free_memory_is_null_not_zero_when_unobserved(self) -> None:
        h = build_harness()
        try:
            _s, _h, body = h.request(
                "GET", "/api/evaluation/monitoring", as_principal="alice"
            )
            quality = body["data"]["quality"]
            self.assertIsNone(quality["gpu_free_mib"])
            self.assertEqual(body["data"]["gpu"]["state"], "not_configured")
        finally:
            h.close()

    def test_the_quality_block_distinguishes_synthetic_from_real(self) -> None:
        h = build_harness()
        try:
            _s, _h, body = h.request(
                "GET", "/api/evaluation/monitoring", as_principal="alice"
            )
            self.assertEqual(body["data"]["quality"]["measured_over"], "synthetic_only")
        finally:
            h.close()

    def test_the_monitoring_view_is_scoped_to_the_caller(self) -> None:
        h = build_harness()
        try:
            h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
            h.submit("dave", deployment_id="dep-dave-private")
            _s, _h, alice = h.request(
                "GET", "/api/evaluation/monitoring", as_principal="alice"
            )
            self.assertEqual(alice["data"]["jobs_total"], 1)
            _s2, _h2, admin = h.request(
                "GET", "/api/evaluation/monitoring", as_principal="carol"
            )
            self.assertEqual(admin["data"]["jobs_total"], 2)
        finally:
            h.close()


class ModuleGateTests(unittest.TestCase):
    """V6 的「EvalScope 入口授权」面板：每一项能力都要说明为什么。"""

    def _decision(self):
        return _LIVE[0].service.admission_decision()

    def test_without_a_session_nothing_is_claimed(self) -> None:
        gate = render_module_gate(requester=None, admission=self._decision())
        self.assertEqual(gate["state"], "unknown")
        self.assertEqual(gate["reason"], REASON_NO_SESSION)
        self.assertEqual(gate["capabilities"], [])

    def test_every_capability_carries_a_state_and_a_reason(self) -> None:
        gate = render_module_gate(
            requester=_LIVE[0].requester("alice"), admission=self._decision()
        )
        self.assertTrue(gate["capabilities"])
        for capability in gate["capabilities"]:
            with self.subTest(capability=capability["name"]):
                self.assertIn(capability["state"], MODULE_STATES)
                self.assertIn("reason", capability)
                self.assertTrue(capability["note"])
                if capability["state"] != MODULE_AVAILABLE:
                    self.assertIsNotNone(capability["reason"])

    def test_the_documented_capabilities_are_present(self) -> None:
        gate = render_module_gate(
            requester=_LIVE[0].requester("alice"), admission=self._decision()
        )
        names = {capability["name"] for capability in gate["capabilities"]}
        self.assertEqual(
            names,
            {
                "evaluation.jobs.read",
                "evaluation.jobs.submit",
                "evaluation.dispatch.real",
                "evalscope.native_ui",
                "evaluation.report.reference",
            },
        )

    def test_the_native_ui_is_disabled_with_a_reason_not_a_dead_button(self) -> None:
        gate = render_module_gate(
            requester=_LIVE[0].requester("alice"), admission=self._decision()
        )
        native = next(c for c in gate["capabilities"] if c["name"] == "evalscope.native_ui")
        self.assertEqual(native["state"], MODULE_DISABLED)
        self.assertEqual(native["reason"], REASON_PREVIEW_NO_BACKEND)

    def test_real_dispatch_is_unavailable_with_its_reasons_listed(self) -> None:
        gate = render_module_gate(
            requester=_LIVE[0].requester("alice"), admission=self._decision()
        )
        dispatch = next(
            c for c in gate["capabilities"] if c["name"] == "evaluation.dispatch.real"
        )
        self.assertEqual(dispatch["state"], MODULE_UNAVAILABLE)
        self.assertEqual(dispatch["reason"], REASON_DISPATCH_FORBIDDEN)
        self.assertTrue(dispatch["reasons"])

    def test_a_subject_without_the_read_scope_sees_why(self) -> None:
        from backend.evaluation.identity import SCOPE_EVAL_SUBMIT, RequesterContext

        partial = RequesterContext(
            principal="alice", uid=4001, linux_user="alice",
            scopes=frozenset({SCOPE_EVAL_SUBMIT}), roles=frozenset({"member"}),
        )
        _s, _h, body = _LIVE[0].request(
            "GET", "/api/evaluation/modules", as_principal="alice"
        )
        self.assertEqual(body["data"]["state"], MODULE_AVAILABLE)

        # 直接问渲染层：缺 read 时整体不可用，且原因是 missing_required_scope。
        gate = render_module_gate(
            requester=partial, admission=self._decision(), native_entry_available=False
        )
        read = next(c for c in gate["capabilities"] if c["name"] == "evaluation.jobs.read")
        self.assertEqual(read["state"], MODULE_UNAVAILABLE)
        self.assertEqual(read["reason"], REASON_MISSING_SCOPE)

    def test_a_fully_open_deployment_reports_available_capabilities(self) -> None:
        from backend.evaluation.limits import TimeWindow

        decision = _LIVE[0].service.admission_decision()
        _policy = AdmissionPolicy(
            dispatch_enabled=True, window=TimeWindow.unrestricted_window(), min_free_mib=1
        )
        from backend.evaluation.admission import evaluate_admission

        opened = evaluate_admission(
            _policy,
            runner=RunnerFacts(
                real_runner_available=True, adapter_kind="evalscope_http",
                adapter_version="1.2.0",
            ),
            gpu=GpuFacts(observed=True, free_mib=40_000, total_mib=49_140, source="probe"),
            now=_LIVE[0].clock(),
            dispatch_scope_granted=True,
        )
        gate = render_module_gate(
            requester=_LIVE[0].requester("alice"), admission=opened, native_entry_available=True
        )
        states = {c["name"]: c["state"] for c in gate["capabilities"]}
        self.assertEqual(states["evaluation.dispatch.real"], MODULE_AVAILABLE)
        self.assertEqual(states["evalscope.native_ui"], MODULE_AVAILABLE)
        self.assertEqual(gate["blocking_count"], 1)  # 只剩未接线的报告根


class OpenApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = build_openapi_document()

    def test_the_document_matches_the_route_table_both_ways(self) -> None:
        validate_openapi(self.document)

    def test_the_version_and_title_are_declared(self) -> None:
        self.assertEqual(self.document["openapi"], OPENAPI_VERSION)
        self.assertIn("EvalScope", self.document["info"]["title"])
        self.assertIn(SCHEMA_VERSION, self.document["info"]["description"])

    def test_every_route_is_documented(self) -> None:
        from backend.evaluation.api import ROUTES

        documented = {
            (path, method)
            for path, methods in self.document["paths"].items()
            for method in methods
        }
        self.assertEqual(documented, {(r.template, r.method.lower()) for r in ROUTES})

    def test_the_submit_schema_matches_the_service_allowlist(self) -> None:
        schema = self.document["components"]["schemas"]["EvaluationSubmitRequest"]
        self.assertEqual(set(schema["properties"]), set(REQUEST_FIELDS))
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(schema["description"])

    def test_the_eval_type_enum_matches_the_contract(self) -> None:
        schema = self.document["components"]["schemas"]["EvaluationSubmitRequest"]
        self.assertEqual(set(schema["properties"]["eval_type"]["enum"]), set(EVAL_TYPES))

    def test_the_job_status_enum_matches_the_state_machine(self) -> None:
        schema = self.document["components"]["schemas"]["JobStatus"]
        self.assertEqual(set(schema["properties"]["status"]["enum"]), set(JOB_STATUSES))

    def test_the_empty_state_value_is_constrained_to_null(self) -> None:
        schema = self.document["components"]["schemas"]["EmptyState"]
        self.assertEqual(schema["properties"]["value"]["type"], "null")

    def test_an_unmeasured_metric_is_documented_as_nullable(self) -> None:
        schema = self.document["components"]["schemas"]["MetricSample"]
        self.assertIn("null", schema["properties"]["value"]["oneOf"][1]["type"])

    def test_content_read_is_documented_as_always_false(self) -> None:
        schema = self.document["components"]["schemas"]["ReportReference"]
        self.assertFalse(schema["properties"]["content_read"]["const"])

    def test_the_dev_only_route_is_marked(self) -> None:
        mock = self.document["paths"]["/api/evaluation/jobs/{job_id}/mock-run"]["post"]
        self.assertTrue(mock["x-dev-only"])

    def test_the_document_is_json_serialisable(self) -> None:
        encoded = json.dumps(self.document, sort_keys=True)
        self.assertIn("bearerAuth", encoded)

    def test_the_served_document_validates_too(self) -> None:
        _s, _h, body = _LIVE[0].request("GET", "/api/evaluation/openapi.json")
        validate_openapi(body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
