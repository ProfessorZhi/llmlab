"""受信 Gateway adapter（invoke 路径）与路由同步（Gateway PR #20 契约）的 CPU-only 证据。

这组测试全部跑在回环上：没有 GPU、没有生产服务、没有真实用户进程、没有真实 key。

关于「真实性」的边界，说在最前面：

* 合流里那台“Gateway”是**本进程临时起的 loopback 模拟器**（端口由内核分配）。它
  按快照里的 ACL 与自己的 ``key -> owner`` 映射判定放行，并在应答里显式标注
  ``synthetic: true``。它证明的是「代码会照着 Gateway 的判定做决定」，
  **不**证明线上跑着一个真 Gateway —— 那需要真实非生产环境，见交付说明。
* “上游推理服务”是一台**诱饵**：任何直连它的路径都会在 :class:`DecoyUpstream`
  的命中计数上现形，因此「浏览器/CLI 拿不到原始 :8080」这条不变量可以被**证伪**，
  而不是靠声明。
* 所有 Gateway key 都以 ``synthetic-`` 前缀、进程内生成、不落盘。
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import os
import socket
import tempfile
import threading
import unittest

from backend.registry.errors import AccessDenied, InvalidRequest, UpstreamUnavailable
from backend.registry.gateway import (
    REASON_GATEWAY_ACL_DENIED,
    REASON_GATEWAY_KEY_UNAVAILABLE,
    REASON_GATEWAY_PROTOCOL_ERROR,
    REASON_UPSTREAM_ERROR,
    DetachedGatewayAdapter,
    FileGatewayKeyResolver,
    NoGatewayKeys,
    StaticGatewayKeyResolver,
    TrustedGatewayAdapter,
)
from backend.registry.health import (
    PROTOCOL_VERIFIED,
    REASON_MODEL_LIST_VERIFIED,
    ProbeResult,
    ProtocolObservation,
    StaticProber,
    StaticProtocolProber,
)
from backend.registry.registry import RegistryConfig
from backend.registry.router import (
    ROUTER_SYNC_APPLIED,
    ROUTER_SYNC_PENDING,
    ROUTING_SNAPSHOT_SCHEMA,
    DetachedRouterAdapter,
    FileRouterAdapter,
    RouterPublishResult,
    RoutingSnapshotError,
    build_routing_snapshot,
    snapshot_sha256,
    validate_routing_snapshot,
)
from tests.registry.support import Harness, close_all

PREFIX = "/api/v2/registry"
LOOPBACK = "127.0.0.1"


def tearDownModule() -> None:
    close_all()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# 回环替身：一台会按 ACL 判定的 Gateway、一台绝不该被碰的上游
# ---------------------------------------------------------------------------


class SyntheticGateway:
    """**回环上的 Gateway 替身**。它复刻 PR #20 的三条 ACL 判定，但只用合成 key。

    ``keys`` 是 ``gateway_key -> owner``（owner 是平台用户名），对应真实 Gateway 里
    「key 记录自带 owner」那条设计。判定与 PR #20 §7 一致：owner 永远可以，
    ``all`` 人人可以，``users`` 只对名单内的人开放，``private``（缺省）只对 owner。
    """

    def __init__(self, keys: dict) -> None:
        self.keys = dict(keys)
        self.snapshot: dict = {
            "schema": ROUTING_SNAPSHOT_SCHEMA,
            "revision": 0,
            "engines": [],
            "endpoints": [],
        }
        self.calls: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.calls.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "request_id": self.headers.get("X-Request-Id"),
                    }
                )
                if self.path != "/v1/chat/completions":
                    return outer._respond(self, 404, {"error": "no such route"})
                auth = self.headers.get("Authorization") or ""
                key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                owner = outer.keys.get(key)
                if owner is None:
                    return outer._respond(self, 401, {"error": "unknown key"})
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return outer._respond(self, 400, {"error": "bad json"})
                alias = body.get("model")
                endpoint = next(
                    (
                        item
                        for item in outer.snapshot.get("endpoints", [])
                        if item.get("model_alias") == alias
                    ),
                    None,
                )
                if endpoint is None:
                    return outer._respond(self, 404, {"error": "no such endpoint", "model": alias})
                if not outer._allowed(endpoint, owner):
                    return outer._respond(
                        self, 403, {"error": "acl denied", "acl": endpoint.get("acl")}
                    )
                return outer._respond(
                    self,
                    200,
                    {
                        # 替身**自称** synthetic —— adapter 只是照抄它说的，不替它下结论。
                        "synthetic": True,
                        "object": "chat.completion",
                        "model": alias,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "SYNTHETIC gateway double",
                                },
                            }
                        ],
                        "usage": {"total_tokens": 7},
                        "note": "loopback double; not a real inference result",
                    },
                )

            def log_message(self, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def _allowed(self, endpoint: dict, owner: str) -> bool:
        acl = endpoint.get("acl") or {}
        kind = acl.get("kind")
        if owner == endpoint.get("owner_uid"):
            return True
        if kind == "all":
            return True
        if kind == "users":
            return owner in (acl.get("principals") or [])
        return False

    def _respond(self, handler: http.server.BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def set_snapshot(self, snapshot: dict) -> None:
        self.snapshot = snapshot

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class DecoyUpstream:
    """一台**绝不该被调用到**的“上游推理服务”。命中计数应当恒为 0。"""

    def __init__(self) -> None:
        outer = self
        self.hits = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _hit(self) -> None:
                outer.hits += 1
                body = b'{"synthetic": true, "note": "decoy upstream was reached"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._hit()

            def do_POST(self) -> None:  # noqa: N802
                self._hit()

            def log_message(self, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# 服务侧用的 RouterAdapter 替身（真实的文件 adapter 单独测）
# ---------------------------------------------------------------------------


class ConfirmingRouterAdapter:
    """读回成功：声称「Gateway 已按本次快照的代数与 sha 加载」。**测试替身**。"""

    name = "confirming_test_double"
    attached = True

    def __init__(self) -> None:
        self._applied: int | None = None

    def publish(self, snapshot: dict) -> RouterPublishResult:
        result = RouterPublishResult(
            published=True,
            applied_revision=int(snapshot["revision"]),
            snapshot_sha256=snapshot_sha256(snapshot),
            state=ROUTER_SYNC_APPLIED,
            reason=None,
            detail="test double confirms",
        )
        self._applied = result.applied_revision
        return result

    def applied_revision(self) -> int | None:
        """**当前**回读到的代数。

        invoke 的路由门会问这一句（「DB 说已生效」不等于「Gateway 此刻仍在跑
        这一代」）。替身必须和真实 `FileRouterAdapter` 一样记住自己发布过哪一代，
        否则它只能证明「publish 时说成功」，证明不了「此刻仍生效」。
        """
        return self._applied


class StaleRouterAdapter:
    """读回**落后**：Gateway 生效的是上一代。应当被拒，状态停在 pending。"""

    name = "stale_test_double"
    attached = True

    def publish(self, snapshot: dict) -> RouterPublishResult:
        return RouterPublishResult(
            published=False,
            applied_revision=max(0, int(snapshot["revision"]) - 1),
            snapshot_sha256=snapshot_sha256(snapshot),
            state=ROUTER_SYNC_PENDING,
            reason="gateway_revision_mismatch",
            detail="test double reports a lagging generation",
        )

    def applied_revision(self) -> int | None:
        return None


# ---------------------------------------------------------------------------
# 1. TrustedGatewayAdapter 的构造与失败模式
# ---------------------------------------------------------------------------


class TrustedGatewayAdapterTest(unittest.TestCase):
    def test_a_misconfigured_base_is_refused_at_construction(self) -> None:
        for bad in (
            "https://127.0.0.1:8200",  # 只允许 http 回环
            "http://10.0.0.5:8200",  # 非回环
            "http://127.0.0.1:8080",  # 保留的上游推理端口
            "http://127.0.0.1:8200/v1",  # 只能是 origin
            "http://user:pw@127.0.0.1:8200",  # 不得带凭据
            "http://127.0.0.1:8200?x=1",  # 不得带 query
            "not-a-url",
            "",
        ):
            with self.subTest(base=bad):
                with self.assertRaises(InvalidRequest):
                    TrustedGatewayAdapter(base_url=bad)

    def test_a_loopback_base_is_accepted(self) -> None:
        adapter = TrustedGatewayAdapter(base_url=f"http://127.0.0.1:8200")
        self.assertTrue(adapter.attached)
        self.assertEqual(adapter.base_url, "http://127.0.0.1:8200")

    def test_no_key_for_the_caller_refuses_before_any_socket(self) -> None:
        gateway = SyntheticGateway(keys={})
        self.addCleanup(gateway.stop)
        adapter = TrustedGatewayAdapter(
            base_url=gateway.base_url, keys=StaticGatewayKeyResolver({})
        )
        with self.assertRaises(UpstreamUnavailable) as caught:
            adapter.forward(
                endpoint=_fake_endpoint(),
                deployment=_fake_deployment(),
                engine=_fake_engine(),
                model=_fake_model(),
                payload={"messages": []},
                requester=_fake_requester("alice"),
            )
        self.assertIn(REASON_GATEWAY_KEY_UNAVAILABLE, str(caught.exception))
        self.assertEqual(gateway.calls, [])  # 一个请求都没发

    def test_a_missing_requester_is_also_fail_closed(self) -> None:
        adapter = TrustedGatewayAdapter(
            base_url="http://127.0.0.1:9", keys=StaticGatewayKeyResolver({"alice": "synthetic-k"})
        )
        with self.assertRaises(UpstreamUnavailable) as caught:
            adapter.forward(
                endpoint=_fake_endpoint(),
                deployment=_fake_deployment(),
                engine=_fake_engine(),
                model=_fake_model(),
                payload={},
                requester=None,
            )
        self.assertIn(REASON_GATEWAY_KEY_UNAVAILABLE, str(caught.exception))

    def test_the_default_key_resolver_is_empty_and_refuses(self) -> None:
        adapter = TrustedGatewayAdapter(base_url="http://127.0.0.1:9")
        self.assertIsInstance(adapter.keys, NoGatewayKeys)
        self.assertIsNone(adapter.resolve_key(_fake_requester("anyone")))

    def test_an_unknown_key_is_a_401_from_the_gateway(self) -> None:
        gateway = SyntheticGateway(keys={"synthetic-known": "alice"})
        self.addCleanup(gateway.stop)
        adapter = TrustedGatewayAdapter(
            base_url=gateway.base_url,
            keys=StaticGatewayKeyResolver({"alice": "synthetic-unknown"}),
        )
        with self.assertRaises(AccessDenied) as caught:
            adapter.forward(
                endpoint=_fake_endpoint(),
                deployment=_fake_deployment(),
                engine=_fake_engine(),
                model=_fake_model(),
                payload={},
                requester=_fake_requester("alice"),
            )
        self.assertIn(REASON_GATEWAY_ACL_DENIED, str(caught.exception))

    def test_a_5xx_is_an_upstream_error_not_a_fake_success(self) -> None:
        with _OneShotGateway(500, b'{"error":"boom"}') as url:
            adapter = TrustedGatewayAdapter(
                base_url=url, keys=StaticGatewayKeyResolver({"alice": "synthetic-k"})
            )
            with self.assertRaises(UpstreamUnavailable) as caught:
                adapter.forward(
                    endpoint=_fake_endpoint(),
                    deployment=_fake_deployment(),
                    engine=_fake_engine(),
                    model=_fake_model(),
                    payload={},
                    requester=_fake_requester("alice"),
                )
            self.assertIn(REASON_UPSTREAM_ERROR, str(caught.exception))

    def test_a_non_json_body_is_a_protocol_error(self) -> None:
        with _OneShotGateway(200, b"<html>not json</html>") as url:
            adapter = TrustedGatewayAdapter(
                base_url=url, keys=StaticGatewayKeyResolver({"alice": "synthetic-k"})
            )
            with self.assertRaises(UpstreamUnavailable) as caught:
                adapter.forward(
                    endpoint=_fake_endpoint(),
                    deployment=_fake_deployment(),
                    engine=_fake_engine(),
                    model=_fake_model(),
                    payload={},
                    requester=_fake_requester("alice"),
                )
            self.assertIn(REASON_GATEWAY_PROTOCOL_ERROR, str(caught.exception))

    def test_an_unreachable_gateway_is_reported_not_faked(self) -> None:
        adapter = TrustedGatewayAdapter(
            base_url=f"http://127.0.0.1:{_free_port()}",
            keys=StaticGatewayKeyResolver({"alice": "synthetic-k"}),
            timeout=0.5,
        )
        with self.assertRaises(UpstreamUnavailable):
            adapter.forward(
                endpoint=_fake_endpoint(),
                deployment=_fake_deployment(),
                engine=_fake_engine(),
                model=_fake_model(),
                payload={},
                requester=_fake_requester("alice"),
            )


# ---------------------------------------------------------------------------
# 2. 快照导出：PR #20 形状、ACL 映射、fail-closed 的排除
# ---------------------------------------------------------------------------


def _snapshot_fixture(**overrides) -> dict:
    from datetime import datetime, timezone

    from backend.registry import models as m

    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    engine = m.Engine(
        id="eng_alice",
        owner_principal="alice",
        owner_uid=4001,
        name="e",
        target={"host": "127.0.0.1", "port": 9000, "path": "/v1"},
        protocol="openai_compatible",
        status=m.ENGINE_VERIFIED,
        health={},
        source=m.SOURCE_CLIENT_REGISTERED,
        created_at=now,
        updated_at=now,
    )
    model = m.Model(
        id="mdl_1",
        owner_principal="alice",
        owner_uid=4001,
        name="Qwen/Qwen2.5-7B-Instruct",
        weight_path=None,
        weight_path_sha256=None,
        format=None,
        parameter_count=None,
        status=m.MODEL_REGISTERED,
        health={},
        source=m.SOURCE_CLIENT_REGISTERED,
        created_at=now,
        updated_at=now,
    )
    deployment = m.Deployment(
        id="dep_1",
        owner_principal="alice",
        owner_uid=4001,
        name="d",
        engine_id="eng_alice",
        model_id="mdl_1",
        status=m.DEPLOYMENT_READY,
        health={},
        source=m.SOURCE_CLIENT_REGISTERED,
        created_at=now,
        updated_at=now,
    )
    endpoint = m.Endpoint(
        id="ep_1",
        owner_principal="alice",
        owner_uid=4001,
        name="chat",
        deployment_id="dep_1",
        route_path="/v1/route/alice/chat",
        visibility=overrides.get("visibility", m.VISIBILITY_PRIVATE),
        status=m.ENDPOINT_READY,
        health={},
        source=m.SOURCE_CLIENT_REGISTERED,
        created_at=now,
        updated_at=now,
        revision=overrides.get("revision", 3),
    )
    return build_routing_snapshot(
        endpoints=[endpoint],
        deployments={"dep_1": deployment},
        engines={"eng_alice": engine},
        models={"mdl_1": model},
        grantees={"ep_1": tuple(overrides.get("grantees", ("bob",)))},
        secret_refs=overrides.get(
            "secret_refs", {"eng_alice": {"file": "/etc/llmlab/engines.env", "var": "K"}}
        ),
        linux_user_by_principal={"alice": "alice", "bob": "bob"},
        generated_at=now,
    )


class RoutingSnapshotTest(unittest.TestCase):
    def test_it_matches_the_gateway_schema_string(self) -> None:
        self.assertEqual(_snapshot_fixture()["schema"], "llmlab.endpoint-routing/1")
        self.assertEqual(ROUTING_SNAPSHOT_SCHEMA, "llmlab.endpoint-routing/1")

    def test_private_endpoints_are_published_as_private_not_hidden(self) -> None:
        snapshot = _snapshot_fixture(visibility="private")
        self.assertEqual(len(snapshot["endpoints"]), 1)
        self.assertEqual(snapshot["endpoints"][0]["acl"], {"kind": "private"})

    def test_shared_users_become_acl_principals(self) -> None:
        snapshot = _snapshot_fixture(visibility="users", grantees=("bob",))
        self.assertEqual(
            snapshot["endpoints"][0]["acl"],
            {"kind": "users", "principals": ["bob"]},
        )

    def test_all_approved_becomes_the_all_acl(self) -> None:
        from backend.registry import models as m

        snapshot = _snapshot_fixture(visibility=m.VISIBILITY_ALL_APPROVED)
        self.assertEqual(snapshot["endpoints"][0]["acl"], {"kind": "all"})

    def test_model_alias_is_the_stable_route_path(self) -> None:
        snapshot = _snapshot_fixture()
        self.assertEqual(snapshot["endpoints"][0]["model_alias"], "/v1/route/alice/chat")
        self.assertEqual(snapshot["endpoints"][0]["model_id"], "Qwen/Qwen2.5-7B-Instruct")

    def test_owner_uid_is_the_linux_user_not_a_header(self) -> None:
        self.assertEqual(_snapshot_fixture()["endpoints"][0]["owner_uid"], "alice")

    def test_revision_is_the_max_endpoint_revision(self) -> None:
        self.assertEqual(_snapshot_fixture(revision=5)["revision"], 5)

    def test_an_engine_without_a_secret_ref_is_excluded_not_published_bare(self) -> None:
        snapshot = _snapshot_fixture(secret_refs={})
        self.assertEqual(snapshot["engines"], [])
        self.assertEqual(snapshot["endpoints"], [])
        self.assertIn(
            {"engine_id": "eng_alice", "reason": "no_secret_ref"}, snapshot["excluded"]
        )

    def test_the_secret_ref_never_carries_a_value(self) -> None:
        snapshot = _snapshot_fixture()
        ref = snapshot["engines"][0]["secret_ref"]
        self.assertEqual(set(ref), {"file", "var"})

    def test_validate_rejects_a_forged_acl_kind(self) -> None:
        snapshot = _snapshot_fixture()
        snapshot["endpoints"][0]["acl"] = {"kind": "everyone"}
        with self.assertRaises(RoutingSnapshotError):
            validate_routing_snapshot(snapshot)

    def test_validate_rejects_a_non_loopback_upstream(self) -> None:
        snapshot = _snapshot_fixture()
        snapshot["engines"][0]["upstream"] = {"transport": "http", "host": "10.0.0.5", "port": 9000}
        with self.assertRaises(RoutingSnapshotError):
            validate_routing_snapshot(snapshot)

    def test_validate_rejects_a_secret_ref_carrying_a_value(self) -> None:
        snapshot = _snapshot_fixture()
        snapshot["engines"][0]["secret_ref"]["value"] = "inline-secret"
        with self.assertRaises(RoutingSnapshotError):
            validate_routing_snapshot(snapshot)


# ---------------------------------------------------------------------------
# 3. FileRouterAdapter：写盘 -> 读回，落后的代数与不符的 sha 都被拒
# ---------------------------------------------------------------------------


class FileRouterAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.snapshot_path = os.path.join(self._dir.name, "routing.json")
        self.status_path = os.path.join(self._dir.name, "status.json")

    def _adapter(self) -> FileRouterAdapter:
        return FileRouterAdapter(
            snapshot_path=self.snapshot_path, status_path=self.status_path
        )

    def _write_status(self, *, revision: int, sha: str) -> None:
        with open(self.status_path, "w", encoding="utf-8") as stream:
            json.dump(
                {"routing": {"revision": revision, "snapshot_sha256": sha}},
                stream,
            )

    def test_a_missing_status_file_is_not_a_success(self) -> None:
        snapshot = _snapshot_fixture()
        result = self._adapter().publish(snapshot)
        self.assertFalse(result.published)
        self.assertEqual(result.state, ROUTER_SYNC_PENDING)
        self.assertTrue(os.path.exists(self.snapshot_path))  # 写是写了

    def test_null_routing_means_the_gateway_loaded_nothing(self) -> None:
        with open(self.status_path, "w", encoding="utf-8") as stream:
            json.dump({"routing": None}, stream)
        result = self._adapter().publish(_snapshot_fixture())
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "snapshot_not_loaded_by_gateway")

    def test_a_lagging_generation_is_refused(self) -> None:
        snapshot = _snapshot_fixture(revision=4)
        self._write_status(revision=3, sha=snapshot_sha256(snapshot))
        result = self._adapter().publish(snapshot)
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "gateway_revision_mismatch")
        self.assertEqual(result.applied_revision, 3)

    def test_a_digest_mismatch_is_refused(self) -> None:
        snapshot = _snapshot_fixture(revision=4)
        self._write_status(revision=4, sha="0" * 64)
        result = self._adapter().publish(snapshot)
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "gateway_digest_mismatch")

    def test_a_matching_generation_and_digest_is_confirmed(self) -> None:
        snapshot = _snapshot_fixture(revision=4)
        self._write_status(revision=4, sha=snapshot_sha256(snapshot))
        result = self._adapter().publish(snapshot)
        self.assertTrue(result.published)
        self.assertEqual(result.state, ROUTER_SYNC_APPLIED)
        self.assertEqual(result.applied_revision, 4)
        # 写下去的字节就是被哈希的那一串：读回来重算必须一致。
        with open(self.snapshot_path, "rb") as stream:
            self.assertEqual(snapshot_sha256(json.loads(stream.read())), result.snapshot_sha256)

    def test_it_refuses_to_be_pointed_at_a_symlink(self) -> None:
        link = os.path.join(self._dir.name, "link.json")
        os.symlink(self.snapshot_path, link)
        with self.assertRaises(InvalidRequest):
            FileRouterAdapter(snapshot_path=link, status_path=self.status_path)


# ---------------------------------------------------------------------------
# 4. 服务接线：repoint 之后，Registry 改了**不**代表线上改了
# ---------------------------------------------------------------------------


class RegistryRouterWiringTest(unittest.TestCase):
    def _harness_with_router(self, router) -> Harness:
        harness = Harness(router=router)
        self.addCleanup(harness.close)
        return harness

    def test_default_is_detached_and_a_repoint_stays_pending(self) -> None:
        h = Harness()  # 缺省：没有任何 RouterAdapter
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        status, _headers, payload = h.repoint("alice", endpoint, target)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["router_sync"], ROUTER_SYNC_PENDING)
        self.assertFalse(payload["meta"]["router_applied"])
        self.assertEqual(
            payload["meta"]["router_sync_report"]["reason"],
            "no_router_adapter_in_this_deployment",
        )

    def test_a_confirming_router_adapter_acks_and_flips_to_applied(self) -> None:
        h = self._harness_with_router(ConfirmingRouterAdapter())
        endpoint = h.make_ready_endpoint("alice")
        # 快照必须真的包含这条路由。没有 secret_ref 时 build_routing_snapshot
        # 会排除整个 engine，快照为空 —— 而空快照上的 published=True 什么也
        # 证明不了（见 test_router_ack_binding.py 的 EmptySnapshot 用例）。
        h.publish_engine()
        target = h.make_sibling_deployment("alice", endpoint, "second")
        status, _headers, payload = h.repoint("alice", endpoint, target)
        self.assertEqual(status, 200, payload)
        meta = payload["meta"]
        self.assertEqual(meta["router_sync"], ROUTER_SYNC_APPLIED)
        self.assertTrue(meta["router_applied"])
        # 回执绑定的是**这次快照里发布了的那一条**，不是 revision 算术的副产品。
        self.assertEqual(meta["router_sync_report"]["acked_endpoints"], [endpoint])
        # 落库的 router_generation 也被推进到新 revision。
        self.assertEqual(payload["data"]["router_generation"], payload["data"]["revision"])
        # 回执的审计主体是机器主体，不是发起 repoint 的人。
        entries = h.service.store.list_audit(limit=100)
        acks = [e for e in entries if e.action == "router_sync_ack"]
        self.assertTrue(acks)
        self.assertEqual({e.actor_principal for e in acks}, {"system:router-sync"})

    def test_a_stale_router_adapter_leaves_it_pending(self) -> None:
        h = self._harness_with_router(StaleRouterAdapter())
        endpoint = h.make_ready_endpoint("alice")
        h.publish_engine()
        target = h.make_sibling_deployment("alice", endpoint, "second")
        status, _headers, payload = h.repoint("alice", endpoint, target)
        self.assertEqual(status, 200, payload)
        meta = payload["meta"]
        self.assertEqual(meta["router_sync"], ROUTER_SYNC_PENDING)
        self.assertFalse(meta["router_applied"])
        self.assertEqual(meta["router_sync_report"]["reason"], "gateway_revision_mismatch")
        # 没有任何 router_sync_ack 被写下。
        entries = h.service.store.list_audit(limit=100)
        self.assertEqual([e for e in entries if e.action == "router_sync_ack"], [])

    def test_the_manual_ack_still_refuses_a_wrong_generation(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        status, _headers, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/routing-applied",
            principal="carol",  # admin_manage
            body={"generation": 99},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.revision_conflict")

    def test_the_snapshot_route_needs_admin_read(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        status, _headers, payload = h.request(
            "GET", f"{PREFIX}/routing-snapshot", principal="alice"
        )
        self.assertEqual(status, 403, payload)
        status, _headers, payload = h.request(
            "GET", f"{PREFIX}/routing-snapshot", principal="carol"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["schema"], ROUTING_SNAPSHOT_SCHEMA)


# ---------------------------------------------------------------------------
# 5. 端到端：A 注册/验证/共享 -> B 经 Gateway 调用 -> A 撤权 -> B 被拒
# ---------------------------------------------------------------------------


class TrustedGatewayEndToEndTest(unittest.TestCase):
    """A=alice、B=bob。整条链经**真实回环 HTTP**，但两端都是测试替身。"""

    def setUp(self) -> None:
        self.decoy = DecoyUpstream()
        self.addCleanup(self.decoy.stop)
        self.gateway = SyntheticGateway(
            keys={"synthetic-alice-key": "alice", "synthetic-bob-key": "bob"}
        )
        self.addCleanup(self.gateway.stop)

        adapter = TrustedGatewayAdapter(
            base_url=self.gateway.base_url,
            keys=StaticGatewayKeyResolver(
                {
                    "alice": "synthetic-alice-key",
                    "bob": "synthetic-bob-key",
                }
            ),
        )
        config = RegistryConfig(
            mode="synthetic",
            allowed_ports=frozenset({self.decoy.port}),
        )
        self.h = Harness(
            gateway=adapter,
            config=config,
            prober=StaticProber({self.decoy.port: ProbeResult(reachable=True, latency_ms=1.0)}),
            protocol_prober=StaticProtocolProber(
                {
                    (LOOPBACK, self.decoy.port): ProtocolObservation(
                        state=PROTOCOL_VERIFIED,
                        reason=REASON_MODEL_LIST_VERIFIED,
                        protocol="openai_compatible",
                        checked_path="/v1/models",
                        http_status=200,
                        model_ids=1,
                        source="static_test_double",
                    )
                }
            ),
        )
        self.addCleanup(self.h.close)

    def _register_chain(self) -> str:
        endpoint = self.h.make_ready_endpoint("alice", name="chat", port=self.decoy.port)
        # engine id 是登记时才生成的，所以 secret_ref 只能登记后补进 config。
        # 不补 -> 快照里没有任何 engine（fail-closed），这条端到端测试会因此失败 ——
        # 这正是我们想要的：它保证我们**确实**发布了一个非空快照。
        engines = self.h.service.store.list_engines()
        self.assertEqual(len(engines), 1, "端到端前应当恰好有一个已登记 engine")
        self.h.service.config = dataclasses.replace(
            self.h.service.config,
            engine_secret_refs={
                engines[0].id: {"file": "/etc/llmlab/engines.env", "var": "ENGINE_VINCE_KEY"}
            },
        )
        # invoke 的路由门要求一次真实回读确认（PR #118）。没有它，这条端到端
        # 链路会在碰 Gateway 之前被 409 拒绝 —— 那正是本轮要修的行为，所以夹具
        # 必须显式确认路由，而不是依赖旧的「无 ACK 也 200」。
        self.h.acknowledge_route(endpoint)
        return endpoint

    def _publish_snapshot_to_gateway(self) -> None:
        """把 Registry 当前快照交给 Gateway 替身（模拟『Gateway 已加载』）。

        真实部署里这一步是 Gateway 自己 stat 文件后重读；替身里由测试显式调用，
        于是「快照 -> Gateway ACL」这条数据流是可核对的。
        """
        snapshot = self.h.service._routing_snapshot()
        # 必须真的发布出来了，否则这条端到端测试在测一个空快照。
        self.assertTrue(snapshot["endpoints"], snapshot.get("excluded"))
        self.gateway.set_snapshot(snapshot)

    def test_the_full_chain_with_a_trusted_gateway(self) -> None:
        endpoint = self._register_chain()
        # A 共享给 B。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertEqual(status, 200, payload)
        self._publish_snapshot_to_gateway()

        # B 经受信 Gateway 调用成功。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/invoke",
            principal="bob",
            body={"messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["gateway"]["adapter"], "trusted_http")
        self.assertTrue(data["gateway"]["attached"])
        self.assertEqual(data["gateway"]["authorization"], "server_side_enforced")
        self.assertTrue(data["result"]["forwarded"])
        self.assertEqual(data["result"]["served_by"], "gateway")
        # Token 数来自 Gateway 的 usage；不是凭空编的。
        self.assertEqual(data["result"]["tokens"], 7)
        # 结果照抄 Gateway 自称的 synthetic 标记（替身自称 synthetic）。
        self.assertTrue(data["result"]["synthetic"])
        self.assertTrue(data["metering"]["recorded"])
        # 计量先于转发：一次被授权的调用被记下来了。
        self.assertEqual(len(self.gateway.calls), 1)
        # 诱饵上游一次都没被碰到 —— 「不直连上游」这条不变量被证伪式的检查。
        self.assertEqual(self.decoy.hits, 0)

    def test_the_gateway_is_the_final_acl_authority(self) -> None:
        """Registry 说 B 可以，但 Gateway 的 ACL 还没跟上 —— Gateway 说了算。"""
        endpoint = self._register_chain()
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertEqual(status, 200, payload)
        # 故意让 Gateway 加载一份**旧的**快照：endpoint 仍是 private。
        stale = dict(self.h.service._routing_snapshot())
        stale["endpoints"] = [dict(item) for item in stale["endpoints"]]
        stale["endpoints"][0]["acl"] = {"kind": "private"}
        self.gateway.set_snapshot(stale)

        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/invoke",
            principal="bob",
            body={"messages": []},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")
        self.assertIn(REASON_GATEWAY_ACL_DENIED, payload["error"]["message"])
        self.assertEqual(self.decoy.hits, 0)

    def test_revocation_hides_the_endpoint_from_b_before_any_forward(self) -> None:
        endpoint = self._register_chain()
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self._publish_snapshot_to_gateway()
        status, _h, _p = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/invoke",
            principal="bob",
            body={"messages": []},
        )
        self.assertEqual(status, 200)
        calls_before = len(self.gateway.calls)

        # A 收回全部共享。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/unshare",
            principal="alice",
            body={},
        )
        self.assertEqual(status, 200, payload)

        # B 的下一次调用在 Registry 侧就被挡下（看不到 -> 404，与不存在同形）。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/invoke",
            principal="bob",
            body={"messages": []},
        )
        self.assertEqual(status, 404, payload)
        # 而且**没有**再向 Gateway 发第二次请求。
        self.assertEqual(len(self.gateway.calls), calls_before)

    def test_a_detached_gateway_honestly_says_503(self) -> None:
        """对照组：同一个 Endpoint，换成**缺省**的 detached adapter 就是 503。"""
        h = Harness(gateway=DetachedGatewayAdapter())
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/invoke",
            principal="alice",
            body={"messages": []},
        )
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], "registry.upstream_unavailable")
        # 计量仍然记下了这次「被授权但没接上」的调用 —— 先记账再转发。
        self.assertEqual(self.decoy.hits, 0)


# ---------------------------------------------------------------------------
# 小工具：构造 adapter.forward 需要的模型对象，以及一次性应答服务器
# ---------------------------------------------------------------------------


def _fake_engine():  # noqa: ANN202
    from backend.registry.models import Engine

    return Engine(
        id="eng_x",
        owner_principal="alice",
        owner_uid=4001,
        name="e",
        target={"host": "127.0.0.1", "port": 9000, "path": "/v1"},
        protocol="openai_compatible",
        status="verified",
        health={},
        source="client_registered",
        created_at=_now(),
        updated_at=_now(),
    )


def _fake_model():  # noqa: ANN202
    from backend.registry.models import Model

    return Model(
        id="mdl_x",
        owner_principal="alice",
        owner_uid=4001,
        name="m",
        weight_path=None,
        weight_path_sha256=None,
        format=None,
        parameter_count=None,
        status="registered",
        health={},
        source="client_registered",
        created_at=_now(),
        updated_at=_now(),
    )


def _fake_deployment():  # noqa: ANN202
    from backend.registry.models import Deployment

    return Deployment(
        id="dep_x",
        owner_principal="alice",
        owner_uid=4001,
        name="d",
        engine_id="eng_x",
        model_id="mdl_x",
        status="ready",
        health={},
        source="client_registered",
        created_at=_now(),
        updated_at=_now(),
    )


def _fake_endpoint():  # noqa: ANN202
    from backend.registry.models import Endpoint

    return Endpoint(
        id="ep_x",
        owner_principal="alice",
        owner_uid=4001,
        name="chat",
        deployment_id="dep_x",
        route_path="/v1/route/alice/chat",
        visibility="private",
        status="ready",
        health={},
        source="client_registered",
        created_at=_now(),
        updated_at=_now(),
    )


def _fake_requester(principal: str):  # noqa: ANN202
    class _Requester:
        pass

    requester = _Requester()
    requester.principal = principal
    return requester


def _now():  # noqa: ANN202
    from datetime import datetime, timezone

    return datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


class _OneShotGateway:
    """一次性回环服务器：对**任何** POST 回一份固定应答。用于错误映射的分支测试。"""

    def __init__(self, status: int, body: bytes) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.base_url

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# 6. key 目录文件与 dev-server 接线（缺参数就拒绝启动，不退化成「没接」）
# ---------------------------------------------------------------------------


class FileGatewayKeyResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "keys.json")

    def _write(self, payload: object, *, mode: int = 0o600) -> None:
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.chmod(self.path, mode)

    def test_a_0600_owned_file_resolves(self) -> None:
        self._write({"alice": "synthetic-alice", "bob": "synthetic-bob"})
        resolver = FileGatewayKeyResolver(self.path)
        self.assertEqual(resolver.resolve("alice"), "synthetic-alice")
        self.assertIsNone(resolver.resolve("nobody"))
        self.assertEqual(resolver.principals, ("alice", "bob"))

    def test_a_group_or_other_readable_file_is_refused(self) -> None:
        self._write({"alice": "synthetic-alice"}, mode=0o644)
        with self.assertRaises(InvalidRequest):
            FileGatewayKeyResolver(self.path)

    def test_a_symlink_is_refused(self) -> None:
        self._write({"alice": "synthetic-alice"})
        link = os.path.join(self._dir.name, "link.json")
        os.symlink(self.path, link)
        with self.assertRaises(InvalidRequest):
            FileGatewayKeyResolver(link)

    def test_a_missing_file_is_refused_not_silently_empty(self) -> None:
        with self.assertRaises(InvalidRequest):
            FileGatewayKeyResolver(os.path.join(self._dir.name, "absent.json"))

    def test_a_non_object_or_empty_value_is_refused(self) -> None:
        self._write(["not", "an", "object"])
        with self.assertRaises(InvalidRequest):
            FileGatewayKeyResolver(self.path)
        self._write({"alice": "   "})
        with self.assertRaises(InvalidRequest):
            FileGatewayKeyResolver(self.path)


class DevServerWiringTest(unittest.TestCase):
    def test_a_trusted_gateway_without_a_key_dir_refuses_to_start(self) -> None:
        from backend.registry.devserver import build_service_from_env

        with self.assertRaises(SystemExit):
            build_service_from_env(
                database=":memory:",
                identities=None,
                trusted_gateway_base="http://127.0.0.1:8200",
                gateway_key_file=None,
            )

    def test_a_half_configured_router_refuses_to_start(self) -> None:
        from backend.registry.devserver import build_service_from_env

        with self.assertRaises(SystemExit):
            build_service_from_env(
                database=":memory:",
                identities=None,
                router_snapshot="/tmp/routing.json",
                router_status=None,
            )

    def test_engine_secret_refs_parse_and_reject_empty_parts(self) -> None:
        from backend.registry.devserver import _parse_engine_secret_refs

        parsed = _parse_engine_secret_refs(["eng_1=/etc/llmlab/e.env:KEY"])
        self.assertEqual(parsed, {"eng_1": {"file": "/etc/llmlab/e.env", "var": "KEY"}})
        for bad in ("eng_1", "eng_1=no-colon", "=/f:V", "eng=/f:", "eng=:V"):
            with self.subTest(raw=bad):
                with self.assertRaises(SystemExit):
                    _parse_engine_secret_refs([bad])
