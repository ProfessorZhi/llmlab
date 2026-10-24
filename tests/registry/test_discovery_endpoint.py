"""``GET /discovery`` 的信封形状：网页「本机发现」完全靠它渲染。

这里测的是**对外契约**，不是扫描算法（那在 ``test_local_discovery.py``）：
未装配发现器、显式根目录、自动默认位置，三种状态下页面要拿到不同的东西。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.registry.support import Harness

from backend.registry.api import PREFIX
from backend.registry.local_discovery import DEFAULT_MODEL_ROOTS, LocalDiscovery
from backend.registry.ownership import ProcNetListenerTable

ADMIN = "carol"


def discovery_with(*, model_roots, root: Path | None = None) -> LocalDiscovery:
    return LocalDiscovery(
        allowed_ports=frozenset(),
        model_roots=model_roots,
        table=ProcNetListenerTable.from_texts(tcp=""),
        fetch=lambda port, path: None,
    )


class DiscoveryEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="llm-lab-discovery-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        model = self.root / "tiny-hf"
        model.mkdir()
        (model / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
        (model / "model.safetensors").write_bytes(b"x" * 42)

    def harness(self, discovery) -> Harness:
        harness = Harness(discovery=discovery)
        self.addCleanup(harness.close)
        return harness

    def scan(self, discovery):
        harness = self.harness(discovery)
        status, _headers, payload = harness.request(
            "GET", f"{PREFIX}/discovery", principal=ADMIN
        )
        self.assertEqual(status, 200, payload)
        return payload["data"]

    def test_a_deployment_without_a_discoverer_says_so_instead_of_returning_an_empty_list(self):
        data = self.scan(None)

        self.assertFalse(data["configured"])
        self.assertEqual(data["models"], [])
        self.assertEqual(data["engines"], [])
        self.assertEqual(data["installed_engines"], [])
        self.assertIn("discovery_not_configured", data["problems"])

    def test_explicit_roots_are_reported_as_configured_not_automatic(self):
        data = self.scan(discovery_with(model_roots=(str(self.root),)))

        self.assertTrue(data["configured"])
        self.assertFalse(data["model_roots_automatic"])
        self.assertEqual(data["model_roots"], [str(self.root)])
        self.assertEqual([item["format"] for item in data["models"]], ["hf"])
        self.assertEqual(data["models"][0]["architecture"], "LlamaForCausalLM")
        self.assertIn("installed_engines", data)

    def test_automatic_roots_are_announced_so_the_page_can_name_them(self):
        """页面要能写出「已查找（自动）：/data、/srv、…」，否则用户仍然不知道看的是哪儿。"""

        data = self.scan(discovery_with(model_roots=None))

        self.assertTrue(data["model_roots_automatic"])
        self.assertEqual(
            data["model_roots"],
            [str(Path(root).expanduser()) for root in DEFAULT_MODEL_ROOTS],
        )

    def test_a_missing_root_becomes_a_named_problem(self):
        missing = str(self.root / "nope")

        data = self.scan(discovery_with(model_roots=(missing,)))

        self.assertEqual(data["models"], [])
        self.assertEqual(data["problems"], [f"model_root_unavailable:{missing}"])


if __name__ == "__main__":
    unittest.main()
