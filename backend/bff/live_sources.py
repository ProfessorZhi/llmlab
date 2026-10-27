"""Fixed, read-only probes for the Dell local-runtime deployment.

The browser never chooses a target.  All URLs below are loopback constants,
all GPU readings come directly from the NVIDIA Management Library, and only a
small allowlisted projection leaves this module.  In particular, Strata's
health response is parsed in memory and its ``api_key`` field is discarded.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from datetime import datetime, timezone
import http.client
import json
import os
import subprocess
from copy import deepcopy
import threading
import time
import re
from urllib.parse import urlsplit

NVML_SOURCE = "nvml-verified"
RUNTIME_SCHEMA = "portal-runtime/1"
RUNTIME_SOURCE = "loopback-runtime-readonly"
MODEL_ALIAS = "strata-vision"
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")

_GPU_METRICS = {
    "gpu_utilization_ratio": "ratio",
    "gpu_temperature_celsius": "celsius",
    "gpu_power_watts": "watts",
    "strata_up": "bool",
    "gpu_memory_used_mib": "mib",
    "gpu_memory_total_mib": "mib",
    "gpu_memory_free_mib": "mib",
    "gpu_memory_reserved_mib": "mib",
}
_SERVICE_URLS = {
    "prometheus": "http://127.0.0.1:9090/-/healthy",
    "gateway": "http://127.0.0.1:8200/healthz",
    "grafana": "http://127.0.0.1:3000/api/health",
    "evalscope": "http://127.0.0.1:9000/health",
}


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemoryV2(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint),
        ("total", ctypes.c_ulonglong),
        ("reserved", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _metric(value: float | int | None, unit: str, sample_at: str, reason: str | None = None) -> dict:
    if value is None:
        return {
            "state": "unknown",
            "value": None,
            "unit": unit,
            "reason": reason or "unreachable",
        }
    return {"state": "known", "value": value, "unit": unit, "sample_at": sample_at}


def _nvml_function(lib, names: tuple[str, ...], argtypes: list, restype=ctypes.c_int):
    for name in names:
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.argtypes = argtypes
            fn.restype = restype
            return fn
    raise AttributeError(names[0])


def read_nvml_metrics() -> dict[str, float | int]:
    """Read max-per-device GPU gauges directly through libnvidia-ml."""
    library = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
    lib = ctypes.CDLL(library)
    init = _nvml_function(lib, ("nvmlInit_v2", "nvmlInit"), [])
    shutdown = _nvml_function(lib, ("nvmlShutdown",), [])
    count_fn = _nvml_function(lib, ("nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"), [ctypes.POINTER(ctypes.c_uint)])
    handle_fn = _nvml_function(
        lib,
        ("nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex"),
        [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
    )
    util_fn = _nvml_function(lib, ("nvmlDeviceGetUtilizationRates",), [ctypes.c_void_p, ctypes.POINTER(_NvmlUtilization)])
    memory_fn = _nvml_function(
        lib,
        ("nvmlDeviceGetMemoryInfo_v2",),
        [ctypes.c_void_p, ctypes.POINTER(_NvmlMemoryV2)],
    )
    temperature_fn = _nvml_function(lib, ("nvmlDeviceGetTemperature",), [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)])
    power_fn = _nvml_function(lib, ("nvmlDeviceGetPowerUsage",), [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)])

    if init() != 0:
        raise RuntimeError("nvml_init_failed")
    try:
        count = ctypes.c_uint()
        if count_fn(ctypes.byref(count)) != 0 or count.value < 1:
            raise RuntimeError("nvml_no_devices")
        samples: dict[str, list[float]] = {
            "gpu_utilization_ratio": [],
            "gpu_temperature_celsius": [],
            "gpu_power_watts": [],
            "gpu_memory_used_mib": [],
            "gpu_memory_total_mib": [],
            "gpu_memory_free_mib": [],
            "gpu_memory_reserved_mib": [],
        }
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if handle_fn(index, ctypes.byref(handle)) != 0:
                continue
            utilization = _NvmlUtilization()
            if util_fn(handle, ctypes.byref(utilization)) == 0:
                samples["gpu_utilization_ratio"].append(utilization.gpu / 100.0)
            memory = _NvmlMemoryV2()
            # NVML versioned structures encode both ABI size and API version.
            memory.version = ctypes.sizeof(_NvmlMemoryV2) | (2 << 24)
            if memory_fn(handle, ctypes.byref(memory)) == 0:
                samples["gpu_memory_used_mib"].append(memory.used / 1048576.0)
                samples["gpu_memory_total_mib"].append(memory.total / 1048576.0)
                samples["gpu_memory_free_mib"].append(memory.free / 1048576.0)
                samples["gpu_memory_reserved_mib"].append(memory.reserved / 1048576.0)
            temperature = ctypes.c_uint()
            if temperature_fn(handle, 0, ctypes.byref(temperature)) == 0:
                samples["gpu_temperature_celsius"].append(float(temperature.value))
            power_mw = ctypes.c_uint()
            if power_fn(handle, ctypes.byref(power_mw)) == 0:
                samples["gpu_power_watts"].append(power_mw.value / 1000.0)

        result = {key: max(values) for key, values in samples.items() if values}
        if not result:
            raise RuntimeError("nvml_no_metrics")
        return result
    finally:
        shutdown()


def _loopback_request(url: str, *, timeout: float = 1.5, max_bytes: int = 65536):
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("only fixed loopback HTTP targets are allowed")
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request("GET", path, headers={"Accept": "application/json"})
        response = conn.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("response_too_large")
        return response.status, body
    finally:
        conn.close()


def _service(url: str, *, checked_at: str) -> dict:
    try:
        status, _ = _loopback_request(url, max_bytes=1024)
    except Exception:
        return {"state": "unknown", "up": None, "reason": "unreachable"}
    return {"state": "known", "up": status == 200, "reason": None, "sample_at": checked_at}


def _strata_health(*, checked_at: str) -> tuple[dict, dict]:
    try:
        status, body = _loopback_request("http://127.0.0.1:8080/health")
    except Exception:
        state = {"state": "unknown", "up": None, "reason": "unreachable"}
        model = {"state": "unknown", "id": None, "vision": None, "reason": "unreachable"}
        return state, model

    engine = {"state": "healthy" if status == 200 else "degraded", "up": status == 200, "checked_at": checked_at}
    if status != 200:
        return engine, {"state": "unknown", "id": None, "vision": None, "reason": "unreachable"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return engine, {"state": "unknown", "id": None, "vision": None, "reason": "invalid_response"}
    if not isinstance(payload, dict):
        return engine, {"state": "unknown", "id": None, "vision": None, "reason": "invalid_response"}

    # Deliberately select only public runtime facts. Never return or log api_key.
    raw_model = payload.get("model")
    model_id = raw_model if isinstance(raw_model, str) and MODEL_ID_PATTERN.fullmatch(raw_model) else None
    loaded = payload.get("loaded")
    state = "loaded" if loaded is True else "unloaded" if loaded is False else "unknown"
    vision = payload.get("images") if isinstance(payload.get("images"), bool) else None
    return engine, {"state": state, "id": model_id, "vision": vision, "checked_at": checked_at}


def _route_readback(*, checked_at: str, model_id: str | None) -> dict:
    try:
        status, body = _loopback_request("http://127.0.0.1:8765/api/gateway", max_bytes=262144)
        payload = json.loads(body.decode("utf-8")) if status == 200 else None
    except Exception:
        payload = None
    gateway = payload.get("gateway") if isinstance(payload, dict) else None
    aliases = gateway.get("aliases") if isinstance(gateway, dict) else None
    if not isinstance(aliases, dict) or not isinstance(model_id, str):
        return {"alias": MODEL_ALIAS, "state": "unknown", "target_model": model_id, "reason": "unreachable"}
    applied = aliases.get(MODEL_ALIAS) == model_id
    return {
        "alias": MODEL_ALIAS,
        "state": "applied" if applied else "not_applied",
        "target_model": model_id,
        "checked_at": checked_at,
    }


class DellLiveSources:
    """The deployed Dell's fixed telemetry and runtime readers."""

    def __init__(self) -> None:
        #: 系统资源是差分量，样本必须跟着一个长期存活的实例走 —— 每次请求
        #: 新建一个 reader 会让所有速率永远停在「第一次采样」。
        self._system = SystemResourceReader()
        # Concurrent viewers share one physical sample. Holding this lock across
        # collection prevents N requests from queuing N minimum sampling windows.
        self._system_lock = threading.Lock()
        self._system_snapshot = None
        self._system_expires = 0.0

    def system_summary(self) -> dict:
        """CPU / 内存 / 交换区 / 磁盘 / 网络 / 按用户 的一份快照。"""
        with self._system_lock:
            if self._system_snapshot is None or time.monotonic() >= self._system_expires:
                snapshot = self._system.summary()
                self._system_snapshot = snapshot
                self._system_expires = time.monotonic() + MIN_SAMPLE_WINDOW_S
            # Keep the original observed_at; reuse must not look like a new sample.
            return deepcopy(self._system_snapshot)

    def host_summary(self) -> dict:
        observed_at = _iso()
        try:
            raw_metrics = read_nvml_metrics()
        except Exception:
            raw_metrics = {}
        metrics = {
            key: _metric(raw_metrics.get(key), unit, observed_at)
            for key, unit in _GPU_METRICS.items()
            if key != "strata_up"
        }
        engine, model = _strata_health(checked_at=observed_at)
        metrics["strata_up"] = _metric(
            1 if engine.get("up") is True else 0 if engine.get("up") is False else None,
            "bool",
            observed_at,
            engine.get("reason"),
        )
        services = {key: _service(url, checked_at=observed_at) for key, url in _SERVICE_URLS.items()}
        services["strata"] = {
            "state": "known" if engine.get("up") is not None else "unknown",
            "up": engine.get("up"),
            "reason": None if engine.get("up") is not None else engine.get("reason", "unreachable"),
        }
        services["strata_model"] = {
            "state": "known" if model.get("state") in ("loaded", "unloaded") else "unknown",
            "up": model.get("state") == "loaded" if model.get("state") in ("loaded", "unloaded") else None,
            "reason": None if model.get("state") in ("loaded", "unloaded") else model.get("reason", "unreachable"),
        }
        return {
            "version": 1,
            "source": NVML_SOURCE,
            "synthetic": False,
            "readonly": True,
            "state": "known" if any(item["state"] == "known" for item in metrics.values()) else "unknown",
            "observed_at": observed_at,
            "metrics": metrics,
            "services": services,
        }

    def runtime_summary(self) -> dict:
        checked_at = _iso()
        engine, model = _strata_health(checked_at=checked_at)
        gateway_status = _service(_SERVICE_URLS["gateway"], checked_at=checked_at)
        gateway_state = "healthy" if gateway_status.get("up") is True else "unreachable" if gateway_status.get("up") is False else "unknown"
        return {
            "schema_version": RUNTIME_SCHEMA,
            "source": RUNTIME_SOURCE,
            "synthetic": False,
            "readonly": True,
            "observed_at": checked_at,
            "engine": {
                "name": "Strata",
                "state": engine.get("state", "unknown"),
                "source": "strata-health-readonly",
                "checked_at": engine.get("checked_at", checked_at),
            },
            "model": {
                "id": model.get("id"),
                "state": model.get("state", "unknown"),
                "vision": model.get("vision"),
                "source": "strata-health-readonly",
                "checked_at": model.get("checked_at", checked_at),
            },
            "gateway": {
                "state": gateway_state,
                "source": "gateway-healthz-readonly",
                "checked_at": gateway_status.get("sample_at", checked_at),
            },
            "route": {
                **_route_readback(checked_at=checked_at, model_id=model.get("id")),
                "source": "gateway-runtime-config-readback",
            },
        }



