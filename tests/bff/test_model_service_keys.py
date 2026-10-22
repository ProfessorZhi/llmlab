"""平台签发的模型推理密钥：仓库安全属性 + 两条路由的门。

这一支是 BFF 里**唯一**把凭据原样下发给前端的来源，所以断言的重点不是
「能不能发」，而是它周围的四条边界：

1. **只追加、不覆盖。** 引擎环境文件里的第一条是网关自己在用的 key；把它冲掉
   等于在不相关的组件里制造一次故障。签发只能往后加。
2. **只对平台管理员开放。** 成员读到的是 403，不是一份掩码清单。
3. **写要过 CSRF/同源。** 签发是一次写入，跨站 POST 不能借着浏览器里的会话
   把密钥发出去。
4. **「已签发」不等于「已生效」。** 引擎只在启动时读一次环境文件，响应里的
   ``restart`` 必须如实反映重载有没有真的被触发。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.bff.app import MODEL_SERVICE_KEY_PATH
from backend.bff.model_service_keys import DEFAULT_ENDPOINT, ModelServiceKeyStore
from tests.bff import support as S

CREATE_PATH = "/api/model-service/keys"


class _Runner:
    """``systemctl`` 的替身：记下每次调用，按脚本回答。"""

    def __init__(self, returncode=0, stderr="", raises=None):
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if self.raises is not None:
            raise self.raises

        class _Done:
            pass

        done = _Done()
        done.returncode = self.returncode
        done.stdout = ""
        done.stderr = self.stderr
        return done


class _Clock:
    def __init__(self):
        self.moment = 0

    def __call__(self):
        self.moment += 1
        return "2026-10-23T00:00:%02d+08:00" % self.moment


def _store(state_dir, *, runner=None, clock=None, env_path=None):
    return ModelServiceKeyStore(
        state_dir,
        env_path=env_path or Path(state_dir) / "strata" / "api-key.env",
        runner=runner or _Runner(),
        clock=clock or _Clock(),
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.env = Path(self.state) / "strata" / "api-key.env"
        self.store = _store(self.state)

    def _env_text(self):
        return self.env.read_text(encoding="utf-8")

    def test_a_new_key_is_appended_and_the_existing_one_survives(self):
        """阳性对照：文件里本来有一条网关用的 key，签发后它必须还在第一条。"""
        self.env.parent.mkdir(parents=True, exist_ok=True)
        self.env.write_text("STRATA_API_KEY=gateway-key\nOTHER_SETTING=1\n", encoding="utf-8")

        record, _restart = self.store.mint("第一把")

        lines = self._env_text().splitlines()
        self.assertIn("OTHER_SETTING=1", lines, "无关的行不能被顺手丢掉")
        key_line = next(line for line in lines if line.startswith("STRATA_API_KEY="))
        self.assertEqual(key_line.split("=", 1)[1].split(",")[0], "gateway-key")
        self.assertIn(record["key"], key_line.split("=", 1)[1].split(","))

    def test_the_key_is_not_appended_twice(self):
        self.store.mint("一次")
        record = self.store.list()[0]
        self.store._append_to_engine(record["key"])  # noqa: SLF001 - 直接验重复追加
        key_line = next(
            line for line in self._env_text().splitlines() if line.startswith("STRATA_API_KEY=")
        )
        self.assertEqual(key_line.count(record["key"]), 1)

    def test_a_missing_engine_env_file_is_created_owner_only(self):
        record, _ = self.store.mint("新建")
        self.assertTrue(self.env.exists())
        self.assertIn(record["key"], self._env_text())
        mode = stat.S_IMODE(self.env.stat().st_mode)
        self.assertEqual(mode, 0o600, "引擎环境文件里有凭据，不能给别人读")

    def test_the_store_file_is_owner_only(self):
        self.store.mint("清单")
        mode = stat.S_IMODE((Path(self.state) / "model-service-keys.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_the_returned_key_is_the_stored_key(self):
        record, _ = self.store.mint("对照")
        self.assertTrue(record["key"].startswith("sk-qwen-"))
        listed = self.store.list()
        self.assertEqual([k["key"] for k in listed], [record["key"]])

    def test_the_newest_key_comes_first(self):
        first, _ = self.store.mint("先")
        second, _ = self.store.mint("后")
        self.assertEqual([k["key"] for k in self.store.list()], [second["key"], first["key"]])

    def test_a_legacy_single_key_file_is_still_listed(self):
        """旧版单数文件里已发出去的 key 不能因为升级就从页面上消失。"""
        legacy = {"key": "sk-qwen-legacy", "label": "旧"}
        (Path(self.state) / "model-service-key.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        listed = self.store.list()
        self.assertEqual([k["key"] for k in listed], ["sk-qwen-legacy"])

    def test_endpoints_come_from_the_deployment_not_the_source(self):
        """对外地址是部署事实：源码里没有内网地址，只有部署声明的那些。"""
        (Path(self.state) / "model-service-key.json").write_text(
            json.dumps({"key": "sk-qwen-legacy"}), encoding="utf-8"
        )
        self.assertNotIn("lan_endpoint", self.store.list()[0])

        with mock.patch.dict(os.environ, {
            "LLMLAB_MODEL_SERVICE_LAN_ENDPOINT": "http://10.0.0.2:8081/v1",
            "LLMLAB_MODEL_SERVICE_EXTERNAL_ENDPOINT": "https://example.invalid/v1",
        }, clear=False):
            entry = self.store.list()[0]
        self.assertEqual(entry["lan_endpoint"], "http://10.0.0.2:8081/v1")
        self.assertEqual(entry["external_endpoint"], "https://example.invalid/v1")

    def test_a_minted_record_carries_the_declared_endpoints(self):
        with mock.patch.dict(os.environ, {"LLMLAB_MODEL_SERVICE_PUBLIC_ENDPOINT": "http://lab.invalid/v1"}):
            record, _ = self.store.mint("地址")
        self.assertEqual(record["public_endpoint"], "http://lab.invalid/v1")
        self.assertEqual(record["endpoint"], DEFAULT_ENDPOINT)

    def test_a_blank_label_falls_back_to_the_default(self):
        record, _ = self.store.mint("   ")
        self.assertTrue(record["label"].strip())
        self.assertNotEqual(record["label"], "   ")

    def test_a_failed_restart_is_reported_not_hidden(self):
        """引擎重载失败时，密钥**已经写进文件**，但还不能用 —— 两句事实都要在。"""
        runner = _Runner(returncode=1, stderr="Unit strata.service not found.")
        store = _store(self.state, runner=runner)
        record, restart = store.mint("失败")
        self.assertEqual(restart["triggered"], False)
        self.assertIn("not found", restart["detail"])
        self.assertIn(record["key"], self._env_text(), "写文件与重载是两件事，前者已经完成")

    def test_a_missing_systemctl_is_a_failed_restart_not_an_exception(self):
        store = _store(self.state, runner=_Runner(raises=FileNotFoundError("systemctl")))
        _record, restart = store.mint("没有 systemctl")
        self.assertEqual(restart["triggered"], False)
        self.assertEqual(restart["detail"], "FileNotFoundError")

    def test_a_successful_restart_says_so(self):
        runner = _Runner(returncode=0)
        store = _store(self.state, runner=runner)
        _record, restart = store.mint("成功")
        self.assertEqual(restart["triggered"], True)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["argv"][:3], ["systemctl", "--user", "restart"])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.env = Path(self.state) / "strata" / "api-key.env"
        self.runner = _Runner()
        self.store = _store(self.state, runner=self.runner)

    def harness(self, *, attached=True):
        h = S.build_harness(
            state_dir=self.state,
            model_service_keys=self.store if attached else None,
        )
        self.addCleanup(h.close)
        return h

    def env_text(self):
        return self.env.read_text(encoding="utf-8") if self.env.exists() else ""

    def test_anonymous_cannot_read_the_key(self):
        # harness 的 ``client`` 会先登入；匿名要用一个不带 cookie 的客户端。
        from tests.auth.support import WsgiClient

        anonymous = WsgiClient(self.harness().app, host="127.0.0.1:8790")
        response = anonymous.run("GET", MODEL_SERVICE_KEY_PATH)
        self.assertEqual(response.status, 401)
        self.assertNotIn("sk-qwen-", json.dumps(response.json()))

    def test_a_member_cannot_read_the_key(self):
        self.store.mint("已发")
        response = self.harness().client(S.MEMBER_NAME).run("GET", MODEL_SERVICE_KEY_PATH)
        self.assertEqual(response.status, 403)
        self.assertNotIn("sk-qwen-", json.dumps(response.json()))

    def test_an_admin_sees_an_empty_list_before_anything_is_minted(self):
        client = self.harness().admin_client(S.ADMIN_NAME)
        response = client.run("GET", MODEL_SERVICE_KEY_PATH)
        self.assertEqual(response.status, 200, "没发过不是 404：这一页能打开")
        body = response.json()["data"]
        self.assertIsNone(body["key"])
        self.assertEqual(body["keys"], [])

    def test_a_member_cannot_mint(self):
        client = self.harness().client(S.MEMBER_NAME)
        response = client.run(
            "POST", CREATE_PATH, payload={"label": "偷偷"}, headers={"X-CSRF-Token": S.csrf(client)}
        )
        self.assertEqual(response.status, 403)
        self.assertNotIn("STRATA_API_KEY=", self.env_text())
        self.assertEqual(self.runner.calls, [], "被拒的请求不该触发引擎重载")

    def test_minting_without_csrf_is_refused(self):
        """跨站 POST 不能借着浏览器里的会话把凭据发出去。"""
        client = self.harness().admin_client(S.ADMIN_NAME)
        response = client.run("POST", CREATE_PATH, payload={"label": "跨站"})
        self.assertEqual(response.status, 403)
        self.assertNotIn("STRATA_API_KEY=", self.env_text())
        self.assertEqual(self.runner.calls, [])

    def test_an_admin_mints_and_the_engine_file_gets_the_key(self):
        client = self.harness().admin_client(S.ADMIN_NAME)
        response = client.run(
            "POST", CREATE_PATH, payload={"label": "客户端"}, headers={"X-CSRF-Token": S.csrf(client)}
        )
        self.assertEqual(response.status, 201)
        record = response.json()["data"]
        self.assertTrue(record["key"].startswith("sk-qwen-"))
        self.assertIn(record["key"], self.env_text())
        self.assertTrue(record["restart"]["triggered"])
        # 清单里也只存了一份真身；再次读取拿到的就是它。
        listed = self.store.list()
        self.assertEqual([k["key"] for k in listed], [record["key"]])

    def test_a_deployment_without_the_store_is_503_not_a_masked_list(self):
        client = self.harness(attached=False).admin_client(S.ADMIN_NAME)
        response = client.run("GET", MODEL_SERVICE_KEY_PATH)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "bff.model_service_key_not_attached")


if __name__ == "__main__":
    unittest.main()
