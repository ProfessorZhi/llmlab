"""推理服务参数：每个框架一张参数表（schema），控制台按表渲染表单。

不同推理引擎的参数各不相同（llama.cpp/Strata 的 ``--ctx-size`` / ``--parallel``，
vLLM 的 ``--max-model-len`` / ``--max-num-seqs``，SGLang 的 ``--context-length`` …），
所以参数不写死在前端：每个框架在 :data:`SCHEMAS` 里声明自己的字段、类型、范围、
命令行开关，以及能从运行中的服务**只读**测出哪些当前值。

流程：

1. **实测**：从运行中的服务只读取当前值（llama.cpp 系 ``GET /props``，vLLM
   ``GET /v1/models`` 的 ``max_model_len``）。测不到就是 ``None``，不猜。
2. **目标配置**：服务所有者或管理员保存一份目标值（只存在平台里）。
3. **生效**：平台目前不替别人重启进程（那需要受限节点执行器，另一阶段的事），
   所以这里生成对应的启动命令；所有者在运行用户下重启服务后，实测值与目标一致，
   状态自动变成「已生效」。
"""

from __future__ import annotations

import http.client
import json
import shlex
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --- 参数表 ------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str  # int | float | bool | choice | text
    flag: str
    group: str = "资源与并发"
    default: object = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None
    help: str = ""
    observe: str | None = None  # 能实测时，实测值在观测结果里的名字

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "kind": self.kind, "flag": self.flag, "group": self.group,
            "default": self.default, "min": self.minimum, "max": self.maximum, "choices": list(self.choices),
            "unit": self.unit, "help": self.help, "observable": self.observe is not None,
        }


LLAMA_CPP_PARAMS = (
    Param("ctx_size", "上下文长度", "int", "--ctx-size", default=8192, minimum=512, maximum=262144, unit="tokens",
          help="所有并发槽位共享这个总长度；每路可用上下文 ≈ 上下文长度 ÷ 并发路数。", observe="ctx_size"),
    Param("parallel", "并发路数", "int", "--parallel", default=4, minimum=1, maximum=64, unit="路",
          help="同时处理的请求数（槽位数）。越多吞吐越高，每路可用上下文越短。", observe="parallel"),
    Param("n_gpu_layers", "GPU 层数", "int", "--n-gpu-layers", default=999, minimum=0, maximum=999,
          help="放进显存的层数；999 表示全部。"),
    Param("batch_size", "批大小", "int", "--batch-size", default=2048, minimum=32, maximum=16384, group="性能"),
    Param("flash_attn", "Flash Attention", "bool", "--flash-attn", default=True, group="性能"),
    Param("cache_type_k", "KV 缓存精度", "choice", "--cache-type-k", default="f16",
          choices=("f16", "q8_0", "q4_0"), group="性能", help="降低精度可以省显存、换更长上下文。"),
)

VLLM_PARAMS = (
    Param("max_model_len", "上下文长度", "int", "--max-model-len", default=8192, minimum=512, maximum=1048576,
          unit="tokens", observe="max_model_len"),
    Param("max_num_seqs", "最大并发序列", "int", "--max-num-seqs", default=64, minimum=1, maximum=1024, unit="路"),
    Param("gpu_memory_utilization", "显存占用比例", "float", "--gpu-memory-utilization", default=0.9,
          minimum=0.1, maximum=0.98, help="vLLM 预先占用的显存比例；和别的服务共用这张卡时要调低。"),
    Param("tensor_parallel_size", "张量并行", "int", "--tensor-parallel-size", default=1, minimum=1, maximum=8),
    Param("dtype", "数据类型", "choice", "--dtype", default="auto", choices=("auto", "bfloat16", "float16"), group="性能"),
    Param("enable_prefix_caching", "前缀缓存", "bool", "--enable-prefix-caching", default=True, group="性能"),
    Param("kv_cache_dtype", "KV 缓存精度", "choice", "--kv-cache-dtype", default="auto",
          choices=("auto", "fp8"), group="性能"),
)

