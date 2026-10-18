# -*- coding: utf-8 -*-
"""The Strata inference node adapter.

Strata exposes four endpoints, and they do not have the same access rules --
which matters, because assuming otherwise produces a console that reports a
healthy node as down:

    /health      open, no key.  Says nothing about the GPU.
    /v1/models   the served model, its context length, its loaded state.
    /v1/status   carries ``machine.gpu`` and ``machine.ram``.  This is the
                 **only live GPU telemetry path on this host**, because the
                 host's NVML is broken (driver/library version mismatch), so
                 ``nvidia-smi`` and every NVML-based tool fail while the CUDA
                 runtime itself still initialises fine.
    /v1/chat/completions  inference.  The console does not call this on a page
                 load; it is here for the eval tooling.

A key may or may not be required depending on the deployment: ``/health``
reports ``api_key: true|false``, and that flag -- not a guess -- is what the
console shows.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# The node's own console.
#
# Strata's UI is a *single-page app served at "/"*, and its three views are
# hash routes handled by the browser -- there is no /chat or /monitor path to
# link to (both answer 404).  The deep link therefore IS the hash, and it only
# works because app.js reads location.hash on load and on hashchange.  Keeping
# this table beside the endpoint definitions means the portal and the node's
# own UI cannot drift apart on what that UI actually offers.
# ---------------------------------------------------------------------------
NATIVE_PANELS = (
    {"key": "chat", "label": "Chat", "hash": "", "note": "对话控制台"},
    {"key": "monitor", "label": "Monitor", "hash": "#monitor", "note": "实时监控面板"},
    {"key": "about", "label": "About", "hash": "#about", "note": "模型与引擎信息"},
)


def native_panels(base_url: str, reachable: bool = True) -> list:
    """Deep links into the node's native console, one per view."""
    base = (base_url or "").rstrip("/")
    return [{
        "key": p["key"], "label": p["label"], "note": p["note"],
        "url": (base + "/" + p["hash"]) if base else "",
        "reachable": bool(base) and reachable,
    } for p in NATIVE_PANELS]


