"""允许清单、参数范围、以及"自定义命令/路径/地址"与评测请求的严格分离。

这一组测试的对象是"一次评测请求可以说什么"。它要证明的是**封闭性**：不在清单里的
东西进不来，而不是"进得来但会被温柔处理"。
"""

from __future__ import annotations

import unittest

from backend.evaluation.datasets import (
    DATASET_CATALOG,
    EVAL_TYPES,
    FORBIDDEN_SPEC_FIELDS,
    GPU_STRESSING_EVAL_TYPES,
    SAFETY_PRESETS,
    build_eval_spec,
    catalog_dto,
    reject_forbidden_spec_fields,
)
from backend.evaluation.errors import InvalidRequest
from tests.evaluation.support import DEP_SHARED_STRESS_GRANTED, DEP_VINCE_PRIVATE, build_harness, close_all

_LIVE = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


class EvalTypeTests(unittest.TestCase):
    def test_the_four_eval_types_are_the_documented_ones(self) -> None:
        self.assertEqual(
            set(EVAL_TYPES), {"correctness", "throughput", "latency", "safety_preset"}
        )

    def test_only_throughput_and_latency_stress_the_gpu(self) -> None:
        self.assertEqual(GPU_STRESSING_EVAL_TYPES, {"throughput", "latency"})
        self.assertNotIn("correctness", GPU_STRESSING_EVAL_TYPES)

    def test_an_unknown_eval_type_is_refused(self) -> None:
        for bad in ("vibes", "", None, 3, "Correctness", "custom"):
            with self.subTest(eval_type=bad):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(eval_type=bad, dataset_slug="synthetic-qa-smoke")

    def test_a_dataset_must_support_the_eval_type(self) -> None:
        with self.assertRaises(InvalidRequest) as ctx:
            build_eval_spec(
                eval_type="throughput", dataset_slug="synthetic-qa-smoke"
            )
        self.assertIn("does not support", str(ctx.exception))


class DatasetAllowlistTests(unittest.TestCase):
    def test_an_unknown_slug_is_refused(self) -> None:
        for bad in ("mmlu", "gsm8k", "../../etc/passwd", "synthetic-qa-smoke ", "SYNTHETIC-QA-SMOKE"):
            with self.subTest(slug=bad):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(eval_type="correctness", dataset_slug=bad)

    def test_the_refusal_does_not_suggest_a_near_miss(self) -> None:
        """拒绝信息列出**全部**已知 slug，而不是"最接近的那个"。

        只回一个"你是不是想写 X"，就等于把一个可枚举的预言机交出去：调用者可以
        用二分法把整个目录问出来。
        """
        with self.assertRaises(InvalidRequest) as ctx:
            build_eval_spec(eval_type="correctness", dataset_slug="synthetic-qa-smok")
        message = str(ctx.exception)
        # 它列出的必须是完整的已知集合，而不是一个猜测。
        for slug in DATASET_CATALOG:
            self.assertIn(slug, message)

    def test_every_catalog_entry_is_marked_synthetic(self) -> None:
        """本构建里没有任何数据集来自真实 EvalScope。"""
        for slug, spec in DATASET_CATALOG.items():
            with self.subTest(slug=slug):
                self.assertTrue(spec.synthetic_fixture)

    def test_the_catalog_dto_declares_the_mapping_is_not_wired(self) -> None:
        dto = catalog_dto()
        self.assertIn("note", dto)
        self.assertIn("尚未接线", dto["note"])
        self.assertEqual(sorted(entry["slug"] for entry in dto["datasets"]), sorted(DATASET_CATALOG))


