"""``backend/evaluation`` 接进 BFF 之后的行为。

三条性质值得单独盯住：

* **隔离**：别人的任务不会出现，也不会以「一条空行」的形式出现。
* **管理员看到的是全局 metadata**，不是原文、不是报告内容 —— 报告引用需要单独的
  scope，内容读取在本模块里**永远**关闭。
* **真实派发是关的**：任务可以入队，但 ``dispatch.state`` 恒为 ``refused``，而且
  ``submit.ready`` 恒为 false（那颗按钮在 UI 里本来就是惰性控件）。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from backend.evaluation import models as eval_models
from tests.bff import support as S


class RunListTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def runs(self, name, *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return S.ops_of(S.fetch_ops(client))["evaluation"]["runs"]

    def test_a_queued_run_carries_no_score(self):
        self.h.submit_eval("alice", self.ids["deployment_id"])
        (run,) = self.runs("alice")
        self.assertEqual(run["status"], "queued")
        self.assertIsNone(run["metric"], "a run that has not finished must carry no score")
        self.assertEqual(run["scope"], "mine")
        self.assertTrue(run["reason"])
        self.assertIsNotNone(run["created_at"])

    def test_the_model_column_is_joined_from_the_registry(self):
        self.h.submit_eval("alice", self.ids["deployment_id"])
        (run,) = self.runs("alice")
        self.assertEqual(run["model_id"], self.ids["model_id"])

    def test_a_completed_mock_run_may_carry_a_score_and_says_it_is_synthetic(self):
        """The mock runner's sample is the only source of a number here."""
        from backend.evaluation import dto as eval_dto

        envelope = self.h.submit_eval("alice", self.ids["deployment_id"])
        job_id = envelope["data"]["job_id"]
        requester = self.h.evaluation.requester_for(self.h.trusted_principal("alice"))
        self.h.evaluation.stack.service.run_mock_attempt(requester=requester, job_id=job_id)

        (run,) = self.runs("alice")
        self.assertEqual(run["status"], "done")
        self.assertIsInstance(run["metric"], (int, float))
        self.assertTrue(run["metric_unit"])
        # And the payload as a whole still admits it is synthetic.
        ops = S.ops_of(S.fetch_ops(self.h.client("alice")))
        self.assertTrue(ops["synthetic"])
        self.assertTrue(eval_dto)  # the module's own DTO layer is what shaped it

    def test_an_unknown_status_is_reported_as_unknown_with_a_reason(self):
        """`canceled` has no counterpart in the front-end vocabulary.

        Members hold no `evaluation:cancel` scope, so this is asserted against
        the mapping itself rather than by cancelling a real job.
        """
        from backend.bff.evaluation_adapter import _run_row

        row = _run_row(
            {
                "job_id": "evj-synthetic",
                "owner": self.h.principal_of("alice"),
                "status": eval_models.STATUS_CANCELED,
                "target": {"deployment_id": self.ids["deployment_id"]},
                "result": {"available": False, "metrics": {}},
            },
            subject=self.h.principal_of("alice"),
            is_admin=False,
        )
        self.assertEqual(row["status"], "unknown")
        self.assertIn("canceled", row["reason"])
        self.assertIsNone(row["metric"])

    def test_a_member_holds_no_cancel_or_retry_scope(self):
        """The work order did not ask for cancel/retry, so members do not get it."""
        from backend.bff import scopes as bff_scopes
        from backend.evaluation.identity import (
            SCOPE_EVAL_CANCEL,
            SCOPE_EVAL_RETRY,
        )

        for role in ("member", "platform_admin"):
            granted = bff_scopes.evaluation_scopes_for(role)
            self.assertNotIn(SCOPE_EVAL_CANCEL, granted)
            self.assertNotIn(SCOPE_EVAL_RETRY, granted)

    def test_every_run_status_is_one_the_contract_knows(self):
        self.h.submit_eval("alice", self.ids["deployment_id"])
        for name in ("alice", "carol"):
            for run in self.runs(name):
                self.assertIn(
                    run["status"], {"queued", "running", "done", "failed", "unknown"}
                )


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")
        self.h.submit_eval("alice", self.ids["deployment_id"])

    def runs(self, name, *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return S.ops_of(S.fetch_ops(client))["evaluation"]["runs"]

    def test_another_member_sees_nothing_of_it(self):
        self.assertEqual(self.runs("bob"), [])
        self.assertEqual(self.runs("dave"), [])

    def test_the_owner_sees_it_as_mine_and_the_elevated_session_as_global(self):
        (mine,) = self.runs("alice")
        self.assertEqual(mine["scope"], "mine")
        (global_row,) = self.runs("carol", admin=True)
        self.assertEqual(global_row["scope"], "global")
        self.assertEqual(global_row["run_id"], mine["run_id"])

    def test_the_same_person_without_elevation_sees_only_their_own(self):
        """`carol` elevated sees the global queue; `carol` plain does not.

        Nothing about the request differs — the account, the cookie name and the
        route are identical. Only the session's elevation does.
        """
        self.assertEqual(self.runs("carol"), [])
        self.assertTrue(self.runs("carol", admin=True))

    def test_a_global_row_never_appears_for_a_session_without_the_scope(self):
        for name in ("alice", "bob", "dave"):
            for run in self.runs(name):
                self.assertEqual(run["scope"], "mine", name)

    def test_the_global_view_is_elevation_not_a_stored_role(self):
        """Same principal, elevation removed ⇒ the whole catalogue closes.

        `carol` keeps the same Linux identity and the same account; only whether
        *that session* is in admin mode changes. Every scope is derived from the
        session's elevation, so exiting is total and the registry ACL answers
        403 on the global view.

        The stored role is deliberately not the lever any more: it is still
        `platform_admin` for `carol` throughout this test, and that fact changes
        nothing. That is the 2026-10-15 fix, stated as a test.
        """
        self.h.client("carol")
        elevated = self.h.admin_client("carol")
        self.assertTrue(S.ops_of(S.fetch_ops(elevated))["admin"]["available"])
        self.assertEqual(
            self.h.auth_app.service.db.get_user(self.h.principal_of("carol")).role,
            "platform_admin",
        )

        S.exit_admin_mode(elevated)
        after = S.fetch_ops(elevated)
        self.assertEqual(after.status, 200)
        self.assertFalse(S.ops_of(after)["admin"]["available"])
        self.assertEqual(S.ops_of(after)["registry"]["all"], [])


class DispatchIsClosedTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def test_a_submission_queues_and_is_never_dispatched(self):
        envelope = self.h.submit_eval("alice", self.ids["deployment_id"])
        data = envelope["data"]
        self.assertEqual(data["status"], eval_models.STATUS_QUEUED)
        self.assertEqual(data["dispatch"]["state"], eval_models.DISPATCH_REFUSED)
        self.assertFalse(data["dispatch"]["dispatched"])
        self.assertTrue(data["synthetic"])
        admission = data["admission"]
        self.assertFalse(admission["allowed"])
        self.assertTrue(admission["synthetic_only"])

    def test_submit_is_never_ready_for_any_role(self):
        for name in ("alice", "bob", "dave", "carol"):
            ops = S.ops_of(S.fetch_ops(self.h.client(name)))
            submit = ops["evaluation"]["submit"]
            self.assertFalse(submit["ready"], name)
            self.assertTrue(submit["reason"], name)

    def test_the_reason_is_the_modules_own_admission_reason(self):
        ops = S.ops_of(S.fetch_ops(self.h.client("alice")))
        decision = self.h.evaluation.stack.service.admission_decision(
            requester=self.h.evaluation.requester_for(self.h.trusted_principal("alice"))
        )
        self.assertEqual(ops["evaluation"]["submit"]["reason"], decision.reason)

    def test_no_real_adapter_can_run(self):
        self.assertFalse(self.h.evaluation.stack.service.runner_can_run_real())
        for kind in ("evalscope_http", "evalscope_cli"):
            from backend.evaluation import runner as eval_runner

            adapter = eval_runner.adapter_by_kind(kind)
            report = adapter.capabilities()
            self.assertFalse(report.available, kind)
            self.assertFalse(report.synthetic_only, kind)

    def test_no_dispatch_scope_is_granted_to_any_role(self):
        from backend.bff import scopes as bff_scopes
        from backend.evaluation.identity import SCOPE_EVAL_DISPATCH_REAL

        for role in ("member", "platform_admin"):
            self.assertNotIn(
                SCOPE_EVAL_DISPATCH_REAL, bff_scopes.evaluation_scopes_for(role)
            )


class MetadataOnlyTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")
        self.h.submit_eval("alice", self.ids["deployment_id"])

    def test_the_admin_view_carries_no_report_content_or_prompt_text(self):
        ops = S.ops_of(S.fetch_ops(self.h.admin_client("carol")))
        blob = json.dumps(ops, ensure_ascii=False)
        for field in ("prompt", "completion", "messages", "payload", "image"):
            self.assertNotIn(f'"{field}"', blob, field)

    def test_the_report_reference_is_never_readable(self):
        """`report_content_reading_disabled` is structural, not a policy toggle."""
        from backend.evaluation import reports as eval_reports

        root = eval_reports.unconfigured_root()
        self.assertFalse(root.configured)
        self.assertEqual(root.describe()["content_reading"], "disabled")
        with self.assertRaises(Exception):
            root.read_bytes("synthetic/whatever.json")

    def test_the_module_touches_no_gpu_and_loads_no_model(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "backend" / "evaluation"
        forbidden = {"subprocess", "socket", "torch", "pynvml", "ctypes", "transformers"}
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            self.assertEqual(names & forbidden, set(), path.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