SGLANG_PARAMS = (
    Param("context_length", "上下文长度", "int", "--context-length", default=8192, minimum=512, maximum=1048576,
          unit="tokens", observe="max_model_len"),
    Param("max_running_requests", "最大并发请求", "int", "--max-running-requests", default=64, minimum=1,
          maximum=1024, unit="路"),
    Param("mem_fraction_static", "静态显存比例", "float", "--mem-fraction-static", default=0.85,
          minimum=0.1, maximum=0.95),
    Param("tp_size", "张量并行", "int", "--tp-size", default=1, minimum=1, maximum=8),
    Param("chunked_prefill_size", "分块预填充", "int", "--chunked-prefill-size", default=8192, minimum=512,
          maximum=65536, group="性能"),
)

OLLAMA_PARAMS = (
    Param("num_ctx", "上下文长度", "int", "OLLAMA_CONTEXT_LENGTH", default=4096, minimum=512, maximum=262144,
          unit="tokens", help="以环境变量设置。"),
    Param("num_parallel", "并发路数", "int", "OLLAMA_NUM_PARALLEL", default=1, minimum=1, maximum=32, unit="路"),
    Param("keep_alive", "空闲保留", "text", "OLLAMA_KEEP_ALIVE", default="5m", help="空闲多久后卸载模型，例如 5m、1h、-1。"),
)

SCHEMAS: dict[str, dict] = {
    "strata": {"label": "Strata", "style": "flags", "binary": "strata serve", "params": LLAMA_CPP_PARAMS,
               "observe": "llama_props"},
    "llama.cpp": {"label": "llama.cpp", "style": "flags", "binary": "llama-server", "params": LLAMA_CPP_PARAMS,
                  "observe": "llama_props"},
    "vllm": {"label": "vLLM", "style": "flags", "binary": "vllm serve <model>", "params": VLLM_PARAMS,
             "observe": "openai_models"},
    "sglang": {"label": "SGLang", "style": "flags", "binary": "python -m sglang.launch_server", "params": SGLANG_PARAMS,
               "observe": "openai_models"},
    "ollama": {"label": "Ollama", "style": "env", "binary": "ollama serve", "params": OLLAMA_PARAMS, "observe": None},
}


def schema_for(framework: str | None) -> dict | None:
    return SCHEMAS.get(framework or "")


def validate(framework: str, values: object) -> dict:
    schema = schema_for(framework)
    if schema is None:
        raise ValueError(f"no parameter schema for framework {framework!r}")
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    known = {p.key: p for p in schema["params"]}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(unknown)}")
    out: dict = {}
    for key, raw in values.items():
        p = known[key]
        if raw is None:
            continue
        if p.kind == "int":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
                raise ValueError(f"{p.label} must be an integer")
            value = int(raw)
        elif p.kind == "float":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"{p.label} must be a number")
            value = float(raw)
        elif p.kind == "bool":
            if not isinstance(raw, bool):
                raise ValueError(f"{p.label} must be true or false")
            value = raw
        elif p.kind == "choice":
            if raw not in p.choices:
                raise ValueError(f"{p.label} must be one of {', '.join(p.choices)}")
            value = raw
        else:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 64 or any(c in raw for c in "\n\r;&|`$"):
                raise ValueError(f"{p.label} is not a safe value")
            value = raw.strip()
        if p.minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value < p.minimum:
            raise ValueError(f"{p.label} must be at least {p.minimum:g}")
        if p.maximum is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and value > p.maximum:
            raise ValueError(f"{p.label} must be at most {p.maximum:g}")
        out[key] = value
    return out


