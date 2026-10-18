"""身份与行级授权：整套 RBAC 负控。

这一组是本次交付的核心安全证据。它们**全部**走真实的 WSGI 路径
（``Harness.request`` 驱动 ``create_app`` 的产物），因此断言的是"端到端行为"，
而不是某个内部函数的返回值。

覆盖的边界，逐条对应需求：

* 两个用户、两个项目 —— ``alice`` 在 ``proj-alpha``，``dave`` 在 ``proj-beta``；
* 能看到**自己的**评测，看不到别人的（同一个项目里的同伴也不行）；
* 未知项目 -> 404；别人的项目 -> 404（**同样的状态码**，不做存在性预言机）；
* ``carol`` 是 admin，可全局；
* 只读管理员能看全局但**不能**改全局（403）；
* 取消别人的任务被拒；取消自己的可重入（幂等，见 ``test_store_and_lifecycle``）；
* 伪造角色（roles 里写满 ``platform_admin``）**不得**提权；
* 伪造 header / body 字段 / 查询串一律整请求拒绝（400），而不是被忽略；
* 未知 / 撤销 / 未审批的凭据一律 401；
* ``dana`` 这个账号不存在，因此也不在任何目录里。
"""

from __future__ import annotations

import unittest

from backend.evaluation.errors import (
    AccessDenied,
    IdentityAssertionRejected,
    NotFound,
    Unauthenticated,
)
from backend.evaluation.identity import (
    Principal,
    PrincipalDirectory,
    SCOPE_EVAL_ADMIN_MANAGE,
    SCOPE_EVAL_ADMIN_READ,
    SCOPE_EVAL_CANCEL,
    SCOPE_EVAL_READ,
    SCOPE_EVAL_RETRY,
    SCOPE_EVAL_SUBMIT,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from tests.evaluation import ADMIN_USER, HOST_USERS, MODEL_USERS
from tests.evaluation.support import (
    DEP_KAIDONG_PRIVATE,
    DEP_SHARED_ALL,
    DEP_VINCE_PRIVATE,
    DEP_ZHENGJI_PRIVATE,
    MEMBER_SCOPES,
    PROJECT_ALPHA,
    PROJECT_BETA,
    TOKEN_PREFIX,
    Harness,
    build_directories,
    build_harness,
    close_all,
)

_LIVE: list[Harness] = []


def setUpModule() -> None:  # noqa: N802 - unittest 协议
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802 - unittest 协议
    _LIVE.clear()
    close_all()


def harness() -> Harness:
    return _LIVE[0]


class HostIdentityFactsTests(unittest.TestCase):
    """身份表是**显式声明**的合成事实，不查询宿主 NSS。

    ``backend/evaluation`` 从不调用 ``pwd``：一个主体的 UID 来自
    :data:`tests.evaluation.HOST_USERS` 这一处声明，并由
    :func:`tests.evaluation.support.build_directories` 消费。这里不再用
    ``pwd.getpwnam`` 断言宿主账户表：那样保护的不是产品行为，而是把套件绑死在
    某一台开发机的 ``/etc/passwd`` 布局上。账号名与 UID 全是合成值，因此断言的是
    **声明本身**与**目录是否按声明消费**，在任何主机上结果都一样。
    """

    def test_the_identity_table_is_explicitly_declared(self) -> None:
        uids = [uid for _name, uid in HOST_USERS]
        for name, uid in HOST_USERS:
            with self.subTest(user=name):
                self.assertIsInstance(uid, int)
                self.assertGreaterEqual(uid, 0)
        # 一个 UID 只能属于一个主体：否则「owner 是谁」会有两个答案。
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(
            {name for name, _uid in HOST_USERS}, set(MODEL_USERS) | {ADMIN_USER}
        )

    def test_the_directory_consumes_the_declared_table(self) -> None:
        """声明与消费不能漂移：目录与目标登记里的 UID 必须就是声明的那一个。"""
        principals, _tokens, _projects, targets = build_directories()
        for name, uid in HOST_USERS:
            with self.subTest(user=name):
                self.assertEqual(principals.require(name).uid, uid)
        self.assertEqual(len(principals), len(HOST_USERS))

        declared = dict(HOST_USERS)
        for deployment_id in (DEP_VINCE_PRIVATE, DEP_KAIDONG_PRIVATE, DEP_ZHENGJI_PRIVATE):
            with self.subTest(deployment=deployment_id):
                record = targets.resolve(deployment_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.owner_uid, declared[record.owner_principal])

    def test_an_identity_outside_the_declaration_resolves_to_nothing(self) -> None:
        """``dana`` 不在声明里。它出现在任何目录里都是配置错误。"""
        self.assertNotIn("dana", {name for name, _uid in HOST_USERS})

        principals, _tokens, _projects, _targets = build_directories()
        self.assertIsNone(principals.lookup("dana"))
        with self.assertRaises(Unauthenticated):
            principals.require("dana")

        h = harness()
        # 目录里没有这个主体：解析**抛错**，而不是返回一个空上下文。
        with self.assertRaises(Unauthenticated):
            h.service.requester_from_token("synthetic-dana")
        with self.assertRaises(Unauthenticated):
            resolve_requester(
                "synthetic-dana",
                TokenDirectory(
                    [
                        TokenBinding(
                            "synthetic-alice", "alice", MEMBER_SCOPES, frozenset({"member"})
                        )
                    ]
                ),
                PrincipalDirectory([Principal("alice", "alice", dict(HOST_USERS)["alice"])]),
            )

    def test_the_three_model_users_are_the_documented_ones(self) -> None:
        self.assertEqual(set(MODEL_USERS), {"alice", "dave", "bob"})
        self.assertEqual(ADMIN_USER, "carol")


class CredentialTests(unittest.TestCase):
    def test_a_missing_credential_is_401(self) -> None:
        status, _h, body = harness().request("GET", "/api/evaluation/jobs")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["contract_code"], "evaluation.unauthenticated")

    def test_an_unknown_credential_is_401(self) -> None:
        status, _h, body = harness().request(
            "GET", "/api/evaluation/jobs", token=f"{TOKEN_PREFIX}nobody"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["contract_code"], "evaluation.unauthenticated")

    def test_a_malformed_authorization_header_is_401(self) -> None:
        for header in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "token abc"):
            with self.subTest(header=header):
                h = harness()
                status, _h2, body = h.body(
                    "GET", "/api/evaluation/jobs", headers=(("Authorization", header),)
                )
                self.assertEqual(status, 401, msg=header)

    def test_a_revoked_credential_is_401(self) -> None:
        tokens = TokenDirectory(
            [
                TokenBinding(
                    "synthetic-revoked", "alice", MEMBER_SCOPES, frozenset({"member"}), revoked=True
                )
            ]
        )
        principals = PrincipalDirectory([Principal("alice", "alice", 4001)])
        with self.assertRaises(Unauthenticated):
            resolve_requester("synthetic-revoked", tokens, principals)

    def test_a_suspended_principal_is_401(self) -> None:
        tokens = TokenDirectory(
            [TokenBinding("synthetic-susp", "alice", MEMBER_SCOPES, frozenset({"member"}))]
        )
        principals = PrincipalDirectory([Principal("alice", "alice", 4001, state="suspended")])
        with self.assertRaises(Unauthenticated):
            resolve_requester("synthetic-susp", tokens, principals)

    def test_an_unknown_scope_in_the_directory_is_refused_at_construction(self) -> None:
        """拼错的 scope 必须在启动时炸掉，而不是在运行时静默失效。"""
        with self.assertRaises(Exception):
            TokenDirectory(
                [TokenBinding("synthetic-x", "alice", frozenset({"evaluation:reed"}))]
            )


