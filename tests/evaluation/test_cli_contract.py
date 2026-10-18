"""CLI 数据契约：命令集、退出码、参数的保守解析、以及"CLI 不是授权旁路"。

最后一条是这一组里最重要的：CLI 与 HTTP **共用同一个 service**，因此同一个主体在
两条入口上看到的可见范围必须完全一致。一个"命令行里能看到的、网页上看不到的"任务，
说明授权被绕过了。
"""

from __future__ import annotations

import io
import json
import unittest

from backend.evaluation.cli import (
    COMMANDS,
    EXIT_CONTRACT,
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    TOKEN_ENV,
    describe_contract,
    main,
    parse_params,
)
from tests.evaluation.support import (
    DEP_VINCE_PRIVATE,
    PROJECT_ALPHA,
    Harness,
    build_harness,
    close_all,
)

_LIVE: list[Harness] = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


def run(argv, *, token: str | None = None, environ: dict | None = None):
    """跑一次 CLI，返回 ``(rc, stdout_payload, stderr_text)``。"""
    h = _LIVE[0]
    out, err = io.StringIO(), io.StringIO()
    env = environ if environ is not None else (
        {TOKEN_ENV: token} if token is not None else {}
    )
    rc = main(list(argv), service=h.service, out=out, err=err, environ=env)
    text = out.getvalue()
    payload = json.loads(text) if text.strip() else None
    return rc, payload, err.getvalue()


class ContractShapeTests(unittest.TestCase):
    def test_the_contract_declares_itself(self) -> None:
        described = describe_contract()
        self.assertEqual(described["entry_point"], "python -m backend.evaluation")
        self.assertEqual(described["token_env"], TOKEN_ENV)
        self.assertEqual(
            described["exit_codes"],
            {"0": "success", "1": "contract rejection", "2": "usage error",
             "3": "transport or environment error"},
        )

    def test_the_exit_codes_are_the_documented_integers(self) -> None:
        self.assertEqual((EXIT_OK, EXIT_CONTRACT, EXIT_USAGE, EXIT_TRANSPORT), (0, 1, 2, 3))

    def test_every_command_is_documented(self) -> None:
        names = {command.name for command in COMMANDS}
        self.assertEqual(
            names,
            {"health", "datasets", "admission", "jobs", "show", "result",
             "submit", "cancel", "retry", "mock-run"},
        )
        for command in COMMANDS:
            with self.subTest(command=command.name):
                self.assertTrue(command.summary)
                self.assertTrue(command.usage)

    def test_the_contract_notes_state_the_discipline(self) -> None:
        joined = " ".join(describe_contract()["notes"])
        self.assertIn("同一个 EvaluationService", joined)
        self.assertIn("永不回显", joined)
        self.assertIn("--allow-mock", joined)

    def test_only_mock_run_needs_the_flag(self) -> None:
        flagged = {c.name for c in COMMANDS if c.requires_mock_flag}
        self.assertEqual(flagged, {"mock-run"})

    def test_the_submit_fields_match_the_service_allowlist(self) -> None:
        from backend.evaluation.service import REQUEST_FIELDS

        self.assertEqual(set(describe_contract()["submit_fields"]), set(REQUEST_FIELDS))


class ParameterParsingTests(unittest.TestCase):
    def test_integers_floats_booleans_and_strings(self) -> None:
        self.assertEqual(
            parse_params(["limit=4", "temperature=0.5", "flag=true", "category=pii"]),
            {"limit": 4, "temperature": 0.5, "flag": True, "category": "pii"},
        )

    def test_false_is_a_boolean(self) -> None:
        self.assertEqual(parse_params(["flag=false"]), {"flag": False})

    def test_a_bracketed_value_stays_a_string(self) -> None:
        """刻意不解析 JSON：``limit=[1,2]`` 应当被数据集校验拒绝，而不是悄悄变成列表。"""
        self.assertEqual(parse_params(["limit=[1,2]"]), {"limit": "[1,2]"})

    def test_a_missing_equals_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_params(["limit"])

    def test_an_empty_key_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_params(["=4"])

    def test_a_duplicated_key_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_params(["limit=1", "limit=2"])

    def test_no_params_is_an_empty_dict(self) -> None:
        self.assertEqual(parse_params([]), {})


