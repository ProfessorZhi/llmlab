"""平台签发的模型推理密钥：读取、签发、交给引擎。

和别的数据源不同，这里存的是**凭据本身**，所以每一处都按凭据对待：
清单文件 0600、引擎环境文件只追加不改写、签发结果只回给平台管理员。

引擎（Strata）只在启动时读一次 ``STRATA_API_KEY``，没有热加载。因此「新建
密钥」不是一次纯文件写入 —— 它必须让引擎重载才能真正生效。这个事实会被
原样返回（``restart``），前端据此告诉用户密钥何时可用，而不是假装立刻可用。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import datetime
from pathlib import Path

#: 与本机引擎对齐的默认值。前端只展示，不再让用户填这些。
DEFAULT_MODEL = "qwen3.8-flash-next-iq2_xs"
DEFAULT_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_PUBLIC_ENDPOINT = "http://fj-nj-ser-1009.tail3d8b76.ts.net:8080/v1/chat/completions"
#: 公网交付地址：tailscale funnel（:8443）→ 本机 :8081 的白名单代理 → Strata。
#: 与 :8080 那条不同，这条**不要求对方在同一 tailnet**，拿到密钥就能调。
DEFAULT_EXTERNAL_ENDPOINT = "https://fj-nj-ser-1009.tail3d8b76.ts.net:8443/v1/chat/completions"
#: 内网交付地址：同一局域网里不带 tailscale 的机器直接连这个（白名单代理的 LAN 口）。
DEFAULT_LAN_ENDPOINT = "http://192.168.32.179:8081/v1/chat/completions"
DEFAULT_CONCURRENCY = 2

#: 清单文件（复数）。单数 ``model-service-key.json`` 是上一版留下的，仍然读，
#: 这样升级不会让已经发出去的那把密钥从页面上消失。
STORE_NAME = "model-service-keys.json"
LEGACY_NAME = "model-service-key.json"

#: 密钥前缀。带上模型名是为了拿到一串 ``sk-...`` 时还认得出它是哪把。
KEY_PREFIX = "sk-qwen-"


def _atomic_write(path: Path, body: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ModelServiceKeyStore:
    """``<state-dir>`` 里的密钥清单 + 引擎环境文件的接线。"""

    def __init__(
        self,
        state_dir,
        *,
        env_path=None,
        unit: str = "strata.service",
        runner=None,
        clock=None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.env_path = Path(env_path) if env_path else Path.home() / ".config" / "strata" / "api-key.env"
        self.unit = unit
        self._runner = runner or subprocess.run
        self._clock = clock or _now

    # --- 读 ---------------------------------------------------------------

    def _load(self) -> list[dict]:
        plural = self.state_dir / STORE_NAME
        if plural.exists():
            data = json.loads(plural.read_text(encoding="utf-8"))
            keys = data.get("keys") if isinstance(data, dict) else data
            return [k for k in (keys or []) if isinstance(k, dict) and k.get("key")]
        legacy = self.state_dir / LEGACY_NAME
        if legacy.exists():
            one = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(one, dict) and one.get("key"):
                return [one]
        return []

    def list(self) -> list[dict]:
        """新签发的排在最前 —— 页面上第一眼该看到的是刚发出去的那把。

        早于公网入口签发的记录里没有 ``external_endpoint``，但**同一把密钥确实
        能从公网调**（funnel 转发到同一台引擎），所以在读出时补上这个地址，而
        不是让页面以为那些密钥只能在内网用。
        """
        keys = self._load()
        for key in keys:
            key.setdefault("external_endpoint", DEFAULT_EXTERNAL_ENDPOINT)
            key.setdefault("lan_endpoint", DEFAULT_LAN_ENDPOINT)
        keys.sort(key=lambda k: str(k.get("created_at") or ""), reverse=True)
        return keys

    # --- 写 ---------------------------------------------------------------

    def _save(self, keys: list[dict]) -> None:
        body = json.dumps({"version": 1, "keys": keys}, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(self.state_dir / STORE_NAME, body, 0o600)

    def _append_to_engine(self, key: str) -> None:
        """把密钥追加进引擎的环境文件。**只追加**，绝不覆盖已有的。

        覆盖是这里唯一真正危险的动作：文件里第一把是 llmlab 网关在用的密钥，
        把它冲掉就等于把网关的上游打断，而报错会出现在一个跟本次操作无关的
        组件里。
        """
        path = self.env_path
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out: list[str] = []
        found = False
        for line in lines:
            if line.startswith("STRATA_API_KEY="):
                found = True
                existing = [p.strip() for p in line.split("=", 1)[1].split(",") if p.strip()]
                if key in existing:
                    out.append(line)
                else:
                    out.append("STRATA_API_KEY=" + ",".join(existing + [key]))
            else:
                out.append(line)
        if not found:
            out.append("STRATA_API_KEY=" + key)
        _atomic_write(path, "\n".join(out) + "\n", 0o600)

    def _restart_engine(self) -> dict:
        try:
            done = self._runner(
                ["systemctl", "--user", "restart", "--no-block", self.unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"triggered": False, "detail": type(exc).__name__}
        if done.returncode != 0:
            detail = (done.stderr or "").strip()[:200] or "exit %d" % done.returncode
            return {"triggered": False, "detail": detail}
        return {
            "triggered": True,
            "detail": "引擎正在重载，恢复监听约需 1 分钟；此前这把密钥还不会生效。",
        }

    def mint(self, label=None, *, model=None, concurrency=None) -> tuple[dict, dict]:
        """签发一把新密钥。

        顺序是先写引擎环境文件、再把记录落盘：万一后一步失败，结果是「能用的
        密钥没被列出来」（可以补录），而不是「列出来了却用不了」（会骗人）。
        """
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        self._append_to_engine(key)
        record = {
            "id": "msk_" + secrets.token_urlsafe(8),
            "label": (label or "").strip() or "平台签发 · 模型推理",
            "key": key,
            "model": model or DEFAULT_MODEL,
            "endpoint": DEFAULT_ENDPOINT,
            "public_endpoint": DEFAULT_PUBLIC_ENDPOINT,
            "external_endpoint": DEFAULT_EXTERNAL_ENDPOINT,
            "lan_endpoint": DEFAULT_LAN_ENDPOINT,
            "concurrency": concurrency or DEFAULT_CONCURRENCY,
            "engine": "strata (fjd 用户态服务)",
            "created_at": self._clock(),
            "note": "调用本机推理引擎用；不具备平台管理权限。",
        }
        keys = self._load()
        keys.append(record)
        self._save(keys)
        return record, self._restart_engine()