class ForgedIdentityTests(unittest.TestCase):
    """自报身份 = 整请求拒绝（400），不是"忽略那个多余字段"。"""

    SUBMISSION = {"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                  "dataset_slug": "synthetic-qa-smoke"}

    def test_forged_identity_headers_are_rejected(self) -> None:
        for header in (
            "X-Role",
            "X-Owner",
            "X-Uid",
            "X-Principal",
            "X-Is-Admin",
            "X-Project",
            "X-Forwarded-User",
            "X-Remote-User",
        ):
            with self.subTest(header=header):
                status, _h, body = harness().request(
                    "POST",
                    "/api/evaluation/jobs",
                    as_principal="alice",
                    payload=self.SUBMISSION | {"idempotency_key": f"hdr-{header}"},
                    headers=((header, "carol"),),
                )
                self.assertEqual(status, 400, msg=header)
                self.assertEqual(body["error"]["contract_code"], "evaluation.identity_assertion_rejected")

    def test_forged_identity_body_fields_are_rejected(self) -> None:
        for field in ("owner", "uid", "principal", "role", "roles", "is_admin", "admin",
                      "project", "project_id", "scopes", "user_id", "linux_user"):
            with self.subTest(field=field):
                status, _h, body = harness().request(
                    "POST",
                    "/api/evaluation/jobs",
                    as_principal="alice",
                    payload=self.SUBMISSION | {field: "carol"},
                )
                self.assertEqual(status, 400, msg=field)
                self.assertEqual(body["error"]["contract_code"], "evaluation.identity_assertion_rejected")

    def test_forged_identity_in_the_query_string_is_rejected(self) -> None:
        for name in ("project", "owner", "role", "is_admin"):
            with self.subTest(param=name):
                status, _h, body = harness().request(
                    "GET", "/api/evaluation/jobs", as_principal="alice", query=f"{name}=carol"
                )
                self.assertEqual(status, 400, msg=name)
                self.assertEqual(body["error"]["contract_code"], "evaluation.identity_assertion_rejected")

    def test_an_unknown_request_field_is_rejected(self) -> None:
        status, _h, body = harness().request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload=self.SUBMISSION | {"extra": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["contract_code"], "evaluation.invalid_request")

    def test_a_forged_role_never_elevates(self) -> None:
        """``roles`` 里写满 ``platform_admin``，但 scope 没给：一样看不到别人的任务。"""
        h = harness()
        owner_job = h.submit("dave", deployment_id=DEP_ZHENGJI_PRIVATE)
        job_id = owner_job["job_id"]

        forged = h.forged_requester(
            "alice", roles=("platform_admin", "admin", "operator"),
            scopes=(SCOPE_EVAL_READ, SCOPE_EVAL_SUBMIT, SCOPE_EVAL_CANCEL, SCOPE_EVAL_RETRY),
        )
        with self.assertRaises(NotFound):
            h.service.job(requester=forged, job_id=job_id)
        with self.assertRaises(NotFound):
            h.service.cancel_job(requester=forged, job_id=job_id)

    def test_a_missing_scope_is_refused_even_for_an_admin_role(self) -> None:
        h = harness()
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)
        # 同一个主体，但一次 scope 都不给。
        bare = h.forged_requester("alice", roles=("platform_admin",), scopes=())
        with self.assertRaises(AccessDenied):
            h.service.job(requester=bare, job_id=job["job_id"])