# ---------------------------------------------------------------------------
# 系统资源：CPU / 内存 / 交换区 / 磁盘读写 / 网络带宽 / 按用户
#
# 为什么直读 /proc 而不是问 Prometheus：这台机器上**没有 node_exporter** ——
# Prometheus 的 activeTargets 只有 `prometheus` 与 `strata` 两个，`node_*`
# 系列一条都不存在。装一个 exporter 要 root，而跑 BFF 的账户没有免密 sudo。
# /proc 的这几张表对任何登录用户可读，所以这条路既不需要提权，也不会因为
# 某个 exporter 掉线而整页变空。
#
# 速率类指标一律是**差分**：进程内保留上一次样本，用两次之间的差值除以真实
# 经过的秒数。第一次调用没有上一次样本时，现场采两次（间隔 SAMPLE_WINDOW_S），
# 而不是报 0 —— 「还没量过」与「量到 0」必须能区分，否则一次采样竞态会被
# 读成「磁盘完全没有写入」。
# ---------------------------------------------------------------------------

SYSTEM_SOURCE = "proc-verified"
SYSTEM_SCHEMA = "host-system/1"

#: 首次调用时两次采样的间隔（秒）。够让 0.5 秒内的计数器噪音被平均掉，
#: 又不至于让一次页面加载多等一秒以上。
SAMPLE_WINDOW_S = 0.5