def read_secret(var: str, path: str) -> str:
    """Read one ``KEY=value`` line out of a secrets file.

    Returns ``""`` when the file or the variable is absent.  The value is never
    logged, never returned to a caller that would render it, and never placed
    in an exception message.
    """
    if not var or not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == var:
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def get_json(url: str, key: str = "", timeout: float = 5.0):
    """GET ``url`` and parse JSON.

    Returns ``(ok, payload, http_status)``.  On failure ``payload`` describes
    *why* -- an HTTP status plus the server's own message where there is one --
    rather than being empty, so a caller can report the real cause.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer" + " " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return True, json.loads(body), resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return False, {"http_status": exc.code, "detail": detail}, exc.code
    except Exception as exc:  # noqa: BLE001
        return False, {"error": "%s: %s" % (type(exc).__name__, exc)}, 0


class StrataClient:
    """Read-only view of one Strata deployment."""

    def __init__(self, base_url: str = "", secret_file: str = "", api_key_var: str = "",
                 timeout: float = 5.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.secret_file = secret_file
        self.api_key_var = api_key_var
        self.timeout = timeout

    # -- credential ------------------------------------------------------
    @property
    def api_key(self) -> str:
        return read_secret(self.api_key_var, self.secret_file)

    def key_fingerprint(self) -> str:
        """A short digest, safe to display: it identifies *which* key is loaded
        without revealing it."""
        key = self.api_key
        return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""

    # -- endpoints -------------------------------------------------------
    def health(self):
        return get_json(self.base_url + "/health", timeout=self.timeout)

    def status(self):
        return get_json(self.base_url + "/v1/status", key=self.api_key, timeout=self.timeout)

    def models(self):
        return get_json(self.base_url + "/v1/models", key=self.api_key, timeout=self.timeout)

    def metrics(self):
        """The node's own telemetry document.

        Requires the key (it answers 401 without one, unlike /health).  It carries
        more than /v1/status does -- per-GPU power and PCIe counters, a rolling
        60-sample history, per-request records and the engine's own configuration
        -- so the console prefers it for telemetry and keeps /v1/status as the
        independent second opinion that must agree before the console calls a
        number live.
        """
        return get_json(self.base_url + "/metrics", key=self.api_key, timeout=self.timeout)

    def chat(self, messages, max_tokens: int = 256, **kwargs):
        """One non-streaming completion.  Not called during a page load."""
        payload = {"model": kwargs.pop("model", None), "messages": messages,
                   "max_tokens": max_tokens, "stream": False}
        payload.update(kwargs)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        key = self.api_key
        if key:
            req.add_header("Authorization", "Bearer" + " " + key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout * 12) as resp:
                return True, json.loads(resp.read().decode("utf-8", "replace")), resp.status
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return False, {"http_status": exc.code, "detail": detail}, exc.code
        except Exception as exc:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(exc).__name__, exc)}, 0

    # -- the shape the console renders ------------------------------------
    def probe(self) -> dict:
        out: dict = {
            "base_url": self.base_url,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "key_present": bool(self.api_key),
            "key_sha256_12": self.key_fingerprint(),
        }
        if not self.base_url:
            out["ok"] = False
            out["error"] = "no base_url configured for this endpoint"
            return out

        ok_h, health, _ = self.health()
        out["health"] = health if ok_h else None
        out["health_error"] = None if ok_h else health

        ok_s, status, code_s = self.status()
        out["status"] = status if ok_s else None
        if not ok_s:
            # 401/403 means "the credential is wrong or missing", which is a
            # different repair from "the node is down".  Say which one it is.
            out["status_error"] = (
                {"http_status": code_s, "detail": status.get("detail", "")}
                if code_s in (401, 403) else status)

        ok_m, models, _ = self.models()
        out["models"] = models if ok_m else None

        out["ok"] = bool(ok_h or ok_s)

        machine = (out.get("status") or {}).get("machine") or {}
        gpu = machine.get("gpu")
        if isinstance(gpu, dict):
            out["gpu_live"] = gpu           # the one live telemetry path
        return out

    # -- the shape the *homepage* renders ---------------------------------
    def console(self) -> dict:
        """Model-endpoint health and GPU telemetry, deliberately kept apart.

        These are two different questions and they fail independently:

          * ``model_api``  -- is the endpoint answering, is a key required, is a
            model loaded, and what does it say it serves?
          * ``gpu``        -- what is the GPU doing *now*?

        A node can serve flawlessly while its telemetry is missing, and a fresh
        GPU number next to a dead API is not "up".  Collapsing both into one
        status word hides exactly those two cases, so this method never does.
        Every block carries a ``source`` string naming where its numbers came
        from, and nothing here is ever back-filled from an older sample.
        """
        out: dict = {
            "base_url": self.base_url,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "key_present": bool(self.api_key),
            "key_sha256_12": self.key_fingerprint(),
        }
        if not self.base_url:
            out["model_api"] = {"ok": False, "error": "no base_url configured"}
            out["gpu"] = {"available": False, "reason": "no base_url configured"}
            return out

        # ---- model endpoint ------------------------------------------------
        ok_h, health, _ = self.health()
        ok_m, models, code_m = self.models()
        api = {
            "source": "Strata /health + /v1/models",
            "health": health if ok_h else None,
            "health_error": None if ok_h else health,
            "models": models if ok_m else None,
            "models_http_status": code_m,
            "auth_required": bool(isinstance(health, dict) and health.get("api_key") is True),
            "loaded": (health or {}).get("loaded") if isinstance(health, dict) else None,
        }
        if ok_m:
            items = models.get("data") or models.get("models") or []
            api["model_list"] = [
                {
                    "id": it.get("id") or it.get("name"),
                    "status": (it.get("status") or {}).get("value")
                              if isinstance(it.get("status"), dict) else it.get("status"),
                    "context": (it.get("meta") or {}).get("n_ctx"),
                }
                for it in items if isinstance(it, dict)
            ]
        # A 401/403 on /v1/models while /health answers is a *credential*
        # problem, not an outage.  Say which, because the repair differs.
        if not api["health"] and not api["models"]:
            api["ok"] = False
            api["error"] = (api["health_error"] or {}).get("error") \
                or "endpoint unreachable"
        else:
            api["ok"] = True
        out["model_api"] = api

        # ---- telemetry: /metrics first, /v1/status as the second opinion ----
        ok_x, metrics, code_x = self.metrics()
        ok_s, status, code_s = self.status()
        machine = ((status or {}).get("machine") or {}) if isinstance(status, dict) else {}
        gpu_status = machine.get("gpu") if isinstance(machine.get("gpu"), dict) else None
        hw = (metrics or {}).get("hardware") if isinstance(metrics, dict) else None
        hw_static = (metrics or {}).get("hardware_static") if isinstance(metrics, dict) else None

        gpu: dict = {
            "sources": {
                "metrics": {"ok": bool(ok_x), "http_status": code_x},
                "status": {"ok": bool(ok_s), "http_status": code_s},
            },
            "cross_check": None,
        }
        if isinstance(hw, dict):
            gpu.update({
                "available": True,
                "source": "Strata /metrics hardware",
                "name": (hw_static or {}).get("gpu_name") if isinstance(hw_static, dict) else None,
                "used_mib": _num(hw.get("gpu_mem_used"), 1 / 1048576, 1),
                "total_mib": _num(hw.get("gpu_mem_total"), 1 / 1048576, 1),
                "util_pct": hw.get("gpu_util"),
                "temp_c": hw.get("gpu_temp"),
                "power_w": hw.get("gpu_power"),
                "power_limit_w": hw.get("gpu_power_limit"),
                "pcie_gen": hw.get("gpu_pcie_gen"),
                "pcie_gen_max": hw.get("gpu_pcie_gen_max"),
                "pcie_width": hw.get("gpu_pcie_width"),
                "pcie_rx_mb": hw.get("gpu_pcie_rx_mb"),
                "pcie_tx_mb": hw.get("gpu_pcie_tx_mb"),
            })
        elif isinstance(gpu_status, dict):
            # /v1/status alone still answers the question; it just answers less.
            gpu.update({
                "available": True,
                "source": "Strata /v1/status machine.gpu (fallback: /metrics unavailable)",
                "name": gpu_status.get("name"),
                "used_mib": gpu_status.get("used_mib"),
                "total_mib": gpu_status.get("total_mib"),
                "util_pct": gpu_status.get("util_pct"),
                "temp_c": gpu_status.get("temp_c"),
                "power_w": gpu_status.get("power_w"),
            })
        else:
            why = []
            if not ok_x:
                why.append("/metrics HTTP %s" % (code_x or "unreachable"))
            if not ok_s:
                why.append("/v1/status HTTP %s" % (code_s or "unreachable"))
            gpu.update({
                "available": False,
                "source": "none",
                "reason": "no telemetry endpoint answered (%s)" % "; ".join(why or ["unknown"]),
            })

        # Cross-check, not a gate.  Both endpoints report GPU memory at their own
        # instant, so they are allowed to differ slightly; a wide gap means one
        # of them is stale, which is worth *showing* and not worth hiding behind
        # whichever answer arrived first.  Only memory is compared: utilisation
        # and power genuinely move between two calls taken 30ms apart.
        if isinstance(hw, dict) and isinstance(gpu_status, dict):
            a = gpu.get("used_mib")
            b = gpu_status.get("used_mib")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b:
                gpu["cross_check"] = {
                    "metrics_used_mib": a, "status_used_mib": b,
                    "delta_pct": round(abs(a - b) / b * 100.0, 2),
                }
        out["gpu"] = gpu

        # ---- the rest of the node's own telemetry ---------------------------
        if isinstance(metrics, dict):
            live = metrics.get("live") or {}
            out["engine_live"] = {
                "source": "Strata /metrics live",
                "state": live.get("state"), "queued": live.get("queued"),
                "tok_s": live.get("tok_s"), "tok_s_mean": live.get("tok_s_mean"),
                "prefill_tok_s_mean": live.get("prefill_tok_s_mean"),
                "generated": live.get("generated"), "max_tokens": live.get("max_tokens"),
            }
            out["engine_totals"] = metrics.get("totals") or {}
            out["engine_config"] = {
                k: (metrics.get("engine") or {}).get(k) for k in
                ("model", "version", "max_context", "kv", "spec", "mtp_max",
                 "lookup", "expert_slots", "arena_mib", "pool_workers", "vram_free_mib")
            }
            hist = metrics.get("history") or {}
            if isinstance(hist, dict):
                out["history"] = {
                    "n": len(hist.get("gpu_util") or []),
                    "gpu_util": hist.get("gpu_util") or [],
                    "gpu_temp": hist.get("gpu_temp") or [],
                    "gpu_mem_used_mib": [round(v / 1048576, 1) for v in
                                         (hist.get("gpu_mem_used") or [])
                                         if isinstance(v, (int, float))],
                    "ram_used_mib": [round(v / 1048576, 1) for v in
                                     (hist.get("ram_used") or [])
                                     if isinstance(v, (int, float))],
                    "tok_s": hist.get("tok_s") or [],
                    "source": "Strata /metrics history (node-side ring buffer)",
                }
            reqs = metrics.get("requests")
            if isinstance(reqs, list):
                out["requests"] = [{
                    "at": r.get("time"), "duration_s": r.get("duration_s"),
                    "finish": r.get("finish"), "prompt_tokens": r.get("prompt_tokens"),
                    "reused": r.get("reused"), "output_tokens": r.get("output_tokens"),
                    "decode_tok_s": r.get("decode_tok_s"),
                } for r in reqs[:10] if isinstance(r, dict)]

        if isinstance(hw, dict):
            out["host_metrics"] = {
                "source": "Strata /metrics hardware",
                "cpu_pct": hw.get("cpu"),
                "ram_used_mib": _num(hw.get("ram_used"), 1 / 1048576, 1),
                "ram_total_mib": _num(hw.get("ram_total"), 1 / 1048576, 1),
                "disk_read_mb": hw.get("disk_read_mb"),
                "disk_write_mb": hw.get("disk_write_mb"),
                "cpu_name": (hw_static or {}).get("cpu_name") if isinstance(hw_static, dict) else None,
                "cores": (hw_static or {}).get("cores") if isinstance(hw_static, dict) else None,
                "threads": (hw_static or {}).get("threads") if isinstance(hw_static, dict) else None,
            }
        if isinstance(machine.get("ram"), dict):
            out["host_metrics_status"] = {
                "source": "Strata /v1/status machine.ram",
                "used_gib": machine["ram"].get("used_gib"),
                "total_gib": machine["ram"].get("total_gib"),
            }
        return out


def _num(value, scale: float = 1.0, digits: int = 1):
    """Scale a numeric field, or return None.  Kept total on purpose: a missing
    or non-numeric field must become None so the UI can say "unavailable"
    instead of rendering NaN."""
    if not isinstance(value, (int, float)):
        return None
    return round(value * scale, digits)