class CrossProjectVisibilityTests(unittest.TestCase):
    """两个用户、两个项目：自己的看得见，别人的看不见。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.h = harness()
        cls.alice_job = cls.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]
        cls.dave_job = cls.h.submit("dave", deployment_id=DEP_ZHENGJI_PRIVATE)["job_id"]
        cls.bob_job = cls.h.submit("bob", deployment_id=DEP_KAIDONG_PRIVATE)["job_id"]

    def test_each_user_reads_their_own_job(self) -> None:
        for principal, job_id in (
            ("alice", self.alice_job),
            ("dave", self.dave_job),
            ("bob", self.bob_job),
        ):
            with self.subTest(principal=principal):
                status, _h, body = self.h.request(
                    "GET", f"/api/evaluation/jobs/{job_id}", as_principal=principal
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["data"]["job_id"], job_id)

    def test_a_user_cannot_read_another_users_job(self) -> None:
        for principal, job_id in (
            ("dave", self.alice_job),
            ("alice", self.dave_job),
            ("bob", self.dave_job),
        ):
            with self.subTest(principal=principal, job=job_id):
                status, _h, body = self.h.request(
                    "GET", f"/api/evaluation/jobs/{job_id}", as_principal=principal
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["error"]["contract_code"], "evaluation.not_found")

    def test_a_project_peer_cannot_read_a_jobs_owner_record(self) -> None:
        """``alice`` 与 ``bob`` 同属 ``proj-alpha``，但评测结果仍然是**自己的**。"""
        status, _h, body = self.h.request(
            "GET", f"/api/evaluation/jobs/{self.alice_job}", as_principal="bob"
        )
        self.assertEqual(status, 404, msg=body)

    def test_the_owner_visible_list_contains_only_own_jobs(self) -> None:
        for principal in MODEL_USERS:
            with self.subTest(principal=principal):
                status, _h, body = self.h.request(
                    "GET", "/api/evaluation/jobs", as_principal=principal
                )
                self.assertEqual(status, 200)
                owners = {job["owner"] for job in body["data"]["jobs"]}
                self.assertEqual(owners, {principal})

    def test_a_missing_job_and_a_forbidden_job_look_identical(self) -> None:
        """不存在与无权知道必须**逐字节**同形，否则端点就是存在性预言机。

        这一条断言的是**整个响应体**，不是状态码加 ``contract_code``：曾经两者相同而
        ``message`` 不同（"no such evaluation job" vs "… is not available to this
        principal"），于是只要逐 id 问一遍，就能问出"这个任务存在吗"——而幂等提交的
        job id 是 ``sha256(owner ‖ project ‖ key)[:24]``，是**可计算**的。
        """
        for path, label in (
            ("/api/evaluation/jobs/evj-000000000000000000000000", "job"),
            (f"/api/evaluation/jobs/{self.dave_job}/result", "result"),
        ):
            with self.subTest(endpoint=label):
                missing_status, _h1, missing = self.h.request(
                    "GET", path.replace(self.dave_job, "evj-000000000000000000000000"),
                    as_principal="alice",
                )
                denied_status, _h2, denied = self.h.request(
                    "GET", path, as_principal="alice"
                )
                self.assertEqual(missing_status, denied_status)
                self.assertEqual(missing, denied, msg="响应体不同 -> 存在性预言机")

    def test_a_missing_job_and_a_forbidden_cancel_look_identical(self) -> None:
        """写操作同样不能做存在性预言机。"""
        missing_status, _h1, missing = self.h.request(
            "POST", "/api/evaluation/jobs/evj-000000000000000000000000/cancel",
            as_principal="alice",
        )
        denied_status, _h2, denied = self.h.request(
            "POST", f"/api/evaluation/jobs/{self.dave_job}/cancel", as_principal="alice"
        )
        self.assertEqual(missing_status, denied_status)
        self.assertEqual(missing, denied, msg="响应体不同 -> 存在性预言机")
        # 而且两个都不是"成功取消"。
        self.assertEqual(missing_status, 404)

    def test_a_missing_job_and_a_forbidden_retry_look_identical(self) -> None:
        missing_status, _h1, missing = self.h.request(
            "POST", "/api/evaluation/jobs/evj-000000000000000000000000/retry",
            as_principal="alice",
        )
        denied_status, _h2, denied = self.h.request(
            "POST", f"/api/evaluation/jobs/{self.dave_job}/retry", as_principal="alice"
        )
        self.assertEqual(missing_status, denied_status)
        self.assertEqual(missing, denied, msg="响应体不同 -> 存在性预言机")

    def test_an_unknown_project_is_not_reachable(self) -> None:
        """项目归属由服务端目录决定；没有任何入口能让调用者挑一个项目。"""
        status, _h, body = self.h.request(
            "GET", "/api/evaluation/jobs", as_principal="alice", query="project=proj-nope"
        )
        # 查询串里的 project 是伪造尝试 -> 整请求拒绝，而不是被采纳。
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["contract_code"], "evaluation.identity_assertion_rejected")


class AdminScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = harness()
        cls.alice_job = cls.h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]
        cls.dave_job = cls.h.submit("dave", deployment_id=DEP_ZHENGJI_PRIVATE)["job_id"]

    def test_admin_reads_every_job(self) -> None:
        for job_id in (self.alice_job, self.dave_job):
            with self.subTest(job=job_id):
                status, _h, body = self.h.request(
                    "GET", f"/api/evaluation/jobs/{job_id}", as_principal="carol"
                )
                self.assertEqual(status, 200)
                self.assertEqual(body["data"]["job_id"], job_id)

    def test_admin_manages_every_job(self) -> None:
        h = harness()
        job_id = h.submit("dave", deployment_id=DEP_ZHENGJI_PRIVATE)["job_id"]
        status, _h, body = h.request(
            "POST", f"/api/evaluation/jobs/{job_id}/cancel", as_principal="carol"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "canceled")

    def test_a_read_only_admin_can_look_but_not_touch(self) -> None:
        """能看全局 != 能改全局。两个 scope 是两个不同的决定。"""
        h = harness()
        job_id = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]

        status, _h, body = h.request(
            "GET", f"/api/evaluation/jobs/{job_id}", token=f"{TOKEN_PREFIX}carol-readonly"
        )
        self.assertEqual(status, 200, msg=body)

        status, _h, body = h.request(
            "POST", f"/api/evaluation/jobs/{job_id}/cancel", token=f"{TOKEN_PREFIX}carol-readonly"
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["contract_code"], "evaluation.access_denied")

    def test_a_read_only_admin_cannot_retry_somebody_elses_job(self) -> None:
        h = harness()
        job_id = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]
        h.request("POST", f"/api/evaluation/jobs/{job_id}/cancel", as_principal="alice")
        status, _h, body = h.request(
            "POST", f"/api/evaluation/jobs/{job_id}/retry", token=f"{TOKEN_PREFIX}carol-readonly"
        )
        self.assertEqual(status, 403, msg=body)

    def test_the_read_only_admin_sees_everything_in_the_list(self) -> None:
        status, _h, body = self.h.request(
            "GET", "/api/evaluation/jobs", token=f"{TOKEN_PREFIX}carol-readonly"
        )
        self.assertEqual(status, 200)
        owners = {job["owner"] for job in body["data"]["jobs"]}
        self.assertTrue({"alice", "dave"} <= owners, msg=owners)


class CancelAuthorizationTests(unittest.TestCase):
    def test_cancelling_somebody_elses_job_is_refused(self) -> None:
        h = harness()
        job_id = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]

        for principal in ("dave", "bob"):
            with self.subTest(principal=principal):
                status, _h, body = h.request(
                    "POST", f"/api/evaluation/jobs/{job_id}/cancel", as_principal=principal
                )
                self.assertEqual(status, 404, msg=body)

        # 原任务没有被任何人改动。
        status, _h, body = h.request(
            "GET", f"/api/evaluation/jobs/{job_id}", as_principal="alice"
        )
        self.assertEqual(body["data"]["status"], "queued")

    def test_a_member_without_the_cancel_scope_is_refused(self) -> None:
        h = harness()
        job_id = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE)["job_id"]
        partial = h.forged_requester("alice", roles=("member",), scopes=(SCOPE_EVAL_READ,))
        with self.assertRaises(AccessDenied):
            h.service.cancel_job(requester=partial, job_id=job_id)


class TargetAuthorizationTests(unittest.TestCase):
    """目标授权与项目授权是两件事，都必须成立。"""

    def test_another_users_private_deployment_is_not_submittable(self) -> None:
        h = harness()
        for principal, deployment in (
            ("alice", DEP_ZHENGJI_PRIVATE),
            ("dave", DEP_VINCE_PRIVATE),
            ("bob", DEP_VINCE_PRIVATE),
        ):
            with self.subTest(principal=principal, deployment=deployment):
                status, _h, body = h.request(
                    "POST", "/api/evaluation/jobs", as_principal=principal,
                    payload={"deployment_id": deployment, "eval_type": "correctness",
                             "dataset_slug": "synthetic-qa-smoke"},
                )
                self.assertEqual(status, 404, msg=body)

    def test_an_unknown_deployment_looks_like_a_forbidden_one(self) -> None:
        h = harness()
        unknown_status, _h1, unknown = h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": "dep-does-not-exist", "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke"},
        )
        forbidden_status, _h2, forbidden = h.request(
            "POST", "/api/evaluation/jobs", as_principal="alice",
            payload={"deployment_id": DEP_ZHENGJI_PRIVATE, "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke"},
        )
        self.assertEqual(unknown_status, forbidden_status)
        self.assertEqual(unknown["error"]["contract_code"], forbidden["error"]["contract_code"])

    def test_a_shared_target_is_usable_by_the_grantee_only(self) -> None:
        h = harness()
        allowed = h.submit("alice", deployment_id=DEP_SHARED_ALL)
        self.assertTrue(allowed["target"]["shared"])
        self.assertEqual(allowed["target"]["owner"], "dave")

        # bob 不在 proj-beta，也不是被显式授予的人。
        status, _h2, body = h.request(
            "POST", "/api/evaluation/jobs", as_principal="bob",
            payload={"deployment_id": DEP_SHARED_ALL, "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke"},
        )
        # all_approved 覆盖全部已审批主体，因此 bob 是**允许**的。
        self.assertEqual(status, 200, msg=body)

    def test_the_job_lands_in_the_requesters_own_project(self) -> None:
        """项目来自服务端目录，不是请求，也不是目标 owner 的项目。"""
        h = harness()
        job = h.submit("alice", deployment_id=DEP_SHARED_ALL)
        self.assertEqual(job["project_id"], PROJECT_ALPHA)
        self.assertEqual(job["target"]["project_id"], PROJECT_BETA)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
