"""Portal 模块目录的 fail-closed 回归。

这个套件要证明的核心是一句话：**没有授权与可信来源时，这个接口什么都不会声称。**

四组：

1. **授权** —— 缺 scope / 缺会话一律 ``AccessDenied``；``role == admin`` 不是回退。
2. **状态与原因** —— ``available`` 需要前置条件**全部**成立；未成立时必须给出原因，
   而且「本构建永远没有真实后端」（disabled）与「这次部署没配」（unavailable）
   是两个不同的状态。
3. **入口（链接）** —— 只在条件全成立时出现；只允许同源路径；**不得**指向上游端口；
   模块可用 ≠ 其原生界面也开放。
4. **数值** —— 没有来源就是 ``null`` + unknown；GPU 指标在本构建里**永远是** null。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.atlas.dto import Provenance  # noqa: E402
from backend.atlas.errors import AccessDenied, ContractError, InvalidRequest  # noqa: E402
from backend.atlas.identity import (  # noqa: E402
    SCOPE_CALLS_SELF_READ,
    RequesterContext,
)
from backend.portal.modules import (  # noqa: E402
    GPU_METRIC_PREFIX,
    MODULES_SCHEMA_VERSION,
    REASON_ENGINE_NOT_STRATA,
    REASON_GATEWAY_NOT_ATTACHED,
    REASON_MISSING_SCOPE,
    REASON_NO_VERIFIED_SOURCE,
    REASON_NOT_CONFIGURED,
    REASON_PREVIEW_ONLY,
    SCOPE_ENGINES_NATIVE,
    SCOPE_ENDPOINTS_SHARE,
    SCOPE_EVALUATION_NATIVE,
    SCOPE_MODULES_READ,
    SCOPE_OBSERVABILITY_NATIVE,
    STATE_AVAILABLE,
    STATE_DISABLED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    DeploymentFacts,
    EndpointRecord,
    ModuleRequest,
    build_modules_dto,
    validate_modules_dto,
)

DEMO = Provenance(source="synthetic-preview", synthetic=True)
LIVE = Provenance(source="gateway-export", synthetic=False)

ALL_SCOPES = (
    SCOPE_MODULES_READ,
    SCOPE_ENGINES_NATIVE,
    SCOPE_EVALUATION_NATIVE,
    SCOPE_OBSERVABILITY_NATIVE,
    SCOPE_ENDPOINTS_SHARE,
    SCOPE_CALLS_SELF_READ,
)


def requester(subject="local:abc", *, scopes=ALL_SCOPES, projects=("proj-a",), roles=("user",)):
    return RequesterContext(
        subject=subject,
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        authorized_projects=frozenset(projects),
    )


def build(ctx=None, *, facts=None, provenance=DEMO, visibility=VISIBILITY_PRIVATE, endpoints=()):
    return build_modules_dto(
        ModuleRequest(
            requester=ctx if ctx is not None else requester(),
            visibility=visibility,
            endpoints=tuple(endpoints),
        ),
        facts if facts is not None else DeploymentFacts(),
        provenance,
    )


def by_id(dto, module_id):
    return next(m for m in dto["modules"] if m["id"] == module_id)


class AuthorizationTest(unittest.TestCase):
    def test_the_modules_scope_is_required(self) -> None:
        with self.assertRaises(AccessDenied):
            build(requester(scopes=(SCOPE_ENGINES_NATIVE,)))

    def test_no_session_is_denied(self) -> None:
        for subject in ("", "   "):
            with self.subTest(subject=repr(subject)):
                with self.assertRaises(AccessDenied):
                    build(requester(subject))

    def test_an_admin_role_is_not_a_fallback(self) -> None:
        ctx = requester(scopes=(), roles=("admin", "platform_admin"))
        with self.assertRaises(AccessDenied):
            build(ctx)

    def test_an_unknown_visibility_mode_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            build(visibility="everything")

    def test_a_wrong_request_type_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_modules_dto("nope", DeploymentFacts(), DEMO)  # type: ignore[arg-type]


class StateAndReasonTest(unittest.TestCase):
    """状态必须由前置条件推出，且总是带原因。"""

    def setUp(self) -> None:
        self.dto = build()

    def test_every_module_states_a_reason_when_it_is_not_available(self) -> None:
        for module in self.dto["modules"]:
            with self.subTest(module=module["id"]):
                if module["state"] == STATE_AVAILABLE:
                    self.assertIsNone(module["reason"])
                else:
                    self.assertTrue(module["reason"], "a non-available module needs a reason")

    def test_playground_is_disabled_not_merely_unavailable(self) -> None:
        """「本构建永远没有真实后端」与「这次部署没配」不是同一件事。"""
        playground = by_id(self.dto, "playground")
        self.assertEqual(playground["state"], STATE_DISABLED)
        self.assertEqual(playground["reason"], REASON_PREVIEW_ONLY)

    def test_an_unknown_engine_is_unknown_not_a_wrong_engine(self) -> None:
        engines = by_id(self.dto, "engines")
        self.assertEqual(engines["state"], STATE_UNKNOWN)
        self.assertEqual(engines["reason"], REASON_NOT_CONFIGURED)

    def test_a_named_non_strata_engine_is_unavailable(self) -> None:
        dto = build(facts=DeploymentFacts(engine="vllm", gateway_attached=True))
        engines = by_id(dto, "engines")
        self.assertEqual(engines["state"], STATE_UNAVAILABLE)
        self.assertEqual(engines["reason"], REASON_ENGINE_NOT_STRATA)

    def test_strata_without_a_gateway_is_unavailable(self) -> None:
        dto = build(facts=DeploymentFacts(engine="strata"))
        engines = by_id(dto, "engines")
        self.assertEqual(engines["state"], STATE_UNAVAILABLE)
        self.assertEqual(engines["reason"], REASON_GATEWAY_NOT_ATTACHED)

    def test_strata_with_a_gateway_is_available(self) -> None:
        dto = build(facts=DeploymentFacts(engine="strata", gateway_attached=True, gateway_path="/gw"))
        self.assertEqual(by_id(dto, "engines")["state"], STATE_AVAILABLE)

    def test_a_missing_scope_makes_a_module_unavailable(self) -> None:
        dto = build(
            requester(scopes=(SCOPE_MODULES_READ,)),
            facts=DeploymentFacts(evalscope_path="/evalscope"),
        )
        evaluation = by_id(dto, "evaluation")
        self.assertEqual(evaluation["state"], STATE_UNAVAILABLE)
        self.assertEqual(evaluation["reason"], REASON_MISSING_SCOPE)

    def test_the_visibility_lists_only_available_modules(self) -> None:
        visible = self.dto["scope"]["visible_modules"]
        derived = sorted(m["id"] for m in self.dto["modules"] if m["state"] == STATE_AVAILABLE)
        self.assertEqual(visible, derived)
        self.assertNotIn("playground", visible)


class LinksTest(unittest.TestCase):
    """入口只在真的能打开时出现，且只能是同源路径。"""

    def test_no_native_console_link_without_strata(self) -> None:
        dto = build(facts=DeploymentFacts(engine="vllm", gateway_attached=True, gateway_path="/gw"))
        self.assertEqual(by_id(dto, "engines")["links"], [])

    def test_no_native_console_link_without_a_gateway(self) -> None:
        dto = build(facts=DeploymentFacts(engine="strata", gateway_path="/gw"))
        self.assertEqual(by_id(dto, "engines")["links"], [])

    def test_the_native_console_link_needs_its_own_scope(self) -> None:
        """模块 available **不**意味着它的原生界面也开放 —— 两套判定。

        缺 scope 时入口以**被挡住**的形式出现并说明原因，而不是悄悄消失：
        「你没有这个权限」比一个无法解释的空白更有用，也更诚实。关键是它
        **没有地址** —— 页面上不可能出现一个点了会失败的链接。
        """
        dto = build(
            requester(scopes=(SCOPE_MODULES_READ, SCOPE_CALLS_SELF_READ)),
            facts=DeploymentFacts(engine="strata", gateway_attached=True, gateway_path="/gw"),
        )
        engines = by_id(dto, "engines")
        self.assertEqual(engines["state"], STATE_AVAILABLE)
        self.assertEqual(len(engines["links"]), 1)
        blocked = engines["links"][0]
        self.assertIsNone(blocked["href"], "a blocked entry must not carry an address")
        self.assertEqual(blocked["requires"], SCOPE_ENGINES_NATIVE)
        self.assertTrue(blocked["blocked"])

    def test_the_native_console_link_appears_only_when_everything_holds(self) -> None:
        dto = build(
            facts=DeploymentFacts(engine="strata", gateway_attached=True, gateway_path="/gw"),
        )
        hrefs = [link["href"] for link in by_id(dto, "engines")["links"]]
        self.assertEqual(hrefs, ["/gw/strata/chat", "/gw/strata/monitor"])

    def test_a_link_may_never_leave_the_origin_or_reach_an_upstream_port(self) -> None:
        """这是「不绕过 Gateway/Proxy」的落点：页面里不可能出现直连上游的地址。"""
        for href in (
            "http://127.0.0.1:8080/v1/chat",
            "https://lab.example/v1",
            "//evil.example/x",
            "/gw/../..:8080",
            "127.0.0.1:8080",
        ):
            with self.subTest(href=href):
                with self.assertRaises(InvalidRequest):
                    from backend.portal.modules import _internal_link

                    _internal_link(href, "x", "native", "y")

    def test_a_link_at_an_upstream_port_is_refused_even_as_a_same_origin_path(self) -> None:
        """同源路径也可能是绕过 Gateway 的那一条 —— 端口出现即拒绝。"""
        from backend.portal.modules import _internal_link

        for href in ("/proxy/127.0.0.1:8080/v1", "/gw/../..:8080", "/x:8080"):
            with self.subTest(href=href):
                with self.assertRaises(InvalidRequest):
                    _internal_link(href, "x", "native", "y")

    def test_a_bare_numeric_path_segment_is_not_treated_as_a_port(self) -> None:
        """规则的边界要说清楚：被拦的是「地址里写了一个端口」，不是「路径里有 8080」。

        ``/8080/v1`` 只是本机服务器上的一个路径段；浏览器按同源请求它，路由到哪
        由服务端决定，因此它并不构成一条绕过 Gateway 的通道。把它也拦下来是
        装模作样，只会让规则看起来更严而实际更含糊。
        """
        from backend.portal.modules import _internal_link

        self.assertEqual(_internal_link("/8080/v1", "x", "native", "y")["href"], "/8080/v1")

    def test_a_plain_internal_path_is_accepted(self) -> None:
        from backend.portal.modules import _internal_link

        link = _internal_link("/gw/strata/chat", "原生 Chat", "native", "engine=strata")
        self.assertEqual(link["href"], "/gw/strata/chat")

    def test_the_generated_dto_has_no_absolute_or_upstream_links_at_all(self) -> None:
        dto = build(
            facts=DeploymentFacts(
                engine="strata",
                gateway_attached=True,
                gateway_path="/gw",
                grafana_path="/grafana",
                prometheus_path="/prom",
                evalscope_path="/evalscope",
            )
        )
        for module in dto["modules"]:
            for link in module["links"]:
                href = link["href"]
                if href is None:
                    continue
                self.assertTrue(href.startswith("/"), href)
                self.assertNotIn("://", href)
                self.assertNotIn(":8080", href)


class MetricsTest(unittest.TestCase):
    """没有来源就没有数字。"""

    def test_every_metric_without_a_source_is_null_and_unknown(self) -> None:
        dto = build()
        metrics = [m for module in dto["modules"] for m in module["metrics"]]
        self.assertTrue(metrics)
        for metric in metrics:
            self.assertIsNone(metric["value"], metric["name"])
            self.assertEqual(metric["state"], STATE_UNKNOWN)
            self.assertEqual(metric["reason"], REASON_NO_VERIFIED_SOURCE)
            self.assertIsNone(metric["source"])

    def test_gpu_metrics_are_never_a_number_in_this_build(self) -> None:
        """本构建没有 NVML 来源 —— 因此不可能出现一个看起来正常的显存数字。

        这条断言在「已接线、来源齐备」的配置下也必须成立，因为那个配置里
        声明的来源是 prometheus，而不是 NVML。
        """
        for facts in (
            DeploymentFacts(),
            DeploymentFacts(gateway_attached=True),
            DeploymentFacts(
                gateway_attached=True,
                attached_sources=frozenset({"prometheus-readonly"}),
                metrics={"gateway_requests_24h": (10.0, "prometheus-readonly")},
            ),
        ):
            with self.subTest(facts=facts):
                dto = build(facts=facts)
                gpu = [
                    m
                    for module in dto["modules"]
                    for m in module["metrics"]
                    if m["name"].startswith(GPU_METRIC_PREFIX)
                ]
                self.assertTrue(gpu)
                for metric in gpu:
                    self.assertIsNone(metric["value"], metric["name"])

    def test_a_declared_verified_source_does_produce_a_number(self) -> None:
        dto = build(
            facts=DeploymentFacts(
                gateway_attached=True,
                attached_sources=frozenset({"prometheus-readonly"}),
                metrics={"gateway_requests_24h": (812.0, "prometheus-readonly")},
            )
        )
        metric = next(
            m for module in dto["modules"] for m in module["metrics"]
            if m["name"] == "gateway_requests_24h"
        )
        self.assertEqual(metric["value"], 812.0)
        self.assertEqual(metric["source"], "prometheus-readonly")
        self.assertEqual(metric["state"], STATE_AVAILABLE)

    def test_a_metric_may_not_cite_an_unverified_source(self) -> None:
        with self.assertRaises(InvalidRequest):
            DeploymentFacts(metrics={"x": (1.0, "made-up-source")})

    def test_a_metric_may_not_cite_a_source_that_is_not_attached(self) -> None:
        with self.assertRaises(InvalidRequest):
            DeploymentFacts(metrics={"x": (1.0, "prometheus-readonly")})

    def test_an_unknown_source_cannot_even_be_declared_attached(self) -> None:
        with self.assertRaises(InvalidRequest):
            DeploymentFacts(attached_sources=frozenset({"definitely-not-real"}))


class TransportHonestyTest(unittest.TestCase):
    def test_the_demo_build_never_claims_to_be_live(self) -> None:
        dto = build()
        self.assertEqual(dto["transport"]["mode"], "demo")
        self.assertTrue(dto["synthetic"])

    def test_a_synthetic_payload_is_demo_even_with_a_gateway_attached(self) -> None:
        dto = build(facts=DeploymentFacts(gateway_attached=True))
        self.assertEqual(dto["transport"]["mode"], "demo")

    def test_live_requires_a_real_source_and_an_attached_gateway(self) -> None:
        attached = build(facts=DeploymentFacts(gateway_attached=True), provenance=LIVE)
        self.assertEqual(attached["transport"]["mode"], "live")
        timid = build(facts=DeploymentFacts(), provenance=LIVE)
        self.assertEqual(timid["transport"]["mode"], "demo")

    def test_the_hints_say_this_is_not_a_production_deployment(self) -> None:
        hints = " ".join(build()["display_hints"])
        self.assertIn("未接线真实 Gateway", hints)
        self.assertIn("UNKNOWN", hints)


class EndpointVisibilityTest(unittest.TestCase):
    """默认私有；Shared 只在已授权项目内、且由运维标记后可见。"""

    RECORDS = (
        EndpointRecord("ep-1", "my-chat", "local:abc", "proj-a", "OpenAI Chat", "llm-a"),
        EndpointRecord("ep-2", "team-vision", "local:abc", "proj-a", "Images", "llm-a", shared=True),
        EndpointRecord("ep-3", "colleague", "local:zzz", "proj-a", "OpenAI Chat", "llm-b"),
        EndpointRecord("ep-4", "other-project", "local:zzz", "proj-b", "OpenAI Chat", "llm-c", shared=True),
    )

    def test_the_default_view_is_private_and_holds_only_my_endpoints(self) -> None:
        dto = build(endpoints=self.RECORDS)
        block = dto["endpoints"]
        self.assertEqual(block["visibility"], VISIBILITY_PRIVATE)
        self.assertEqual([i["endpoint_id"] for i in block["items"]], ["ep-1", "ep-2"])
        self.assertTrue(all(i["scope"] == VISIBILITY_PRIVATE for i in block["items"]))

    def test_private_never_shows_a_colleague(self) -> None:
        dto = build(endpoints=self.RECORDS)
        self.assertNotIn("colleague", json.dumps(dto["endpoints"], ensure_ascii=False))

    def test_shared_adds_only_shared_endpoints_inside_my_projects(self) -> None:
        dto = build(endpoints=self.RECORDS, visibility=VISIBILITY_SHARED)
        ids = [i["endpoint_id"] for i in dto["endpoints"]["items"]]
        # ep-1 and ep-2 are mine. ep-4 is shared but sits in proj-b, which this
        # subject is not authorised for, so it must NOT appear. ep-3 is a
        # colleague's in my project and is not shared, so it must not either.
        self.assertEqual(ids, ["ep-1", "ep-2"])
        self.assertNotIn("ep-3", ids)
        self.assertNotIn("ep-4", ids)

    def test_switching_to_shared_never_hides_my_own_endpoints(self) -> None:
        private = build(endpoints=self.RECORDS)["endpoints"]["items"]
        shared = build(endpoints=self.RECORDS, visibility=VISIBILITY_SHARED)["endpoints"]["items"]
        self.assertTrue({i["endpoint_id"] for i in private} <= {i["endpoint_id"] for i in shared})

    def test_publishing_shared_requires_its_own_scope(self) -> None:
        without = build(requester(scopes=(SCOPE_MODULES_READ,)), endpoints=self.RECORDS)
        self.assertFalse(without["endpoints"]["can_publish_shared"])
        with_scope = build(endpoints=self.RECORDS)
        self.assertTrue(with_scope["endpoints"]["can_publish_shared"])

    def test_the_client_cannot_widen_visibility_through_the_payload(self) -> None:
        """可见性是**服务端**算好的两个子集，客户端只是在它们之间切换。"""
        dto = build(endpoints=self.RECORDS, visibility=VISIBILITY_PRIVATE)
        for item in dto["endpoints"]["items"]:
            self.assertEqual(item["owner"], "self")


class SelfCheckTest(unittest.TestCase):
    """出口自检必须真的会失败。"""

    def setUp(self) -> None:
        self.dto = build(
            facts=DeploymentFacts(engine="strata", gateway_attached=True, gateway_path="/gw")
        )

    def _tampered(self, mutate):
        dto = copy.deepcopy(self.dto)
        mutate(dto)
        return dto

    def test_the_baseline_passes(self) -> None:
        validate_modules_dto(self.dto)
        self.assertEqual(self.dto["schema_version"], MODULES_SCHEMA_VERSION)

    def test_an_available_module_without_its_scope_is_refused(self) -> None:
        def mutate(dto):
            module = by_id(dto, "evaluation")
            module["state"] = STATE_AVAILABLE
            module["reason"] = None
            dto["scope"]["visible_modules"].append("evaluation")

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_an_available_module_with_nothing_to_show_is_refused(self) -> None:
        def mutate(dto):
            module = by_id(dto, "administration")
            module["data"] = {}
            module["metrics"] = []
            module["links"] = []

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_a_gpu_metric_with_a_value_is_refused(self) -> None:
        def mutate(dto):
            metric = next(
                m for module in dto["modules"] for m in module["metrics"]
                if m["name"].startswith(GPU_METRIC_PREFIX)
            )
            metric["value"] = 4096.0
            metric["state"] = STATE_AVAILABLE
            metric["source"] = "prometheus-readonly"
            metric["reason"] = None

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_a_metric_with_an_unverified_source_is_refused(self) -> None:
        def mutate(dto):
            metric = next(
                m for module in dto["modules"] for m in module["metrics"]
            )
            metric["value"] = 1.0
            metric["state"] = STATE_AVAILABLE
            metric["source"] = "trust-me"
            metric["reason"] = None

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_an_absolute_link_is_refused(self) -> None:
        def mutate(dto):
            by_id(dto, "engines")["links"].append(
                {"href": "https://lab.example/gw", "label": "x", "kind": "native", "requires": "y"}
            )

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_a_link_to_an_upstream_port_is_refused(self) -> None:
        def mutate(dto):
            by_id(dto, "engines")["links"].append(
                {"href": "/127.0.0.1:8080/v1", "label": "x", "kind": "native", "requires": "y"}
            )

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_claiming_live_on_synthetic_data_is_refused(self) -> None:
        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(lambda dto: dto["transport"].__setitem__("mode", "live")))

    def test_an_unknown_module_id_is_refused(self) -> None:
        def mutate(dto):
            dto["modules"].append(
                {
                    "id": "surprise", "title": "x", "detail": "x", "state": STATE_UNKNOWN,
                    "reason": "r", "requires_scopes": [], "requires": [],
                    "data": {}, "metrics": [], "links": [],
                }
            )

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_a_visible_modules_list_that_disagrees_is_refused(self) -> None:
        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(lambda dto: dto["scope"].__setitem__("visible_modules", ["nope"])))

    def test_a_shared_endpoint_in_a_private_view_is_refused(self) -> None:
        def mutate(dto):
            dto["endpoints"]["items"].append(
                {"endpoint_id": "x", "name": "x", "project": "proj-a", "protocol": "p",
                 "model": "m", "owner": "shared", "scope": VISIBILITY_SHARED}
            )

        with self.assertRaises(ContractError):
            validate_modules_dto(self._tampered(mutate))

    def test_the_dto_is_json_serialisable(self) -> None:
        self.assertEqual(json.loads(json.dumps(self.dto, sort_keys=True)), self.dto)


if __name__ == "__main__":
    unittest.main()
