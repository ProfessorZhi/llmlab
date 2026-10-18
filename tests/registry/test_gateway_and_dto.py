"""Gateway 边界与 DTO 形状：只读路由契约、调用替身、以及「合成数据必须自我声明」。

路由契约那条测试里有一个方向性断言：**私有的与未就绪的 Endpoint 绝不能出现在
路由配置里**。写进去不只是噪声 —— 那份文件会变成一份「谁注册了什么」的清单，
而它本来是要给运维读的。
"""

from __future__ import annotations

import json
import unittest

from backend.registry import dto
from backend.registry.errors import InvalidRequest, UpstreamUnavailable
from backend.registry.gateway import (
    REASON_GATEWAY_NOT_ATTACHED,
    DetachedGatewayAdapter,
    MockGatewayAdapter,
    build_routing_config,
)
from backend.registry.health import unknown as unknown_health
from backend.registry.models import (
    Deployment,
    Endpoint,
    Engine,
    Model,
    utc_now,
)
from tests.registry.support import Harness, close_all

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


def _engine(engine_id: str, name: str, port: int = 8000) -> Engine:
    return Engine(
        id=engine_id,
        owner_principal="alice",
        owner_uid=4001,
        name=name,
        target={
            "scheme": "http",
            "host": "127.0.0.1",
            "port": port,
            "path": "/v1",
            "query": "",
            "url": f"http://127.0.0.1:{port}/v1",
        },
        protocol="openai_compatible",
        status="verified",
        health=unknown_health(),
        source="client_registered",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _model(model_id: str, name: str) -> Model:
    return Model(
        id=model_id,
        owner_principal="alice",
        owner_uid=4001,
        name=name,
        weight_path="/home/alice/models/m",
        weight_path_sha256="deadbeefdeadbeef",
        format="gguf",
        parameter_count="8B",
        status="registered",
        health=unknown_health(),
        source="client_registered",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _deployment(deployment_id: str, engine_id: str, model_id: str) -> Deployment:
    return Deployment(
        id=deployment_id,
        owner_principal="alice",
        owner_uid=4001,
        name="dep",
        engine_id=engine_id,
        model_id=model_id,
        status="ready",
        health=unknown_health(),
        source="client_registered",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _endpoint(endpoint_id: str, deployment_id: str, *, visibility: str, status: str) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        owner_principal="alice",
        owner_uid=4001,
        name="svc",
        deployment_id=deployment_id,
        route_path=f"/x/alice/{endpoint_id}",
        visibility=visibility,
        status=status,
        health=unknown_health(),
        source="client_registered",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


class RoutingConfigTest(unittest.TestCase):
    def _config(self, endpoints: list[Endpoint]):
        deployments = {item.id: item for item in [_deployment("dep_1", "eng_1", "mdl_1")]}
        engines = {"eng_1": _engine("eng_1", "e")}
        models = {"mdl_1": _model("mdl_1", "m")}
        counts = {item.id: 3 for item in endpoints}
        return build_routing_config(endpoints, deployments, engines, models, counts)

    def test_a_ready_shared_endpoint_is_included(self) -> None:
        endpoint = _endpoint("ep_1", "dep_1", visibility="users", status="ready")
        config = self._config([endpoint])
        self.assertEqual(len(config["entries"]), 1)
        entry = config["entries"][0]
        self.assertEqual(entry["endpoint_id"], "ep_1")
        self.assertEqual(entry["upstream"]["host"], "127.0.0.1")
        self.assertEqual(entry["upstream"]["port"], 8000)
        self.assertEqual(entry["grantee_count"], 3)
        self.assertTrue(entry["ready"])

    def test_a_private_endpoint_is_excluded(self) -> None:
        endpoint = _endpoint("ep_1", "dep_1", visibility="private", status="ready")
        config = self._config([endpoint])
        self.assertEqual(config["entries"], [])
        self.assertEqual(config["excluded_endpoints"], 1)

    def test_an_unready_endpoint_is_excluded(self) -> None:
        for status in ("registered", "unavailable"):
            with self.subTest(status=status):
                endpoint = _endpoint("ep_1", "dep_1", visibility="users", status=status)
                config = self._config([endpoint])
                self.assertEqual(config["entries"], [])

    def test_the_config_carries_no_credentials(self) -> None:
        endpoint = _endpoint("ep_1", "dep_1", visibility="users", status="ready")
        config = self._config([endpoint])
        entry = config["entries"][0]
        # 结构断言而非关键词扫描：关键词扫描会被「说明自己没有凭据」的那句话
        # 误伤（note 里就写着 per-user key），反而放过一个真的多出来的字段。
        self.assertEqual(
            set(entry),
            {
                "endpoint_id",
                "route_path",
                "owner",
                "owner_uid",
                "protocol",
                "model",
                "upstream",
                "visibility",
                "grantee_count",
                "ready",
                "credentials",
            },
        )
        self.assertIsNone(entry["credentials"])
        self.assertEqual(set(entry["upstream"]), {"host", "port", "path"})
        self.assertEqual(set(config), {"contract", "read_only", "entries", "excluded_endpoints", "notes"})

    def test_the_config_declares_itself_read_only(self) -> None:
        config = self._config([])
        self.assertTrue(config["read_only"])
        self.assertEqual(config["contract"], "registry-routing/1")
        self.assertTrue(any("只读" in note for note in config["notes"]))


class RoutingConfigApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")

    def test_a_private_endpoint_stays_out_of_the_config(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/routing-config", principal="carol"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["entries"], [])

    def test_a_shared_ready_endpoint_appears(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/routing-config", principal="carol"
        )
        self.assertEqual(len(payload["data"]["entries"]), 1)
        self.assertEqual(payload["data"]["entries"][0]["visibility"], "users")
        self.assertEqual(payload["data"]["entries"][0]["grantee_count"], 1)

    def test_a_plain_user_cannot_read_the_config(self) -> None:
        status, _h, _p = self.h.request(
            "GET", f"{PREFIX}/routing-config", principal="alice"
        )
        self.assertEqual(status, 403)


class GatewayAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")
        # invoke 的路由门要求一次真实回读确认（PR #118）；夹具显式完成它。
        self.h.confirm_route(self.endpoint_id)

    def test_the_detached_adapter_refuses_and_says_why(self) -> None:
        adapter = DetachedGatewayAdapter()
        endpoint = _endpoint("ep_1", "dep_1", visibility="private", status="ready")
        with self.assertRaises(UpstreamUnavailable) as ctx:
            adapter.forward(
                endpoint=endpoint,
                deployment=_deployment("dep_1", "eng_1", "mdl_1"),
                engine=_engine("eng_1", "e"),
                model=_model("mdl_1", "m"),
                payload={},
            )
        self.assertIn(REASON_GATEWAY_NOT_ATTACHED, str(ctx.exception))
        self.assertFalse(adapter.attached)

    def test_the_mock_adapter_records_the_call_and_marks_itself(self) -> None:
        adapter = MockGatewayAdapter()
        result = adapter.forward(
            endpoint=_endpoint("ep_1", "dep_1", visibility="users", status="ready"),
            deployment=_deployment("dep_1", "eng_1", "mdl_1"),
            engine=_engine("eng_1", "e"),
            model=_model("mdl_1", "m"),
            payload={"prompt": "hi"},
        )
        self.assertTrue(result["synthetic"])
        self.assertTrue(result["forwarded"])
        # 替身没有量过 token 与延迟：必须是 null，不是编出来的 0。
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["latency_ms"])
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(any("MOCK" in note for note in result["notes"]))

    def test_the_mock_adapter_can_be_made_to_fail(self) -> None:
        adapter = MockGatewayAdapter(failing=True)
        with self.assertRaises(UpstreamUnavailable):
            adapter.forward(
                endpoint=_endpoint("ep_1", "dep_1", visibility="users", status="ready"),
                deployment=_deployment("dep_1", "eng_1", "mdl_1"),
                engine=_engine("eng_1", "e"),
                model=_model("mdl_1", "m"),
                payload={},
            )

    def test_the_mock_adapter_requires_an_object_payload(self) -> None:
        adapter = MockGatewayAdapter()
        with self.assertRaises(InvalidRequest):
            adapter.forward(
                endpoint=_endpoint("ep_1", "dep_1", visibility="users", status="ready"),
                deployment=_deployment("dep_1", "eng_1", "mdl_1"),
                engine=_engine("eng_1", "e"),
                model=_model("mdl_1", "m"),
                payload=["not", "an", "object"],  # type: ignore[arg-type]
            )

    def test_the_invoke_dto_declares_server_side_authorization(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="dave",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["data"]["gateway"]["authorization"], "server_side_enforced"
        )
        self.assertEqual(payload["data"]["metering"]["source"], "registry_audit")
        self.assertTrue(payload["data"]["metering"]["recorded"])

    def test_a_failing_gateway_surfaces_as_503_and_is_still_metered(self) -> None:
        self.h.gateway.failing = True
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "registry.upstream_unavailable")
        # 计量先于转发：一次被授权的调用即便上游失败，也已经记下了。
        _s, _h, usage = self.h.request(
            "GET", f"{PREFIX}/usage/mine", principal="alice"
        )
        self.assertEqual(
            usage["data"]["metrics"]["registry_recorded_invocations"]["value"], 1
        )


class EnvelopeShapeTest(unittest.TestCase):
    def test_an_envelope_requires_a_known_mode_and_an_explicit_source(self) -> None:
        with self.assertRaises(ValueError):
            dto.envelope({}, mode="production-ish", source="x")
        with self.assertRaises(ValueError):
            dto.envelope({}, mode=dto.MODE_SYNTHETIC, source="")

    def test_a_synthetic_envelope_says_so_in_the_hints(self) -> None:
        envelope = dto.envelope({}, mode=dto.MODE_SYNTHETIC, source="x")
        self.assertTrue(envelope["synthetic"])
        self.assertTrue(any("SYNTHETIC" in hint for hint in envelope["display_hints"]))

    def test_a_live_envelope_carries_no_synthetic_hint(self) -> None:
        envelope = dto.envelope({}, mode=dto.MODE_LIVE, source="x")
        self.assertFalse(envelope["synthetic"])
        self.assertEqual(envelope["display_hints"], [])

    def test_the_schema_version_is_pinned(self) -> None:
        self.assertEqual(dto.SCHEMA_VERSION, "registry/1")

    def test_the_not_configured_block_is_honest(self) -> None:
        block = dto.not_configured_block(what="usage")
        self.assertEqual(block["status"], dto.USAGE_NOT_CONFIGURED)
        self.assertIsNone(block["value"])
        self.assertEqual(block["state"], "unknown")
        self.assertIsNone(block["source"])

    def test_the_engine_view_records_whether_the_target_was_redacted(self) -> None:
        engine = _engine("eng_1", "e")
        revealed = dto.render_engine(engine, reveal_target=True)
        hidden = dto.render_engine(engine, reveal_target=False)
        self.assertFalse(revealed["target_redacted"])
        self.assertEqual(revealed["target"]["host"], "127.0.0.1")
        self.assertTrue(hidden["target_redacted"])
        self.assertIsNone(hidden["target"]["host"])
        self.assertIsNone(hidden["target"]["port"])

    def test_the_scope_block_separates_scopes_from_roles(self) -> None:
        block = dto.scope_block(
            principal="alice",
            uid=4001,
            scopes={"registry:read"},
            roles={"admin"},
            is_admin_read=False,
            is_admin_manage=False,
        )
        self.assertEqual(block["scopes"], ["registry:read"])
        self.assertEqual(block["roles"], ["admin"])
        self.assertFalse(block["is_admin_read"])


class DtoOverApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_every_response_declares_its_provenance(self) -> None:
        for method, path in (
            ("GET", f"{PREFIX}/whoami"),
            ("GET", f"{PREFIX}/engines"),
            ("GET", f"{PREFIX}/models"),
            ("GET", f"{PREFIX}/deployments"),
            ("GET", f"{PREFIX}/endpoints"),
            ("GET", f"{PREFIX}/usage/mine"),
            ("GET", f"{PREFIX}/status"),
            ("GET", f"{PREFIX}/audit"),
        ):
            with self.subTest(path=path):
                _s, _h, payload = self.h.request(method, path, principal="alice")
                self.assertEqual(payload["schema_version"], "registry/1")
                self.assertIsInstance(payload["synthetic"], bool)
                self.assertTrue(payload["source"])

    def test_the_usage_dto_never_fabricates_a_zero(self) -> None:
        _s, _h, payload = self.h.request("GET", f"{PREFIX}/usage/mine", principal="alice")
        metrics = payload["data"]["metrics"]
        for name in ("tokens", "latency_ms", "ttft_ms"):
            with self.subTest(metric=name):
                self.assertIsNone(metrics[name]["value"])
                self.assertEqual(metrics[name]["state"], "unknown")
                self.assertIsNone(metrics[name]["source"])
        # 唯一有来源的那条是 Registry 自己数出来的，且它**确实**是 0。
        measured = metrics["registry_recorded_invocations"]
        self.assertEqual(measured["value"], 0)
        self.assertEqual(measured["source"], "registry_audit")
        self.assertEqual(measured["state"], "healthy")

    def test_the_audit_dto_is_a_flat_serialisable_list(self) -> None:
        self.h.make_ready_endpoint("alice")
        _s, _h, payload = self.h.request("GET", f"{PREFIX}/audit", principal="alice")
        self.assertTrue(payload["data"]["items"])
        for entry in payload["data"]["items"]:
            self.assertEqual(
                set(entry),
                {"id", "at", "actor", "actor_uid", "action", "object_type", "object_id", "detail"},
            )
            json.dumps(entry)

    def test_the_endpoint_acl_is_only_visible_to_the_owner_and_admins(self) -> None:
        endpoint_id = self.h.make_ready_endpoint("alice")
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        _s, _h, owner = self.h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint_id}", principal="alice"
        )
        _s, _h, grantee = self.h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint_id}", principal="dave"
        )
        self.assertEqual(owner["data"]["acl"]["grantees"], ["dave"])
        # 受托人拿到 acl=None：共享模式的形状本身就是别人的授权信息。
        self.assertIsNone(grantee["data"]["acl"])

    def test_the_list_envelope_counts_match_the_items(self) -> None:
        self.h.make_ready_endpoint("alice")
        self.h.make_ready_endpoint("alice", name="svc2")
        _s, _h, payload = self.h.request("GET", f"{PREFIX}/endpoints", principal="alice")
        self.assertEqual(payload["data"]["count"], len(payload["data"]["items"]))
        self.assertEqual(payload["data"]["count"], 2)

    def test_the_envelope_marks_a_registered_endpoint_as_not_shareable(self) -> None:
        h = Harness(prober=None)
        engine_id = h.make_engine("alice", "e")
        model_id = h.make_model("alice", "m")
        _s, _h, deployment = h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )
        _s, _h, endpoint = h.request(
            "POST",
            f"{PREFIX}/endpoints",
            principal="alice",
            body={"name": "svc", "deployment_id": deployment["data"]["id"]},
        )
        data = endpoint["data"]
        self.assertTrue(data["registered"])
        self.assertFalse(data["ready"])
        self.assertFalse(data["shareable"])
        self.assertEqual(data["status"], "registered")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