#: 两次采样之间至少隔这么久，差分才有意义。控制台现在可以每秒刷新：一个标签页
#: 每秒来一次时窗口本来是够的，但两个标签页（或一个页面的两处读数）撞在一起会把
#: 窗口压到几毫秒——那时把计数器的抖动放大成「磁盘忽然 800 MB/s」并不能算作一个
#: 更真的读数。所以窗口太短的这一拍先等到够长再采，而不是照算。
MIN_SAMPLE_WINDOW_S = 0.5

#: 上一次样本超过这个年龄就不再拿来差分：进程被内核换出又唤醒后，
#: 一个 10 分钟前的样本会把这 10 分钟平均进去，得到一个「看起来很平稳」
#: 却对当下毫无意义的数字。
MAX_REUSE_AGE_S = 30.0

_SECTOR_BYTES = 512
_MEMINFO_UNITS = {"kb": 1024, "mb": 1024 * 1024, "b": 1}

#: 只统计真实块设备。loop / ram / dm 是内核与容器内部的东西，把它们算进
#: 「磁盘吞吐」会凭空多出几 MB/s，而那块盘其实没转。
_PSEUDO_DISK_PREFIXES = ("loop", "ram", "dm-", "zram", "sr")


def _proc_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _read_cpu_times() -> dict[str, int] | None:
    """``/proc/stat`` 第一行：``cpu user nice system idle iowait irq softirq steal``。"""
    for line in _proc_text("/proc/stat").splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
        try:
            values = [int(x) for x in parts[1:9]]
        except ValueError:
            return None
        out = dict(zip(names, values))
        out["total"] = sum(values)
        # iowait 算「不忙」：CPU 在等磁盘时不能算成计算负载，否则一次慢 I/O
        # 会显示成 CPU 打满，把排查引到错误的方向。
        out["busy"] = out["total"] - out["idle"] - out["iowait"]
        return out
    return None


