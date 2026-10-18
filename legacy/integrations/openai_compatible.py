# -*- coding: utf-8 -*-
"""Adapter for any OpenAI-shaped HTTP endpoint.

Written for phase C of the roadmap -- the company DeepSeek gateway and the
production LiteLLM gateway -- which are **declared in ``config/llm-lab.example.toml``
but not enabled**, because enabling them needs a credential decision that has
not been made.

Honesty note, so nobody mistakes this file for tested code: it has been run
against nothing.  Its request and response shapes follow the OpenAI
``/v1/models`` and ``/v1/chat/completions`` contracts, which both gateways
implement, but no live call has been made from this project and no response
here has been verified end to end.  Treat the first real call as a test, not as
a routine operation.

Two properties are inherited from the package contract and are deliberate:

  * a **billing-shaped** call is never automatic -- nothing in the console
    calls ``chat()`` on a page load, a refresh, or a health check;
  * the key is read from a file, never passed in, and a failing call reports
    the upstream status rather than retrying blindly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .strata import get_json, read_secret


class OpenAICompatibleClient:
    def __init__(self, base_url: str = "", secret_file: str = "", api_key_var: str = "",
                 timeout: float = 10.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.secret_file = secret_file
        self.api_key_var = api_key_var
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        return read_secret(self.api_key_var, self.secret_file)

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        key = self.api_key
        if key:
            headers["Authorization"] = "Bearer" + " " + key
        return headers

    def models(self):
        return get_json(self.base_url + "/v1/models", key=self.api_key, timeout=self.timeout)

    def chat(self, messages, model: str, max_tokens: int = 256, **kwargs):
        if not self.base_url:
            return False, {"error": "base_url is empty; the endpoint is declared but not configured"}, 0
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "stream": False}
        payload.update(kwargs)
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(self._headers(), **{"Content-Type": "application/json"}),
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout * 12) as resp:
                return True, json.loads(resp.read().decode("utf-8", "replace")), resp.status
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            # 403 here is almost always a model-name/credential mismatch rather
            # than a network fault, so surface the upstream message verbatim.
            return False, {"http_status": exc.code, "detail": detail}, exc.code
        except Exception as exc:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(exc).__name__, exc)}, 0

    def probe(self) -> dict:
        """Cheap reachability check: model list only, never a completion."""
        out = {"base_url": self.base_url, "key_present": bool(self.api_key)}
        if not self.base_url:
            out["ok"] = False
            out["error"] = "declared but not enabled: no base_url configured"
            return out
        ok, payload, code = self.models()
        out["ok"] = ok
        out["models"] = payload if ok else None
        if not ok:
            out["error"] = payload
        return out