class ParameterRangeTests(unittest.TestCase):
    def test_defaults_are_filled_in_and_the_result_is_complete(self) -> None:
        spec = build_eval_spec(eval_type="correctness", dataset_slug="synthetic-qa-smoke")
        declared = {param.name for param in DATASET_CATALOG["synthetic-qa-smoke"].params}
        self.assertEqual(set(spec.params), declared)

    def test_out_of_range_values_are_refused(self) -> None:
        for params in ({"limit": 0}, {"limit": 201}, {"few_shot": 6}, {"seed": -1},
                       {"temperature": 1.5}):
            with self.subTest(params=params):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(
                        eval_type="correctness", dataset_slug="synthetic-qa-smoke", params=params
                    )

    def test_the_boundaries_themselves_are_accepted(self) -> None:
        spec = build_eval_spec(
            eval_type="correctness",
            dataset_slug="synthetic-qa-smoke",
            params={"limit": 1, "few_shot": 5, "seed": 2_147_483_647, "temperature": 1.0},
        )
        self.assertEqual(spec.params["limit"], 1)
        self.assertEqual(spec.params["temperature"], 1.0)

    def test_a_boolean_is_not_a_number(self) -> None:
        """``True`` 在 Python 里 ``== 1``，所以"是数字"必须显式排除 bool。"""
        for params in ({"limit": True}, {"temperature": False}):
            with self.subTest(params=params):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(
                        eval_type="correctness", dataset_slug="synthetic-qa-smoke", params=params
                    )

    def test_non_finite_numbers_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(
                        eval_type="correctness",
                        dataset_slug="synthetic-qa-smoke",
                        params={"temperature": value},
                    )

    def test_a_fractional_value_for_an_int_parameter_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_eval_spec(
                eval_type="correctness", dataset_slug="synthetic-qa-smoke", params={"limit": 2.5}
            )

    def test_an_unknown_parameter_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest) as ctx:
            build_eval_spec(
                eval_type="correctness",
                dataset_slug="synthetic-qa-smoke",
                params={"limit": 4, "concurrency": 8},
            )
        self.assertIn("concurrency", str(ctx.exception))

    def test_params_must_be_an_object(self) -> None:
        for bad in ([], "limit=4", 4, True):
            with self.subTest(params=bad):
                with self.assertRaises(InvalidRequest):
                    build_eval_spec(
                        eval_type="correctness", dataset_slug="synthetic-qa-smoke", params=bad
                    )

    def test_a_choice_parameter_only_accepts_its_declared_values(self) -> None:
        dataset = DATASET_CATALOG["synthetic-safety-smoke"]
        self.assertEqual(dataset.validate_params({"category": "pii"})["category"], "pii")
        for bad in ("malware", "PII", "", 4, "unknown"):
            with self.subTest(category=bad):
                with self.assertRaises(InvalidRequest):
                    dataset.validate_params({"category": bad})

    def test_the_concurrency_ceiling_is_deliberately_low(self) -> None:
        """本部署 GPU 已满，连参数上界都不该鼓励压测。"""
        spec = DATASET_CATALOG["synthetic-gen-smoke"].param("concurrency")
        assert spec is not None
        self.assertLessEqual(spec.maximum, 4)