class ExitCodeTests(unittest.TestCase):
    def test_a_successful_command_exits_zero_with_an_envelope(self) -> None:
        rc, payload, _err = run(["datasets"], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(payload["schema_version"], "evaluation/1")
        self.assertTrue(payload["synthetic"])

    def test_health_carries_no_subject_information(self) -> None:
        h = _LIVE[0]
        out, err = io.StringIO(), io.StringIO()
        rc = main(["health"], service=h.service, out=out, err=err, environ={})
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["scope"]["principal"], None)

    def test_a_missing_credential_is_a_transport_error(self) -> None:
        rc, payload, err = run(["jobs"], environ={})
        self.assertEqual(rc, EXIT_TRANSPORT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.unauthenticated")
        self.assertIn(TOKEN_ENV, err)

    def test_an_unknown_credential_is_a_transport_error(self) -> None:
        rc, payload, _err = run(["jobs"], token="synthetic-nobody")
        self.assertEqual(rc, EXIT_TRANSPORT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.unauthenticated")

    def test_a_contract_rejection_exits_one(self) -> None:
        rc, payload, _err = run(
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "not-on-the-allowlist"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.invalid_request")

    def test_a_forbidden_field_via_the_cli_is_refused_too(self) -> None:
        rc, payload, _err = run(
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "synthetic-qa-smoke", "--param", "path=/etc/passwd"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.invalid_request")

    def test_an_out_of_range_parameter_is_refused_via_the_cli(self) -> None:
        rc, _payload, _err = run(
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "synthetic-qa-smoke", "--param", "limit=99999"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_CONTRACT)

    def test_a_usage_error_exits_two(self) -> None:
        import contextlib
        import sys as _sys

        h = _LIVE[0]
        out, err = io.StringIO(), io.StringIO()
        # argparse 把用法错误写进进程的 stderr（而不是我们注入的那条流），
        # 那是**给命令行用户看的**正确行为，但在测试输出里是噪声，因此这里就地静音。
        with contextlib.redirect_stderr(io.StringIO()):
            rc = main(
                ["submit"],  # 缺必填参数
                service=h.service, out=out, err=err, environ={TOKEN_ENV: "synthetic-alice"},
            )
        self.assertEqual(rc, EXIT_USAGE)
        self.assertIsNotNone(_sys.stderr)

    def test_mock_run_requires_the_explicit_flag(self) -> None:
        job = _LIVE[0].submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        rc, payload, _err = run(["mock-run", job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertIn("--allow-mock", payload["error"]["message"])

    def test_mock_run_with_the_flag_produces_a_synthetic_result(self) -> None:
        job = _LIVE[0].submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        rc, payload, _err = run(
            ["mock-run", job["job_id"], "--allow-mock"], token="synthetic-alice"
        )
        self.assertEqual(rc, EXIT_OK)
        self.assertTrue(payload["data"]["synthetic"])
        self.assertEqual(payload["data"]["executed_by"], "mock")


class CliFlowTests(unittest.TestCase):
    def test_submit_then_show_then_result(self) -> None:
        rc, payload, _err = run(
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "latency",
             "--dataset", "synthetic-qa-smoke", "--param", "limit=3",
             "--idempotency", "cli-flow"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_OK)
        job_id = payload["data"]["job_id"]
        self.assertEqual(payload["data"]["project_id"], PROJECT_ALPHA)

        rc, shown, _err = run(["show", job_id], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(shown["data"]["job_id"], job_id)

        rc, emptied, _err = run(["result", job_id], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertFalse(emptied["data"]["result"]["available"])
        self.assertIsNone(emptied["data"]["result"]["empty_state"]["value"])

        rc, _payload, _err = run(
            ["mock-run", job_id, "--allow-mock"], token="synthetic-alice"
        )
        self.assertEqual(rc, EXIT_OK)

        rc, filled, _err = run(["result", job_id], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertTrue(filled["data"]["result"]["available"])
        self.assertIn("params_hash", filled["data"]["result"]["reproducibility"])

    def test_cancel_and_retry_are_idempotent_via_the_cli(self) -> None:
        job = _LIVE[0].submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        rc, first, _err = run(["cancel", job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        rc, second, _err = run(["cancel", job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertTrue(second["data"]["already_canceled"])

        rc, retried, _err = run(["retry", job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        rc, again, _err = run(["retry", job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(retried["data"]["job_id"], again["data"]["job_id"])
        self.assertTrue(again["data"]["replayed"])

    def test_the_jobs_listing_is_json_and_sorted(self) -> None:
        h = _LIVE[0]
        out, err = io.StringIO(), io.StringIO()
        rc = main(
            ["jobs", "--limit", "5"], service=h.service, out=out, err=err,
            environ={TOKEN_ENV: "synthetic-alice"},
        )
        self.assertEqual(rc, EXIT_OK)
        text = out.getvalue()
        self.assertEqual(text, json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def test_a_dataset_scoped_command_reports_the_allowlist(self) -> None:
        rc, payload, _err = run(["datasets"], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(payload["data"]["datasets"]), 3)
        self.assertEqual(len(payload["data"]["safety_presets"]), 2)

    def test_admission_reports_why_dispatch_is_forbidden(self) -> None:
        rc, payload, _err = run(["admission"], token="synthetic-alice")
        self.assertEqual(rc, EXIT_OK)
        self.assertFalse(payload["data"]["can_dispatch"])
        self.assertTrue(payload["data"]["reasons"])


class CliIsNotAnAuthzBypassTests(unittest.TestCase):
    """同一个主体在 CLI 与 HTTP 上看到的可见范围必须一致。"""

    def setUp(self) -> None:
        self.h = _LIVE[0]
        self.dave_job = self.h.submit("dave", deployment_id="dep-dave-private")

    def test_the_cli_cannot_see_another_users_job(self) -> None:
        rc, payload, _err = run(["show", self.dave_job["job_id"]], token="synthetic-alice")
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.not_found")

    def test_the_cli_cannot_cancel_another_users_job(self) -> None:
        rc, payload, _err = run(
            ["cancel", self.dave_job["job_id"]], token="synthetic-alice"
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.not_found")

    def test_the_cli_cannot_submit_against_another_users_private_target(self) -> None:
        rc, payload, _err = run(
            ["submit", "--deployment", "dep-dave-private", "--type", "correctness",
             "--dataset", "synthetic-qa-smoke"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.not_found")

    def test_an_admin_sees_everything_through_the_cli(self) -> None:
        rc, payload, _err = run(["show", self.dave_job["job_id"]], token="synthetic-carol")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(payload["data"]["owner"], "dave")

    def test_a_read_only_admin_cannot_cancel_through_the_cli(self) -> None:
        rc, payload, _err = run(
            ["cancel", self.dave_job["job_id"]], token="synthetic-carol-readonly"
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertEqual(payload["error"]["contract_code"], "evaluation.access_denied")


class SecretHandlingTests(unittest.TestCase):
    def test_the_credential_is_never_echoed(self) -> None:
        h = _LIVE[0]
        token = h.token_for("alice")
        for argv in (
            ["datasets"],
            ["jobs"],
            ["admission"],
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "synthetic-qa-smoke"],
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "bogus"],
        ):
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                main(list(argv), service=h.service, out=out, err=err, environ={TOKEN_ENV: token})
                self.assertNotIn(token, out.getvalue())
                self.assertNotIn(token, err.getvalue())

    def test_a_parameter_value_is_not_echoed_in_the_error(self) -> None:
        """参数里可能有用户自己认定敏感的东西；错误信息只回键名。"""
        rc, payload, _err = run(
            ["submit", "--deployment", DEP_VINCE_PRIVATE, "--type", "correctness",
             "--dataset", "synthetic-qa-smoke", "--param", "sensitive-marker=supersecret"],
            token="synthetic-alice",
        )
        self.assertEqual(rc, EXIT_CONTRACT)
        self.assertNotIn("supersecret", json.dumps(payload, sort_keys=True))

    def test_an_internal_storage_error_is_not_exposed(self) -> None:
        """存储层的话术里有库路径与 schema 细节，不外发。"""
        from backend.evaluation.errors import StorageError
        from backend.evaluation.cli import _emit_error

        out, err = io.StringIO(), io.StringIO()
        _emit_error(out, err, StorageError("/var/lib/secret/eval.sqlite is corrupt"))
        payload = json.loads(out.getvalue())
        self.assertIsNone(payload["error"]["message"])
        self.assertNotIn("/var/lib/secret", json.dumps(payload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