def _read_meminfo() -> dict[str, int]:
    """``/proc/meminfo`` 的字节值（原表以 kB 计）。"""
    out: dict[str, int] = {}
    for line in _proc_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        unit = _MEMINFO_UNITS.get(parts[1].lower(), 1) if len(parts) > 1 else 1
        out[key.strip()] = value * unit
    return out


def _read_diskstats() -> dict[str, dict[str, int]]:
    """``/proc/diskstats`` → 每块真实盘的累计扇区数与累计 I/O 毫秒。

    字段位置取自内核文档（14 字段版）：``[2]=名字 [5]=读扇区 [9]=写扇区
    [12]=正在做的 I/O 毫秒``。
    """
    out: dict[str, dict[str, int]] = {}
    for line in _proc_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if name.startswith(_PSEUDO_DISK_PREFIXES):
            continue
        # 分区也会出现在这张表里。把 nvme0n1 与 nvme0n1p1 一起加总，同一批
        # 字节会被算两遍，磁盘吞吐凭空翻倍。只有出现在 /sys/block 下的才是
        # 整盘，分区在 /sys/class/block 下。
        sysdir = "/sys/block/" + name
        if not os.path.exists(sysdir):
            continue
        # 大小为 0 的不是存储设备（这台机上的 sdc 是空的 SD 读卡器）。
        # 它永远读写 0 字节，留在列表里只会让「哪块盘在忙」更难读。
        try:
            with open(sysdir + "/size") as fh:
                if int(fh.read().strip()) <= 0:
                    continue
        except (OSError, ValueError):
            pass
        try:
            out[name] = {
                "read_sectors": int(parts[5]),
                "write_sectors": int(parts[9]),
                "io_ms": int(parts[12]),
            }
        except ValueError:
            continue
    return out


def _read_netdev() -> dict[str, dict[str, int]]:
    """``/proc/net/dev`` → 每块网卡的累计收发字节。``lo`` 不计入对外带宽。"""
    out: dict[str, dict[str, int]] = {}
    for line in _proc_text("/proc/net/dev").splitlines():
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name or name == "lo":
            continue
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            out[name] = {"rx_bytes": int(parts[0]), "tx_bytes": int(parts[8])}
        except ValueError:
            continue
    return out


#: 单次 nvidia-smi 调用的超时。驱动偶发卡住时，不能让整个 BFF 请求跟着挂。
_NVIDIA_SMI_TIMEOUT_S = 4.0