def render_command(framework: str, values: dict, *, port: int | None) -> str:
    """目标配置 ⇒ 可以直接粘贴的启动命令（只含参数表里的开关，值都经过 :func:`validate`）。"""
    schema = schema_for(framework)
    if schema is None:
        return ""
    if schema["style"] == "env":
        env = " ".join(f"{p.flag}={shlex.quote(str(values[p.key]))}" for p in schema["params"] if p.key in values)
        return f"{env} {schema['binary']}".strip()
    parts = [schema["binary"]]
    for p in schema["params"]:
        if p.key not in values:
            continue
        value = values[p.key]
        if p.kind == "bool":
            if value:
                parts.append(p.flag)
        else:
            parts.append(f"{p.flag} {shlex.quote(str(value))}")
    if port:
        parts.append(f"--port {int(port)}")
    return " ".join(parts)


# --- 实测 --------------------------------------------------------------------

Fetch = Callable[[str, int, str], "tuple[int, bytes] | None"]


def loopback_fetch(host: str, port: int, path: str):
    if host not in ("127.0.0.1", "::1", "localhost"):
        return None
    connection = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        return response.status, response.read(256 * 1024)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def observe(framework: str, host: str, port: int, *, fetch: Fetch = loopback_fetch,
            headers_key: str | None = None) -> dict:
    """只读测当前值。返回 ``{"reachable": bool, "values": {...}}``；测不到的键不出现。"""
    schema = schema_for(framework)
    kind = schema["observe"] if schema else None
    if kind is None:
        return {"reachable": None, "values": {}}
    if kind == "llama_props":
        answer = fetch(host, port, "/props")
        if answer is None:
            return {"reachable": False, "values": {}}
        try:
            props = json.loads(answer[1])
        except (ValueError, TypeError):
            return {"reachable": True, "values": {}}
        values = {}
        settings = props.get("default_generation_settings") if isinstance(props, dict) else None
        n_ctx = (settings or {}).get("n_ctx") if isinstance(settings, dict) else None
        slots = props.get("total_slots") if isinstance(props, dict) else None
        if isinstance(slots, int):
            values["parallel"] = slots
        if isinstance(n_ctx, int):
            # llama.cpp 报的是**每个槽位**的上下文；``--ctx-size`` 是总数 = 每路 × 路数。
            values["ctx_per_slot"] = n_ctx
            if isinstance(slots, int):
                values["ctx_size"] = n_ctx * slots
        return {"reachable": True, "values": values}
    answer = fetch(host, port, "/v1/models")
    if answer is None:
        return {"reachable": False, "values": {}}
    try:
        data = json.loads(answer[1]).get("data") or []
    except (ValueError, TypeError, AttributeError):
        return {"reachable": True, "values": {}}
    length = data[0].get("max_model_len") if data and isinstance(data[0], dict) else None
    return {"reachable": True, "values": {"max_model_len": length} if isinstance(length, int) else {}}


# --- 目标配置 ----------------------------------------------------------------


class ParamStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS engine_params (engine_id TEXT PRIMARY KEY, framework TEXT NOT NULL, "
                "values_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
        finally:
            conn.close()

    def get(self, engine_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM engine_params WHERE engine_id = ?", (engine_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"framework": row["framework"], "values": json.loads(row["values_json"]),
                "updated_by": row["updated_by"], "updated_at": row["updated_at"]}

    def put(self, engine_id: str, framework: str, values: dict, updated_by: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO engine_params (engine_id, framework, values_json, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(engine_id) DO UPDATE SET framework = excluded.framework, "
                "values_json = excluded.values_json, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (engine_id, framework, json.dumps(values), updated_by, self._clock()),
            )
        finally:
            conn.close()


def compare(desired: dict, observed: dict, schema: dict) -> str:
    """``applied`` / ``pending`` / ``unverifiable`` / ``none``。只比能实测的键。"""
    if not desired:
        return "none"
    checks = [(p.key, p.observe) for p in schema["params"] if p.observe and p.key in desired]
    if not checks:
        return "unverifiable"
    for key, observed_key in checks:
        if observed.get(observed_key) != desired[key]:
            return "pending"
    return "applied"
