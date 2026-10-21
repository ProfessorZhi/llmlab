"""插件页的跳转入口：每个插件可选地带一个「原生界面」地址。

推理引擎、监控、评测工具里，很多自带网页界面（Strata 控制台、Grafana、
EvalScope 报告……）。平台不嵌入、不代理它们，只在插件页给一个跳转入口：
管理员填地址，成员点一下在新标签页打开。没有填地址的插件就是一个普通插件
（例如只有 API 的 vLLM）。

规则：

* 插件目录是**固定的**（:data:`CATALOG`）；客户端不能新增插件，只能给已有
  插件设地址；
* 地址只能是 ``http(s)://`` 绝对地址或站内路径（``/…``）；``javascript:``、
  ``//host``、带用户名密码的地址一律拒绝；
* ``visibility`` 决定谁能看到这个地址：``all`` 全体成员，``admin`` 仅管理员。
  对看不到的人，接口下发 ``ui_url: null``——不是把地址交给前端去藏；
* 状态存在部署的状态目录里（``integrations.json``，0600，原子替换）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

VISIBILITY_ALL = "all"
VISIBILITY_ADMIN = "admin"
VISIBILITIES = (VISIBILITY_ALL, VISIBILITY_ADMIN)

KIND_ENGINE = "engine"
KIND_OBSERVABILITY = "observability"
KIND_EVALUATION = "evaluation"
KIND_GATEWAY = "gateway"

MAX_URL_LENGTH = 512


@dataclass(frozen=True)
class Plugin:
    key: str
    name: str
    kind: str
    #: 没设过时谁能看到地址。原生监控面默认只给管理员。
    default_visibility: str = VISIBILITY_ALL


CATALOG: tuple[Plugin, ...] = (
    Plugin("strata", "Strata", KIND_ENGINE),
    Plugin("vllm", "vLLM", KIND_ENGINE),
    Plugin("sglang", "SGLang", KIND_ENGINE),
    Plugin("gateway", "Gateway", KIND_GATEWAY, VISIBILITY_ADMIN),
    Plugin("prometheus", "Prometheus", KIND_OBSERVABILITY, VISIBILITY_ADMIN),
    Plugin("grafana", "Grafana", KIND_OBSERVABILITY, VISIBILITY_ADMIN),
    Plugin("evalscope", "EvalScope", KIND_EVALUATION),
)
CATALOG_BY_KEY = {plugin.key: plugin for plugin in CATALOG}


class InvalidIntegration(ValueError):
    """请求不合法（400）。"""


def validate_ui_url(raw: object) -> str | None:
    """``None`` / 空串 = 清除地址；否则返回规范化后的地址，不合法就抛错。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidIntegration("ui_url must be a string")
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_URL_LENGTH:
        raise InvalidIntegration(f"ui_url is longer than {MAX_URL_LENGTH} characters")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        raise InvalidIntegration("ui_url must not contain spaces or control characters")
    if value.startswith("/"):
        if value.startswith("//") or value.startswith("/\\"):
            raise InvalidIntegration("ui_url must be an http(s) URL or a site path like /grafana/")
        return value
    split = urllib.parse.urlsplit(value)
    if split.scheme not in ("http", "https") or not split.hostname:
        raise InvalidIntegration("ui_url must be an http(s) URL or a site path like /grafana/")
    if split.username is not None or split.password is not None:
        raise InvalidIntegration("ui_url must not carry a username or password")
    return value


class IntegrationStore:
    """``{key: {"ui_url": str|None, "visibility": str}}``，存一个 JSON 文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".integrations-", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def entry(self, plugin: Plugin) -> dict:
        saved = self._read().get(plugin.key)
        saved = saved if isinstance(saved, dict) else {}
        url = saved.get("ui_url")
        try:
            url = validate_ui_url(url)
        except InvalidIntegration:
            url = None  # 一份被手改坏的文件只会让入口消失，不会下发一个坏地址
        visibility = saved.get("visibility")
        if visibility not in VISIBILITIES:
            visibility = plugin.default_visibility
        return {"ui_url": url, "visibility": visibility}

    def update(self, key: str, *, ui_url: object, visibility: object) -> dict:
        plugin = CATALOG_BY_KEY.get(key)
        if plugin is None:
            raise KeyError(key)
        url = validate_ui_url(ui_url)
        if visibility is None:
            visibility = self.entry(plugin)["visibility"]
        if visibility not in VISIBILITIES:
            raise InvalidIntegration("visibility must be 'all' or 'admin'")
        with self._lock:
            data = self._read()
            data[key] = {"ui_url": url, "visibility": visibility}
            self._write(data)
        return self.entry(plugin)

    def listing(self, *, is_admin: bool) -> list[dict]:
        rows = []
        for plugin in CATALOG:
            entry = self.entry(plugin)
            visible = is_admin or entry["visibility"] == VISIBILITY_ALL
            rows.append(
                {
                    "key": plugin.key,
                    "name": plugin.name,
                    "kind": plugin.kind,
                    "has_ui": entry["ui_url"] is not None,
                    "ui_url": entry["ui_url"] if visible else None,
                    "visibility": entry["visibility"],
                }
            )
        return rows


__all__ = [
    "CATALOG",
    "CATALOG_BY_KEY",
    "IntegrationStore",
    "InvalidIntegration",
    "VISIBILITIES",
    "validate_ui_url",
]