def _pid_uid_and_unit(pid: int) -> tuple[int | None, str | None]:
    """``/proc/<pid>`` 里能跨用户读到的两样东西：属主 UID 与所属单元名。"""
    uid = None
    try:
        with open("/proc/%d/status" % pid, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    break
    except OSError:
        pass
    unit = None
    try:
        with open("/proc/%d/cgroup" % pid, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                seg = line.strip().rsplit("/", 1)[-1]
                if seg.endswith(".service") or seg.endswith(".scope"):
                    unit = seg
    except OSError:
        pass
    if unit is None:
        try:
            unit = _proc_text("/proc/%d/comm" % pid).strip() or None
        except OSError:
            unit = None
    return uid, unit


def _read_gpu_processes() -> list[dict] | None:
    """按进程的显存占用。

    返回 ``None`` = **没读到**（没驱动 / 没权限 / 超时）；返回 ``[]`` = 读到了，
    卡上确实没有计算进程。这两种情况在界面上不能显示成同一件事。
    """
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_NVIDIA_SMI_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out: list[dict] = []
    for line in done.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        try:
            used_mib = int(float(parts[1]))
        except ValueError:
            continue
        pid = int(parts[0])
        uid, unit = _pid_uid_and_unit(pid)
        out.append({
            "pid": pid,
            "used_mib": used_mib,
            "uid": uid,
            "user": _uid_name(uid) if uid is not None else None,
            "unit": unit,
        })
    out.sort(key=lambda item: item["used_mib"], reverse=True)
    return out


#: 只统计真实文件系统；伪文件系统的「容量」是内核的记账，不是磁盘空间。
_REAL_FS = ("ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "ntfs", "ntfs3", "zfs", "fuse.sshfs")

#: 引擎 exporter 的地址与超时。它和 BFF 在同一台机上，回环连接；
#: 2 秒足够，且绝不能因为它卡住就让整页等下去。
ENGINE_METRICS_URL = "http://127.0.0.1:9101/metrics"
ENGINE_TIMEOUT_S = 2.0

#: 从引擎 exporter 里取哪些指标。白名单而不透传：exporter 加一个新指标不应
#: 自动出现在前端，那会让「页面上出现的东西」失去可追溯性。
_ENGINE_WANTED = (
    "requests_total", "requests_kept",
    "prompt_tokens_total", "output_tokens_total",
    "cached_prompt_tokens_total", "cache_last_prompt_tokens", "cache_last_reused_tokens",
    "tokens_per_second", "tokens_per_second_mean", "prefill_tokens_per_second_mean",
    "prompt_seconds_total", "decode_seconds_total",
    "speculative_drafts_offered_total", "speculative_drafts_accepted_total",
    "gpu_utilization_ratio", "gpu_power_watts", "gpu_temperature_celsius",
)

#: `/proc/net/tcp` 的连接状态码 → 人读。
_TCP_STATES = {
    "01": "established", "02": "syn_sent", "03": "syn_recv", "04": "fin_wait1",
    "05": "fin_wait2", "06": "time_wait", "07": "close", "08": "close_wait",
    "09": "last_ack", "0A": "listen", "0B": "closing",
}


def _read_disk_usage() -> list[dict]:
    """每个真实挂载点的容量。

    单独成一块而不是塞进 ``disk.devices``：设备（nvme0n1）与文件系统
    （/、/data）不是一一对应 —— LVM 一个卷组能挂好几个地方，反过来一块盘
    也能分好几个区。把它们混在一张表里，读者会去对一个不存在的对应关系。
    """
    out: list[dict] = []
    seen: set[str] = set()
    try:
        mounts = _proc_text("/proc/mounts").splitlines()
    except OSError:
        return out
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[0], parts[1], parts[2]
        if fstype not in _REAL_FS or not source.startswith("/dev/"):
            continue
        if target in seen:
            continue
        try:
            st = os.statvfs(target)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        seen.add(target)
        out.append({
            "mount": target,
            "device": source,
            "fstype": fstype,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "used_ratio": round(used / total, 4) if total else None,
        })
    out.sort(key=lambda item: item["total_bytes"], reverse=True)
    return out


def _read_thermal() -> dict:
    """温度与风扇。取 ``/sys/class/hwmon`` 下的真实传感器。

    CPU 温度取同一颗芯片上所有 core 的**最大值**：平均值会把一个过热的核心
    摊平掉，而「有一个核 95 ℃」正是要报警的那件事。
    """
    cpus: list[float] = []
    disks: list[float] = []
    fans: list[int] = []
    base = "/sys/class/hwmon"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return {"cpu_c": None, "nvme_c": None, "fans_rpm": [], "sensors": []}
    sensors: list[dict] = []
    for entry in entries:
        root = os.path.join(base, entry)
        try:
            chip = _proc_text(os.path.join(root, "name")).strip()
        except OSError:
            continue
        temps: list[float] = []
        for name in sorted(os.listdir(root)):
            if name.startswith("temp") and name.endswith("_input"):
                try:
                    temps.append(int(_proc_text(os.path.join(root, name)).strip()) / 1000.0)
                except (OSError, ValueError):
                    continue
            elif name.startswith("fan") and name.endswith("_input"):
                try:
                    fans.append(int(_proc_text(os.path.join(root, name)).strip()))
                except (OSError, ValueError):
                    continue
        if temps:
            sensors.append({"chip": chip, "max_c": round(max(temps), 1)})
        if chip == "coretemp":
            cpus.extend(temps)
        elif chip == "nvme":
            disks.extend(temps)
    return {
        "cpu_c": round(max(cpus), 1) if cpus else None,
        "nvme_c": round(max(disks), 1) if disks else None,
        "fans_rpm": fans,
        "sensors": sensors,
    }


def _read_net_connections() -> dict:
    """按状态统计 TCP 连接数（IPv4 + IPv6）。

    这不是带宽，是「有多少人在连着」：一个 LISTEN 都没有说明服务没起来，
    ESTABLISHED 暴涨则说明有人在压。带宽面板回答不了这两个问题。
    """
    counts: dict[str, int] = {}
    for name in ("tcp", "tcp6"):
        try:
            lines = _proc_text("/proc/net/" + name).splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            state = _TCP_STATES.get(parts[3].upper())
            if state is None:
                continue
            counts[state] = counts.get(state, 0) + 1
    return {
        "listening": counts.get("listen", 0),
        "established": counts.get("established", 0),
        "time_wait": counts.get("time_wait", 0),
        "total": sum(counts.values()),
    }


def _read_engine_metrics() -> dict:
    """引擎 exporter 的即时读数。

    读不到就回 ``reachable: False`` 并保持其余字段为 ``None`` —— 引擎停了和
    「引擎在跑但没人用」是两件完全不同的事，不能用同一排 0 表示。
    """
    import urllib.request

    try:
        with urllib.request.urlopen(ENGINE_METRICS_URL, timeout=ENGINE_TIMEOUT_S) as resp:
            body = resp.read(256 * 1024).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 连不上就是连不上
        return {"reachable": False, "source": "strata-exporter", "values": {}}
    values: dict[str, float] = {}
    info: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith("llmlab_strata_info{"):
            for pair in line[line.find("{") + 1 : line.rfind("}")].split(","):
                key, _, val = pair.partition("=")
                info[key.strip()] = val.strip().strip('"')
            continue
        name, _, rest = line.partition(" ")
        if not name.startswith("llmlab_strata_"):
            continue
        short = name[len("llmlab_strata_"):]
        if short not in _ENGINE_WANTED:
            continue
        try:
            values[short] = float(rest.strip())
        except ValueError:
            continue
    return {
        "reachable": True,
        "source": "strata-exporter",
        "info": info,
        "values": values,
    }


def _read_process_table() -> dict[int, dict[str, int]]:
    """按 UID 汇总：进程数、常驻内存、累计 CPU 时间。

    跨用户读别的进程只能拿到 ``/proc/<pid>/status`` 与 ``stat``（这两个文件
    对任何用户可读），拿不到 ``/proc/<pid>/io`` —— 所以**按用户的磁盘读写
    是这份数据拿不到的那一项**，界面上必须如实标缺，不能补一个 0。
    """
    out: dict[int, dict[str, int]] = {}
    ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        base = "/proc/" + entry
        try:
            with open(base + "/status", "r", encoding="utf-8", errors="replace") as fh:
                uid = None
                rss_kib = 0
                for line in fh:
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])
                    elif line.startswith("VmRSS:"):
                        rss_kib = int(line.split()[1])
                    elif uid is not None and line.startswith("VmRSS:"):
                        break
            if uid is None:
                continue
            with open(base + "/stat", "r", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
            # comm 字段被括号包着且可能含空格，必须从最后一个 ')' 之后切。
            tail = stat[stat.rfind(")") + 2:].split()
            cpu_ticks = int(tail[11]) + int(tail[12])
        except (OSError, ValueError, IndexError):
            continue
        slot = out.setdefault(uid, {"processes": 0, "rss_bytes": 0, "cpu_ticks": 0})
        slot["processes"] += 1
        slot["rss_bytes"] += rss_kib * 1024
        slot["cpu_ticks"] += cpu_ticks
    out["_ticks_per_second"] = ticks  # type: ignore[assignment]
    return out


#: 系统服务账号的 UID 上界：0–999 由发行版或软件包创建（root、polkitd、
#: messagebus、avahi、_chrony…），65534 是 nobody。
#:
#: 「按用户」这张表归并的是**进程的所属账号**，不是「谁在用机器」。不分开看，
#: 一台单人开发机会显示成「17 个用户」—— 那不是数据错，是标签错。
_SYSTEM_UID_MAX = 1000
_NOBODY_UID = 65534


def _account_kind(uid: int) -> str:
    """``human`` 还是 ``system``。判断只依据 UID，绝不依据用户名像不像人名。"""
    if uid == _NOBODY_UID or uid < _SYSTEM_UID_MAX:
        return "system"
    return "human"


def _uid_name(uid: int) -> str | None:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return None


def _mb(value: float | int | None) -> float | None:
    return None if value is None else round(float(value) / 1048576.0, 1)


def _rate(current: float, previous: float, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    delta = current - previous
    if delta < 0:  # 计数器回绕或设备被重置
        return None
    return delta / seconds


class SystemResourceReader:
    """``/proc`` 的差分读取器。保留上一次样本，所以必须由**长期存活的**进程持有。"""

    def __init__(self) -> None:
        self._prev: dict | None = None
        self._prev_at: float | None = None
        #: BFF 是多线程的：两个页面的读数会并发走到这里。上一样本「读—算—写」
        #: 必须是同一件事，否则两个线程会各自拿同一份上一样本、各自写回自己那份，
        #: 结果是其中一次的窗口被另一条请求改写。
        self._lock = threading.Lock()

    # -- 采样 --------------------------------------------------------------

    def _sample(self) -> dict:
        return {
            "at": time.monotonic(),
            "cpu": _read_cpu_times(),
            "mem": _read_meminfo(),
            "disk": _read_diskstats(),
            "net": _read_netdev(),
            "procs": _read_process_table(),
        }

    def _snapshot_pair(self) -> tuple[dict, dict, float]:
        """返回 ``(上一样本, 当前样本, 经过秒数)``。没有可用上一样本时现场采两次。"""
        with self._lock:
            now = time.monotonic()
            if self._prev is not None and self._prev_at is not None:
                age = now - self._prev_at
                if 0 < age < MIN_SAMPLE_WINDOW_S:
                    # 窗口太短：等够再采，而不是拿几毫秒的差值当速率。
                    time.sleep(MIN_SAMPLE_WINDOW_S - age)
                    now = time.monotonic()
                    age = now - self._prev_at
                if 0 < age <= MAX_REUSE_AGE_S:
                    previous = self._prev
                    current = self._sample()
                    self._prev, self._prev_at = current, current["at"]
                    # 必须返回**两份不同的样本**：拿同一份当 `before` 与 `after`
                    # 会让所有差值恒为 0，页面上表现为「磁盘和网卡一直没动静」。
                    return previous, current, age
            first = self._sample()
            time.sleep(SAMPLE_WINDOW_S)
            second = self._sample()
            self._prev, self._prev_at = second, second["at"]
            return first, second, max(second["at"] - first["at"], 1e-6)

    # -- 对外 --------------------------------------------------------------

    def summary(self) -> dict:
        before, after, window = self._snapshot_pair()
        observed_at = _iso()
        notes: list[str] = []

        # --- CPU ---
        cpu_block: dict = {"cores": os.cpu_count(), "utilization_ratio": None, "iowait_ratio": None}
        prev_cpu, cur_cpu = before.get("cpu"), after.get("cpu")
        if prev_cpu and cur_cpu:
            total = cur_cpu["total"] - prev_cpu["total"]
            if total > 0:
                cpu_block["utilization_ratio"] = round((cur_cpu["busy"] - prev_cpu["busy"]) / total, 4)
                cpu_block["iowait_ratio"] = round((cur_cpu["iowait"] - prev_cpu["iowait"]) / total, 4)
        try:
            load1, load5, load15 = os.getloadavg()
            cpu_block.update({"load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2)})
        except OSError:
            cpu_block.update({"load1": None, "load5": None, "load15": None})

        # --- 内存与交换区 ---
        prev_mem = before.get("mem") or {}
        mem = after.get("mem") or prev_mem
        total = mem.get("MemTotal") or None
        available = mem.get("MemAvailable") or None
        swap_total = mem.get("SwapTotal") or 0
        swap_free = mem.get("SwapFree") or 0
        memory_block = {
            "total_mib": _mb(total),
            "available_mib": _mb(available),
            "used_mib": _mb(total - available) if total and available is not None else None,
            "used_ratio": round(1 - available / total, 4) if total and available is not None else None,
            "swap_total_mib": _mb(swap_total),
            "swap_used_mib": _mb(swap_total - swap_free),
            "swap_used_ratio": round((swap_total - swap_free) / swap_total, 4) if swap_total else None,
        }

        # --- 磁盘 ---
        prev_disk = before.get("disk") or {}
        cur_disk = after.get("disk") or {}
        disk_devices = []
        read_bps = write_bps = 0.0
        io_busy_ms = 0.0
        for name, cur in sorted(cur_disk.items()):
            prev = prev_disk.get(name)
            if prev is None:
                continue
            r = _rate(cur["read_sectors"], prev["read_sectors"], window)
            w = _rate(cur["write_sectors"], prev["write_sectors"], window)
            busy = _rate(cur["io_ms"], prev["io_ms"], window * 1000.0)
            if r is None or w is None:
                continue
            r_bytes = r * _SECTOR_BYTES
            w_bytes = w * _SECTOR_BYTES
            read_bps += r_bytes
            write_bps += w_bytes
            if busy is not None:
                io_busy_ms += busy
            disk_devices.append({
                "name": name,
                "read_bytes_per_s": round(r_bytes),
                "write_bytes_per_s": round(w_bytes),
                "busy_ratio": None if busy is None else round(min(busy, 1.0), 4),
            })
        disk_block = {
            "devices": disk_devices,
            "read_bytes_per_s": round(read_bps),
            "write_bytes_per_s": round(write_bps),
            # 多块盘同时忙时这个数会超过 1：它是「设备忙时间之和」，不是
            # 「机器有多少时间在等磁盘」。要判断后者请看单个设备。
            "busy_ratio_sum": round(io_busy_ms / (window * 1000.0), 4) if io_busy_ms else 0.0,
        }

        # --- 网络 ---
        prev_net = before.get("net") or {}
        cur_net = after.get("net") or {}
        interfaces = []
        rx_total = tx_total = 0.0
        for name, cur in sorted(cur_net.items()):
            prev = prev_net.get(name)
            if prev is None:
                continue
            rx = _rate(cur["rx_bytes"], prev["rx_bytes"], window)
            tx = _rate(cur["tx_bytes"], prev["tx_bytes"], window)
            if rx is None or tx is None:
                continue
            rx_total += rx
            tx_total += tx
            interfaces.append({
                "name": name,
                "rx_bytes_per_s": round(rx),
                "tx_bytes_per_s": round(tx),
            })
        network_block = {
            "interfaces": interfaces,
            "rx_bytes_per_s": round(rx_total),
            "tx_bytes_per_s": round(tx_total),
        }

        # --- 按用户 ---
        prev_procs = before.get("procs") or {}
        cur_procs = after.get("procs") or {}
        ticks_per_second = float(cur_procs.get("_ticks_per_second") or prev_procs.get("_ticks_per_second") or 100)
        # --- 显存归因（按进程，再汇到用户） ---
        gpu_procs = _read_gpu_processes()
        gpu_by_uid: dict[int, int] = {}
        if gpu_procs:
            for item in gpu_procs:
                if item.get("uid") is None:
                    continue
                gpu_by_uid[item["uid"]] = gpu_by_uid.get(item["uid"], 0) + item["used_mib"]
        if gpu_procs is None:
            notes.append("显存按进程归因不可得：nvidia-smi 未返回按进程清单。")

        # --- 容量 / 温度 / 连接 / 引擎吞吐 ---
        disk_usage = _read_disk_usage()
        thermal = _read_thermal()
        connections = _read_net_connections()
        engine = _read_engine_metrics()

        per_user = []
        for uid, cur in cur_procs.items():
            if not isinstance(uid, int):
                continue
            prev = prev_procs.get(uid)
            cpu_ratio = None
            if prev is not None:
                delta_ticks = cur["cpu_ticks"] - prev["cpu_ticks"]
                if delta_ticks >= 0 and window > 0:
                    cpu_ratio = round(delta_ticks / ticks_per_second / window, 4)
            per_user.append({
                "uid": uid,
                "user": _uid_name(uid),
                # 人类账户 vs 系统服务账号。前端据此默认折叠后者 —— 数据一条不少，
                # 只是不让守护进程账号冒充「机器上有十几个用户」。
                "account": _account_kind(uid),
                "processes": cur["processes"],
                "memory_mib": _mb(cur["rss_bytes"]),
                "cpu_ratio": cpu_ratio,
                # 该用户所有计算进程的显存合计；没有占用就是 None（不是 0）。
                "vram_mib": gpu_by_uid.get(uid) if gpu_procs else None,
            })
        per_user.sort(key=lambda item: (item["memory_mib"] or 0), reverse=True)
        notes.append("per-user 磁盘读写不可得：跨用户读 /proc/<pid>/io 需要 root。")

        return {
            "schema": SYSTEM_SCHEMA,
            "source": SYSTEM_SOURCE,
            "synthetic": False,
            "readonly": True,
            "observed_at": observed_at,
            "window_seconds": round(window, 3),
            "cpu": cpu_block,
            "memory": memory_block,
            "disk": {**disk_block, "usage": disk_usage},
            "thermal": thermal,
            "connections": connections,
            "engine": engine,
            "network": network_block,
            "per_user": per_user,
            "gpu_processes": gpu_procs,
            "notes": notes,
        }

__all__ = ["DellLiveSources", "NVML_SOURCE", "RUNTIME_SCHEMA", "RUNTIME_SOURCE",
           "SYSTEM_SCHEMA", "SYSTEM_SOURCE", "SystemResourceReader",
           "read_nvml_metrics"]
