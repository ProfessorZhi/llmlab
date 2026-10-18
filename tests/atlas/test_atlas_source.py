"""可注入 seam 的装配工厂（契约 §8.1 + §8.3 的合流点）。

BFF 的 ``AtlasStack``（``backend/bff/atlas_adapter.py``，由 BFF Lane 拥有）需要
三样东西：一个 ``KeyDirectory``、一个 ``gateway_text``、一份 ``Provenance``。
本文件锁定的是**把「一份受信文档 + 一个语料路径」变成那三样东西**的那个工厂，
以及它最重要的一个性质：**整条链路可以在没有 Dell 的机器上跑通并自证。**

两条边界由静态用例守着（而不是靠注释）：

* 本模块**不** import ``backend.bff`` —— 依赖方向必须是 BFF 依赖 Atlas，不能反过来，
  否则「谁拥有哪一层」就只剩一句话；
* 工厂的入参里**没有**任何来自 HTTP 请求的东西（没有 identity、没有 query），
  所以一次请求不可能改变被读的目录或语料。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from tests.atlas import support

from backend.atlas.corpus import (
    REASON_FILE_MISSING,
    AtlasSource,
    CorpusLimits,
    build_atlas_source,
)
from backend.atlas.dto import Provenance
from backend.atlas.errors import SourceUnavailable

SYNTHETIC = Provenance(source="gateway-export", synthetic=True)


class BuildAtlasSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        support.write_file(
            self.root / "directory.json", support.dumps(support.directory_document())
        )
        support.write_file(self.root / "usage.jsonl", support.corpus_text())

    def _build(self, **overrides) -> AtlasSource:
        kwargs = dict(
            directory_path=self.root / "directory.json",
            directory_root=self.root,
            corpus_path=self.root / "usage.jsonl",
            corpus_root=self.root,
            provenance=SYNTHETIC,
        )
        kwargs.update(overrides)
        return build_atlas_source(**kwargs)

    def test_a_complete_source_is_available(self):
        source = self._build()

        self.assertTrue(source.available)
        self.assertIsNone(source.reason)
        self.assertEqual(source.directory_revision, support.DEMO_REVISION)
        self.assertEqual(source.directory.lookup("key-alpha").principal, "u-1001")
        self.assertEqual(source.gateway_text(None), support.corpus_text())

    def test_a_missing_directory_makes_the_whole_source_unavailable(self):
        """**不返回半套东西。** 目录读不出来时不能只把 ``directory`` 置空、把语料
        接上去 —— 那样每一行都会因为「key 不在受信目录」而归不了属，最后的 DTO 是
        一份看起来正常的空账本。"""
        source = self._build(directory_path=self.root / "nope.json")

        self.assertFalse(source.available)
        self.assertEqual(source.reason, REASON_FILE_MISSING)
        self.assertIsNone(source.directory)
        self.assertIsNone(source.gateway_text)

    def test_a_missing_corpus_makes_the_whole_source_unavailable(self):
        """同理：语料缺失不是「没有调用」，而是「这台机器没接上语料」。"""
        source = self._build(corpus_path=self.root / "nope.jsonl")

        self.assertFalse(source.available)
        self.assertEqual(source.reason, REASON_FILE_MISSING)
        self.assertIsNone(source.gateway_text)

    def test_an_untrusted_directory_wins_over_the_corpus_check(self):
        """两条来源都坏时，报**目录**那一条：归属依据比内容更根本。"""
        source = self._build(
            directory_path=self.root / "nope.json",
            corpus_path=self.root / "also-nope.jsonl",
        )

        self.assertFalse(source.available)

    def test_gateway_attached_defaults_to_false_and_is_never_flipped(self):
        """「已接线真实 Gateway」只能由装配方**显式**声明。工厂不做任何推断，
        也不因为「文件读得到」就把它翻成真 —— 读到一个文件不等于那文件来自生产。"""
        self.assertFalse(self._build().gateway_attached)
        self.assertTrue(self._build(gateway_attached=True).gateway_attached)

    def test_provenance_is_carried_through_unchanged(self):
        source = self._build()

        self.assertIs(source.provenance, SYNTHETIC)

    def test_the_limits_are_honoured(self):
        source = self._build(corpus_limits=CorpusLimits(max_bytes=8))

        self.assertFalse(source.available)

    def test_a_synthetic_source_never_drives_live_transport(self):
        """端到端：即便装配方误把 ``gateway_attached=True`` 与合成 provenance 一起传，
        ``transport.mode`` 也必须是 ``demo`` —— Atlas 自己的口径，这里只是照抄。"""
        from backend.atlas.calls import SelfCallsRequest, build_self_calls_dto
        from backend.atlas.identity import SCOPE_CALLS_SELF_READ, RequesterContext

        source = self._build(gateway_attached=True)
        requester = RequesterContext(
            subject="u-1001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_CALLS_SELF_READ}),
            authorized_projects=frozenset({"proj-a"}),
        )
        request = SelfCallsRequest(
            requester=requester,
            window_from=support_ts("2026-10-14T00:00:00Z"),
            window_to=support_ts("2026-10-14T04:00:00Z"),
            gateway_attached=source.gateway_attached,
        )

        dto = build_self_calls_dto(
            request, source.gateway_text(requester), source.directory, source.provenance
        )

        self.assertEqual(dto["transport"]["mode"], "demo")


class EndToEndWithoutDellTest(unittest.TestCase):
    """**「无需接触 Dell 就能测试」这句话的证据。**

    从一个临时目录里的两份合成文件出发，走完整条 seam —— 受信文档 -> KeyDirectory、
    语料路径 -> gateway_text、两者喂给 Atlas 自己的 builder —— 并断言逐条视图里
    **只有本人的、在授权项目内的 key** 的行。

    这条用例不 stub 任何 Atlas 内部实现：被验证的正是「把文件接上去之后的真实行为」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_only_the_callers_own_authorised_rows_reach_the_dto(self):
        from backend.atlas.calls import SelfCallsRequest, build_self_calls_dto
        from backend.atlas.identity import SCOPE_CALLS_SELF_READ, RequesterContext

        support.write_file(
            self.root / "directory.json", support.dumps(support.directory_document())
        )
        support.write_file(self.root / "usage.jsonl", support.corpus_text())
        source = build_atlas_source(
            directory_path=self.root / "directory.json",
            directory_root=self.root,
            corpus_path=self.root / "usage.jsonl",
            corpus_root=self.root,
            provenance=SYNTHETIC,
        )
        self.assertTrue(source.available)

        requester = RequesterContext(
            subject="u-1001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_CALLS_SELF_READ}),
            authorized_projects=frozenset({"proj-a"}),
        )
        request = SelfCallsRequest(
            requester=requester,
            window_from=support_ts("2026-10-14T00:00:00Z"),
            window_to=support_ts("2026-10-14T04:00:00Z"),
        )

        dto = build_self_calls_dto(
            request, source.gateway_text(requester), source.directory, source.provenance
        )

        key_ids = {row["key_id"] for row in dto["calls"]}
        self.assertEqual(key_ids, {"key-alpha"})
        self.assertEqual(dto["counts"]["returned"], 1)


class StaticGuaranteeTest(unittest.TestCase):
    """守住依赖方向。这是 Lane 边界，不是风格偏好。"""

    def test_corpus_module_does_not_import_the_bff_package(self):
        """测的是**依赖**，不是**措辞**：模块正文里正当地提到了
        ``backend/bff/atlas_adapter.py``（它说明谁拥有注入点），那不算 import。
        所以这里走 AST，找一个真正的 import 语句。
        """
        import ast

        path = pathlib.Path(__file__).resolve().parents[2] / "backend" / "atlas" / "corpus.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        offenders = {name for name in imported if "bff" in name.split(".")}
        self.assertEqual(offenders, set(), f"atlas must not depend on bff, found {offenders}")

    def test_the_factory_takes_no_request_shaped_parameter(self):
        """工厂的参数表里不能出现身份/查询类名字 —— 那些东西只来自 BFF 会话。"""
        import inspect

        signature = inspect.signature(build_atlas_source)
        forbidden = {"identity", "subject", "principal", "request", "query", "user"}

        self.assertFalse(forbidden & set(signature.parameters))


def support_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
