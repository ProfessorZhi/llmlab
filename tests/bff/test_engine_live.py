"""``/api/v2/engine/live``：BFF 代取引擎读数，密钥不出后端。

这一组断言的不是「读数好不好看」，而是三条口径：

1. **读不到就是读不到。** 拿不到数时所有数值字段是 ``None``，不是 0 ——
   「排了 0 个」和「不知道排了没有」是两件事，混起来就是撒谎。
2. **密钥不出现在任何响应里。** 引擎凭据只在服务端；这条路由存在的理由就是
   不让它进浏览器。
3. **它仍是一道会话门。** 匿名调用 401，且在任何读引擎的动作之前就拒。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error

from backend.bff import engine_live
from backend.bff.app import ENGINE_LIVE_PATH
from tests.bff import support as S


def _payload(**overrides):
    """一份与 Strata ``/metrics`` **同形**的载荷，故意塞上提示词与输出正文。"""
    base = {
        "engine": {
            "model": "qwen-test", "max_context": 32768, "images": True,
            "batch_slots": 4, "version": "0.1.41",
        },
        "live": {
            "state": "decoding", "parallel": 4, "running": 2, "queued": 1, "waiting": 0,
            "tok_s": 91.5, "tok_s_mean": 80.25, "prefill_tok_s_mean": 301.0,
            "slots": [
                {"slot": 0, "state": "decoding", "held_tokens": 512},
                {"slot": 1, "state": "idle", "held_tokens": 0},
            ],
        },
        "totals": {
            "since": 1_700_000_000.0, "requests": 7,
            "prompt_tokens": 900, "output_tokens": 450,
        },
        "requests": [{
            "time": 1_700_000_100.0, "duration_s": 0.8, "finish": "stop",
            "prompt_tokens": 53, "output_tokens": 36, "decode_tok_s": 109.4,
            "prompt": "PROMPT-TEXT-MUST-NOT-LEAVE", "completion": "ANSWER-MUST-NOT-LEAVE",
        }],
    }
    base.update(overrides)
    return base


def _key_file(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return handle.name


def _fetch(payload=None, error=None, calls=None):
    def _call(*, url, key, timeout):
        if calls is not None:
            calls.append({"url": url, "key": key, "timeout": timeout})
        if error is not None:
            raise error
        return payload
    return _call


class ProjectionTests(unittest.TestCase):
    def test_metadata_is_kept_and_message_text_is_dropped(self):
        """投影只搬运元数据。载荷里**有**提示词与正文，输出里必须一个都不剩。"""
        out = engine_live.project(_payload(), observed_at="2026-10-22T00:00:00Z")
        blob = json.dumps(out)
        self.assertEqual(out["reachable"], True)
        self.assertEqual(out["engine"]["model"], "qwen-test")
        self.assertEqual(out["live"]["running"], 2)
        self.assertEqual(out["live"]["slots"][0]["held_tokens"], 512)
        self.assertEqual(out["throughput"]["tok_s"], 91.5)
        self.assertEqual(out["totals"]["requests"], 7)
        self.assertEqual(len(out["recent"]), 1)
        self.assertEqual(out["recent"][0]["decode_tok_s"], 109.4)
        # 阳性对照：载荷里确实有这两串，输出里必须没有。
        self.assertIn("PROMPT-TEXT-MUST-NOT-LEAVE", json.dumps(_payload()))
        self.assertNotIn("PROMPT-TEXT-MUST-NOT-LEAVE", blob)
        self.assertNotIn("ANSWER-MUST-NOT-LEAVE", blob)

    def test_missing_fields_are_null_not_zero(self):
        out = engine_live.project({}, observed_at="2026-10-22T00:00:00Z")
        self.assertTrue(out["reachable"])
        self.assertIsNone(out["live"]["queued"])
        self.assertIsNone(out["throughput"]["tok_s"])
        self.assertIsNone(out["totals"]["requests"])
        self.assertEqual(out["live"]["slots"], [])
        self.assertEqual(out["recent"], [])

    def test_a_boolean_is_not_a_number(self):
        """``True`` 是 ``int`` 的子类；不能让它冒充 1 溜进数值字段。"""
        out = engine_live.project({"live": {"queued": True}}, observed_at="x")
        self.assertIsNone(out["live"]["queued"])

    def test_a_non_object_payload_is_bad_shape(self):
        out = engine_live.project([1, 2, 3], observed_at="x")
        self.assertFalse(out["reachable"])
        self.assertEqual(out["reason"], "bad_payload")
        self.assertIsNone(out["live"])

    def test_recent_is_bounded(self):
        payload = _payload(requests=[{"time": 1_700_000_000.0 + i} for i in range(40)])
        out = engine_live.project(payload, observed_at="x")
        self.assertEqual(len(out["recent"]), engine_live.RECENT_LIMIT)


class KeyFileTests(unittest.TestCase):
    def test_the_first_entry_is_the_engines_own_key(self):
        """文件里第一条是引擎本体的 key，后面才是发给别人的；只能取第一条。"""
        path = _key_file("STRATA_API_KEY=engine-key,delivered-sk-qwen-one,delivered-two\n")
        self.addCleanup(os.unlink, path)
        self.assertEqual(engine_live.read_engine_key(path), "engine-key")

    def test_a_comment_or_blank_file_has_no_key(self):
        path = _key_file("# STRATA_API_KEY=nope\n\n")
        self.addCleanup(os.unlink, path)
        self.assertIsNone(engine_live.read_engine_key(path))

    def test_a_missing_file_has_no_key(self):
        self.assertIsNone(engine_live.read_engine_key("/nonexistent/strata/api-key.env"))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.path = _key_file("STRATA_API_KEY=engine-key-0123456789\n")
        self.addCleanup(os.unlink, self.path)

    def read(self, **kwargs):
        return engine_live.read_engine_live(key_file=self.path, clock=lambda: 1_700_000_000.0, **kwargs)

    def test_a_healthy_engine_is_projected_without_its_key(self):
        calls = []
        out = self.read(fetch=_fetch(_payload(), calls=calls))
        self.assertTrue(out["reachable"])
        self.assertEqual(out["observed_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(calls[0]["key"], "engine-key-0123456789")
        self.assertEqual(calls[0]["url"], engine_live.DEFAULT_URL)
        # 阳性对照：那把 key 确实进了请求；响应里必须没有它。
        self.assertNotIn("engine-key-0123456789", json.dumps(out))

    def test_no_configured_key_is_its_own_reason(self):
        out = engine_live.read_engine_live(key_file="/nonexistent/env", clock=lambda: 0.0)
        self.assertFalse(out["reachable"])
        self.assertEqual(out["reason"], "key_not_configured")
        self.assertIsNone(out["live"])
        self.assertIsNone(out["totals"])

    def test_a_refused_credential_is_not_an_outage(self):
        error = urllib.error.HTTPError("http://x", 401, "Unauthorized", None, io.BytesIO(b""))
        out = self.read(fetch=_fetch(error=error))
        self.assertEqual(out["reason"], "unauthorized")

    def test_connection_refused_is_unreachable(self):
        error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        out = self.read(fetch=_fetch(error=error))
        self.assertEqual(out["reason"], "unreachable")

    def test_garbage_is_bad_payload(self):
        out = self.read(fetch=_fetch(error=ValueError("not json")))
        self.assertEqual(out["reason"], "bad_payload")

    def test_an_http_error_that_is_not_auth_is_http_error(self):
        error = urllib.error.HTTPError("http://x", 500, "boom", None, io.BytesIO(b""))
        out = self.read(fetch=_fetch(error=error))
        self.assertEqual(out["reason"], "http_error")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self.payload = engine_live.project(
            _payload(), observed_at="2026-10-22T00:00:00Z"
        )
        engine_live_stub = self._stub
        self.h = S.build_harness(
            state_dir=tempfile.mkdtemp(), clock=S.FixedClock(), engine_live=engine_live_stub
        )
        self.addCleanup(self.h.close)

    def _stub(self):
        self.calls += 1
        return self.payload

    def test_anonymous_is_refused_before_anything_is_read(self):
        # Host 权威守卫在任何身份判断之前：默认的 portal.example.com 会被 421 拒掉，
        # 那样测到的就不是「匿名」而是「错的主机名」。
        client = S.WsgiClient(self.h.app, host="127.0.0.1:8790")
        response = client.run("GET", ENGINE_LIVE_PATH)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, 0, "匿名请求不该触发一次引擎读数")

    def test_a_member_gets_the_projection(self):
        response = self.h.client("alice").run("GET", ENGINE_LIVE_PATH)
        self.assertEqual(response.status, 200)
        body = response.json()["data"]
        self.assertTrue(body["reachable"])
        self.assertEqual(body["engine"]["model"], "qwen-test")
        self.assertNotIn("PROMPT-TEXT-MUST-NOT-LEAVE", json.dumps(body))
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