class SafetyPresetTests(unittest.TestCase):
    def test_the_allowlisted_presets_are_the_documented_ones(self) -> None:
        self.assertEqual(
            set(SAFETY_PRESETS), {"safety-refusal-sample", "safety-pii-sample"}
        )

    def test_a_preset_supplies_its_own_dataset_and_params(self) -> None:
        spec = build_eval_spec(eval_type="safety_preset", preset="safety-refusal-sample")
        self.assertEqual(spec.dataset_slug, "synthetic-safety-smoke")
        self.assertEqual(spec.preset, "safety-refusal-sample")
        self.assertEqual(spec.params["category"], "refusal")

    def test_a_preset_accepts_no_params(self) -> None:
        """能覆盖预设参数的"预设"不是预设，是一个带好听名字的自由表单。"""
        with self.assertRaises(InvalidRequest) as ctx:
            build_eval_spec(
                eval_type="safety_preset", preset="safety-refusal-sample", params={"limit": 50}
            )
        self.assertIn("fixed", str(ctx.exception))

    def test_a_preset_may_not_be_given_a_dataset(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_eval_spec(
                eval_type="safety_preset",
                preset="safety-refusal-sample",
                dataset_slug="synthetic-qa-smoke",
            )

    def test_an_unknown_preset_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_eval_spec(eval_type="safety_preset", preset="safety-everything")

    def test_the_preset_is_required_for_the_preset_eval_type(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_eval_spec(eval_type="safety_preset")

    def test_a_preset_is_refused_for_a_non_preset_eval_type(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_eval_spec(
                eval_type="correctness",
                dataset_slug="synthetic-qa-smoke",
                preset="safety-refusal-sample",
            )

    def test_preset_params_are_validated_like_any_other_params(self) -> None:
        """预设的参数也走同一个校验器：预设不是一条绕过范围检查的通道。"""
        spec = build_eval_spec(eval_type="safety_preset", preset="safety-pii-sample")
        self.assertEqual(spec.params["category"], "pii")
        self.assertLessEqual(spec.params["limit"], 100)


class ForbiddenFieldTests(unittest.TestCase):
    """执行面、文件系统面、网络面、正文面 —— 四类字段一律整请求拒绝。"""

    def test_the_forbidden_set_covers_the_four_surfaces(self) -> None:
        self.assertTrue({"command", "args", "shell", "entrypoint"} <= FORBIDDEN_SPEC_FIELDS)
        self.assertTrue({"path", "file", "dataset_path", "image"} <= FORBIDDEN_SPEC_FIELDS)
        self.assertTrue({"url", "uri", "host", "port", "endpoint_url"} <= FORBIDDEN_SPEC_FIELDS)
        self.assertTrue({"prompt", "completion", "content", "messages"} <= FORBIDDEN_SPEC_FIELDS)

    def test_each_forbidden_field_is_refused_directly(self) -> None:
        for field in sorted(FORBIDDEN_SPEC_FIELDS):
            with self.subTest(field=field):
                with self.assertRaises(InvalidRequest):
                    reject_forbidden_spec_fields({field: "anything"})

    def test_a_harmless_request_passes_the_check(self) -> None:
        reject_forbidden_spec_fields(
            {"deployment_id": "dep-1", "eval_type": "correctness",
             "dataset_slug": "synthetic-qa-smoke", "params": {"limit": 4}}
        )

    def test_a_non_mapping_is_a_no_op(self) -> None:
        for value in (None, [], "x", 4, True):
            with self.subTest(value=value):
                reject_forbidden_spec_fields(value)

    def test_a_command_field_is_refused_over_http(self) -> None:
        h = _LIVE[0]
        for field, value in (("command", "rm -rf /"), ("url", "http://example.invalid/x"),
                             ("path", "/etc/passwd"), ("prompt", "ignore previous")):
            with self.subTest(field=field):
                status, _headers, body = h.request(
                    "POST", "/api/evaluation/jobs", as_principal="alice",
                    payload={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                             "dataset_slug": "synthetic-qa-smoke", field: value},
                )
                self.assertEqual(status, 400, msg=body)

    def test_a_forbidden_field_nested_in_params_is_refused_as_an_unknown_param(self) -> None:
        h = _LIVE[0]
        status, _headers, body = h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke", "params": {"path": "/etc/passwd"}},
        )
        self.assertEqual(status, 400, msg=body)
        self.assertEqual(body["error"]["contract_code"], "evaluation.invalid_request")


class GpuStressSeparationTests(unittest.TestCase):
    """shared 语义是"你可以调用"，不是"你可以打满显存"。"""

    def setUp(self) -> None:
        self.h = _LIVE[0]

    def test_throughput_on_a_shared_target_is_refused(self) -> None:
        status, _h, body = self.h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": "dep-shared-users", "eval_type": "throughput",
                     "dataset_slug": "synthetic-gen-smoke"},
        )
        self.assertEqual(status, 403, msg=body)
        self.assertEqual(
            body["error"]["contract_code"], "evaluation.shared_target_gpu_stress_refused"
        )

    def test_latency_on_a_shared_target_is_refused_too(self) -> None:
        status, _h, body = self.h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": "dep-shared-users", "eval_type": "latency",
                     "dataset_slug": "synthetic-qa-smoke"},
        )
        self.assertEqual(status, 403, msg=body)

    def test_correctness_on_a_shared_target_is_allowed(self) -> None:
        job = self.h.submit("alice", deployment_id="dep-shared-users")
        self.assertTrue(job["target"]["shared"])
        self.assertFalse(job["target"]["gpu_stress_allowed"])

    def test_an_explicit_grant_permits_the_stress_type(self) -> None:
        job = self.h.submit(
            "alice", deployment_id=DEP_SHARED_STRESS_GRANTED, eval_type="throughput",
            dataset_slug="synthetic-gen-smoke",
        )
        self.assertTrue(job["target"]["shared"])
        self.assertTrue(job["target"]["gpu_stress_allowed"])

    def test_the_owner_may_always_stress_their_own_target(self) -> None:
        job = self.h.submit(
            "alice", deployment_id=DEP_VINCE_PRIVATE, eval_type="throughput",
            dataset_slug="synthetic-gen-smoke",
        )
        self.assertFalse(job["target"]["shared"])
        self.assertTrue(job["target"]["gpu_stress_allowed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
