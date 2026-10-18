"""受限主机指标采集：CPU / 内存 / 磁盘 / GPU 物理读数 / 驱动状态。

**生产不启用。** :class:`ProcHostCollector` 的 ``enabled`` 默认 ``False``，此时每一项
都如实产出 ``quality=unknown, reason=not_collected``，**不**产出 0。本任务不部署采集器，
所以「未启用」是默认形态而非降级形态。

**本模块永不使用 GPU。** NVML 句柄必须由外部**注入**（:class:`NvmlGpuReader` 的
``handle``），模块自己**不** import ``pynvml``。这样任何一次测试运行都不可能意外加载
驱动或占用设备 —— 也就不可能违反「本任务不得使用 GPU」的资源约束。

解析全部拆成**纯函数**（``parse_meminfo`` / ``parse_proc_stat`` / ``parse_nvidia_version`` …），
于是它们能用合成文本充分测试，而不必依赖运行这台机器的实际状态。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .dto import (
    DRIVER_STATES,
    QUALITY_MEASURED,
    QUALITY_UNKNOWN,
    REASON_NOT_COLLECTED,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    REASON_UNSUPPORTED,
    MetricReading,
    StateReading,
    TextReading,
)

#: 主机采集的来源标识。写进每一条读数的 ``source``。
HOST_SOURCE = "host-proc"

#: ``/proc`` 与设备文件的默认位置。测试可覆写。
DEFAULT_PROC_ROOT = "/proc"
DEFAULT_NVIDIA_VERSION_PATH = "/proc/driver/nvidia/version"
DEFAULT_DISK_MOUNT = "/"

#: 首次采集时 CPU 使用率必然算不出来（只有一个计数器快照）。
#: 这不是缺陷，是「一个样本算不出速率」—— 用 ``no_sample`` 如实表达。


@dataclass(frozen=True)
class CpuCounters:
    """``/proc/stat`` 第一行的累加计数器（jiffies）。"""

    user: int
    nice: int
    system: int
    idle: int
    iowait: int
    irq: int
    softirq: int
    steal: int

    @property
    def total(self) -> int:
        return (
            self.user
            + self.nice
            + self.system
            + self.idle
            + self.iowait
            + self.irq
            + self.softirq
            + self.steal
        )

    @property
    def busy(self) -> int:
        return self.total - self.idle - self.iowait


# ---------------------------------------------------------------------------
# 纯解析函数
# ---------------------------------------------------------------------------

_MEMINFO_UNITS = {
    "kb": 1024,
    "mb": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "b": 1,
}


def parse_meminfo(text: str, *, keys: Iterable[str] = ()) -> dict[str, int]:
    """解析 ``/proc/meminfo`` 成 ``{键: 字节}``。

    Linux 惯例是 ``kB``，但**不假定**：逐行读单位。认不出的单位整行丢弃
    （宁可少一项，也不要一个乘以错系数的数字）。
    """
    wanted = set(keys)
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if wanted and name not in wanted:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].strip().lower() if len(parts) > 1 else "b"
        multiplier = _MEMINFO_UNITS.get(unit)
        if multiplier is None:
            continue
        if amount < 0:
            continue
        result[name] = amount * multiplier
    return result


def parse_proc_stat(text: str) -> CpuCounters | None:
    """解析 ``/proc/stat`` 的第一行 ``cpu ...``。认不出返回 ``None``。"""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()
        try:
            numbers = [int(value) for value in fields[1:9]]
        except ValueError:
            return None
        if len(numbers) < 8:
            return None
        user, nice, system, idle, iowait, irq, softirq, steal = numbers[:8]
        if min(user, nice, system, idle, iowait, irq, softirq, steal) < 0:
            return None
        return CpuCounters(
            user=user,
            nice=nice,
            system=system,
            idle=idle,
            iowait=iowait,
            irq=irq,
            softirq=softirq,
            steal=steal,
        )
    return None


def cpu_utilization_percent(previous: CpuCounters, current: CpuCounters) -> float | None:
    """两次快照之间的整机 CPU 使用率。

    计数器回绕 / 复位（``total`` 不增）时返回 ``None`` —— 那是「测不出来」，
    不是「0%」。
    """
    total_delta = current.total - previous.total
    busy_delta = current.busy - previous.busy
    if total_delta <= 0:
        return None
    if busy_delta < 0:
        return None
    return round(100.0 * busy_delta / total_delta, 3)


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    """解析 ``/proc/loadavg`` 的前三个数。"""
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        one, five, fifteen = (float(value) for value in fields[:3])
    except ValueError:
        return None
    return one, five, fifteen


#: ``/proc/driver/nvidia/version`` 里的行，例如::
#:
#:     NVRM version: NVIDIA UNIX x86_64 Kernel Module  595.58.03  Wed ...
#:     GCC version:  gcc version 13.3.0
#:
#: 第一行的版本串是**内核模块**；用户态库里自报的版本另在 NVML 侧。两者不一致正是
#: 「NVML 能打开设备但读数不可信」的经典成因。
_NVRM_VERSION_RE = re.compile(r"NVRM version:.*?Kernel Module\s+(\S+)")
_USERSPACE_VERSION_RE = re.compile(r"^\s*GCC version:\s+gcc version\s+(\S+)")


def parse_nvidia_version(text: str) -> tuple[str | None, str | None]:
    """从 ``/proc/driver/nvidia/version`` 文本里取 ``(内核模块版本, gcc 版本)``。

    用户态 NVML 版本不在这个文件里 —— 本函数**不**去猜它，第二个返回值是 gcc 版本，
    命名上刻意不叫 userspace。真正的用户态版本由 :class:`NvmlGpuReader` 提供。
    """
    module: str | None = None
    gcc: str | None = None
    for line in text.splitlines():
        if module is None:
            found = _NVRM_VERSION_RE.search(line)
            if found:
                module = found.group(1)
        if gcc is None:
            found = _USERSPACE_VERSION_RE.match(line)
            if found:
                gcc = found.group(1)
    return module, gcc


#: 读取器报告的错误类别。
NVML_ERROR_PERMISSION = "permission_denied"


def driver_status(
    kernel_module_version: str | None,
    userspace_version: str | None,
    *,
    userspace_error: str | None = None,
) -> str:
    """给出驱动状态。返回 :data:`DRIVER_STATES` 之一。

    * 用户态**因权限读不到** -> ``permission_denied``：有卡、有权的问题 ——
      与「没有卡」是完全不同的两件事，合并会让一次权限问题看起来像一次硬件缺失。
    * 两边都没有 -> ``no_device``
    * 只有一边 -> ``ok``（有设备，另一边没读到而已 —— **不**升格成 mismatch，
      因为「没读到」不是「不一致」）
    * 两边都有且不同 -> ``version_mismatch``
    * 两边都有且相同 -> ``ok``

    ``userspace_error`` 的存在是必要的：NVML 打不开时它只会返回 ``None``，与
    「这台机器没有 NVML」在返回值上无法区分。判定必须由读取器把成因**显式**带过来，
    否则「无权读取」会被上面第 3 条静静地判成 ``ok``。
    """
    if userspace_error == NVML_ERROR_PERMISSION:
        return "permission_denied"
    if not kernel_module_version and not userspace_version:
        return "no_device"
    if not kernel_module_version or not userspace_version:
        return "ok"
    if kernel_module_version != userspace_version:
        return "version_mismatch"
    return "ok"


# ---------------------------------------------------------------------------
# 报告结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceBlock:
    """单张卡的物理读数。**没有** ``uid``：整卡读数不属于任何人。"""

    index: int
    name: str | None
    readings: tuple

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "scope": "device",
            "readings": {r.metric_id: r.to_dict() for r in self.readings},
        }


@dataclass(frozen=True)
class HostReport:
    """一次主机采集的完整结果。"""

    source: str
    synthetic: bool
    node_readings: tuple
    driver_readings: tuple
    devices: tuple[DeviceBlock, ...]
    notes: tuple[str, ...]

    def node_map(self) -> dict:
        return {r.metric_id: r.to_dict() for r in self.node_readings}

    def driver_map(self) -> dict:
        return {r.metric_id: r.to_dict() for r in self.driver_readings}

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "synthetic": self.synthetic,
            "node": self.node_map(),
            "driver": self.driver_map(),
            "devices": [device.to_dict() for device in self.devices],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# GPU 读取（句柄注入，模块自身不 import pynvml）
# ---------------------------------------------------------------------------


class NullGpuReader:
    """没有 GPU 读取能力时的默认实现：一律 ``unsupported``，绝不编造。"""

    source = "nvml-absent"

    def driver_version(self) -> str | None:
        return None

    def device_count(self) -> int | None:
        return None

    def devices(self) -> tuple[DeviceBlock, ...]:
        return ()


#: 权限类异常文本的识别线索。**不 import pynvml** —— 它的异常类型会随版本变，
#: 而这三条词根在 ``NVMLError_NoPermission`` / ``PermissionError`` /
#: ``Errno 13`` 的所有常见拼法里都在。
_PERMISSION_MARKERS = (
    "permission",
    "errno 13",
    "access is denied",
    "operation not permitted",
    "insufficient privilege",
    "insufficient permission",
)


def classify_nvml_error(exc: object) -> str:
    """把 NVML 抛出的异常归类成 :data:`NVML_ERROR_PERMISSION` 或 ``"unknown"``。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    for marker in _PERMISSION_MARKERS:
        if marker in text:
            return NVML_ERROR_PERMISSION
    return "unknown"


@dataclass
class NvmlGpuReader:
    """把 NVML 句柄包成读数。``handle`` 由调用方注入（生产是 ``pynvml``，测试是替身）。

    只调用**只读**查询；本类不设置任何设备状态、不加载模型、不改驱动配置。

    句柄缺项时对应读数报 ``no_sample`` / ``unsupported`` —— 例如没有温度传感器的卡
    不返回 ``0`` 摄氏度。

    **注意这是可变对象**（不是 ``frozen``）：它记录最近一次查询的失败成因
    （:attr:`last_error`），好让「无权读取」不被当成「没有设备」。成因只保留**类别**，
    不留原始异常文本 —— 上游错误串可能带上主机路径之类的信息。
    """

    handle: object
    source: str = "nvml"
    now: datetime | None = None
    #: 最近一次 ``_call`` 的失败成因类别（``None`` = 没失败过）。
    last_error: str | None = None

    def _call(self, name: str, *args):
        method = getattr(self.handle, name, None)
        if method is None:
            return None
        try:
            return method(*args)
        except Exception as exc:  # noqa: BLE001 - NVML 抛的是它自己的异常类型，不引依赖
            self.last_error = classify_nvml_error(exc)
            return None

    def error_kind(self) -> str | None:
        """最近一次查询失败的**类别**（``permission_denied`` / ``unknown`` / ``None``）。"""
        return self.last_error

    def driver_version(self) -> str | None:
        self.last_error = None
        value = self._call("nvmlSystemGetDriverVersion")
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    def device_count(self) -> int | None:
        value = self._call("nvmlDeviceGetCount")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def devices(self) -> tuple[DeviceBlock, ...]:
        count = self.device_count()
        if not count:
            return ()
        blocks: list[DeviceBlock] = []
        for index in range(count):
            handle = self._call("nvmlDeviceGetHandleByIndex", index)
            if handle is None:
                blocks.append(
                    DeviceBlock(
                        index=index,
                        name=None,
                        readings=tuple(
                            MetricReading.unknown(
                                metric_id,
                                source=self.source,
                                reason=REASON_SOURCE_UNAVAILABLE,
                                now=self.now,
                            )
                            for metric_id in (
                                "device.gpu.memory.total_bytes",
                                "device.gpu.memory.used_bytes",
                                "device.gpu.utilization_percent",
                                "device.gpu.temperature_celsius",
                            )
                        ),
                    )
                )
                continue
            blocks.append(self._device_block(index, handle))
        return tuple(blocks)

    def _device_block(self, index: int, handle: object) -> DeviceBlock:
        readings: list = []
        name: str | None = None

        raw_name = getattr(handle, "__nvml_name__", None)
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()

        memory = _read_attr(handle, "memory_info")
        total = getattr(memory, "total", None) if memory is not None else None
        used = getattr(memory, "used", None) if memory is not None else None
        readings.append(
            self._metric("device.gpu.memory.total_bytes", total, index)
        )
        readings.append(self._metric("device.gpu.memory.used_bytes", used, index))

        utilization = _read_attr(handle, "utilization")
        gpu_util = getattr(utilization, "gpu", None) if utilization is not None else None
        readings.append(
            self._metric("device.gpu.utilization_percent", gpu_util, index)
        )

        temperature = _read_attr(handle, "temperature")
        readings.append(
            self._metric("device.gpu.temperature_celsius", temperature, index)
        )
        return DeviceBlock(index=index, name=name, readings=tuple(readings))

    def _metric(self, metric_id: str, value, index: int):
        if value is None:
            return MetricReading.unknown(
                metric_id, source=self.source, reason=REASON_UNSUPPORTED, now=self.now
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return MetricReading.unknown(
                metric_id, source=self.source, reason=REASON_UNSUPPORTED, now=self.now
            )
        return MetricReading.from_metric(
            metric_id,
            value=value,
            source=self.source,
            observed_at=self.now or datetime.now(timezone.utc),
            now=self.now,
        )


def _read_attr(obj: object, name: str):
    method = getattr(obj, name, None)
    if method is None:
        return None
    try:
        return method() if callable(method) else method
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------


class ProcHostCollector:
    """从 ``/proc`` 与磁盘读取受限主机指标。

    ``enabled=False``（默认）时**每一项**都是 ``not_collected`` —— 这正是本任务交付的
    形态：采集器已实现、可测，但生产不接线。
    """

    source = HOST_SOURCE

    def __init__(
        self,
        *,
        proc_root: str = DEFAULT_PROC_ROOT,
        nvidia_version_path: str = DEFAULT_NVIDIA_VERSION_PATH,
        disk_mount: str = DEFAULT_DISK_MOUNT,
        gpu_reader: object | None = None,
        disk_usage: Callable[[str], object] | None = None,
        enabled: bool = False,
        source: str = HOST_SOURCE,
    ) -> None:
        self._proc_root = proc_root
        self._nvidia_version_path = nvidia_version_path
        self._disk_mount = disk_mount
        self._gpu_reader = gpu_reader if gpu_reader is not None else NullGpuReader()
        self._disk_usage = disk_usage if disk_usage is not None else shutil.disk_usage
        self._enabled = bool(enabled)
        self._previous_cpu: CpuCounters | None = None
        self.source = source

    def _read_text(self, path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except (OSError, ValueError):
            return None

    def collect(self, now: datetime | None = None, *, synthetic: bool = False) -> HostReport:
        moment = now or datetime.now(timezone.utc)
        if not self._enabled:
            return self._not_collected_report(synthetic)

        notes: list[str] = []
        readings: list = list(self._cpu_readings(notes, moment))
        readings.extend(self._memory_readings(notes, moment))
        readings.extend(self._disk_readings(notes, moment))

        driver_readings = self._driver_readings(now=moment)
        devices = tuple(self._gpu_reader.devices())

        readings.append(self._gpu_count_reading(devices, moment))

        return HostReport(
            source=self.source,
            synthetic=synthetic,
            node_readings=tuple(readings),
            driver_readings=tuple(driver_readings),
            devices=devices,
            notes=tuple(notes),
        )

    # -- 未启用 ------------------------------------------------------------

    _NODE_METRICS = (
        "node.cpu.utilization_percent",
        "node.cpu.load1",
        "node.memory.total_bytes",
        "node.memory.available_bytes",
        "node.memory.used_bytes",
        "node.disk.total_bytes",
        "node.disk.used_bytes",
        "node.gpu.count",
    )
    _DRIVER_METRICS = (
        "node.gpu.driver.status",
        "node.gpu.driver.kernel_module_version",
        "node.gpu.driver.userspace_version",
        "node.gpu.driver.compiler_version",
    )

    def _not_collected_report(self, synthetic: bool) -> HostReport:
        node = tuple(
            MetricReading.unknown(
                metric_id, source=self.source, reason=REASON_NOT_COLLECTED
            )
            for metric_id in self._NODE_METRICS
        )
        driver = (
            StateReading.from_metric(
                "node.gpu.driver.status",
                state=None,
                allowed_states=DRIVER_STATES,
                source=self.source,
                quality=QUALITY_UNKNOWN,
                reason=REASON_NOT_COLLECTED,
            ),
            TextReading.from_metric(
                "node.gpu.driver.kernel_module_version",
                text=None,
                source=self.source,
                quality=QUALITY_UNKNOWN,
                reason=REASON_NOT_COLLECTED,
            ),
            TextReading.from_metric(
                "node.gpu.driver.userspace_version",
                text=None,
                source=self.source,
                quality=QUALITY_UNKNOWN,
                reason=REASON_NOT_COLLECTED,
            ),
            TextReading.from_metric(
                "node.gpu.driver.compiler_version",
                text=None,
                source=self.source,
                quality=QUALITY_UNKNOWN,
                reason=REASON_NOT_COLLECTED,
            ),
        )
        return HostReport(
            source=self.source,
            synthetic=synthetic,
            node_readings=node,
            driver_readings=driver,
            devices=(),
            notes=(
                "主机采集器未启用（enabled=False）：所有主机读数一律 not_collected，"
                "不以 0 代替。本任务不接线采集器。",
            ),
        )

    # -- CPU ---------------------------------------------------------------

    def _cpu_readings(self, notes: list[str], moment: datetime):
        text = self._read_text(os.path.join(self._proc_root, "stat"))
        if text is None:
            notes.append("读不到 /proc/stat：CPU 读数不可得。")
            yield MetricReading.unknown(
                "node.cpu.utilization_percent",
                source=self.source,
                reason=REASON_SOURCE_UNAVAILABLE,
                now=moment,
            )
        else:
            counters = parse_proc_stat(text)
            if counters is None:
                notes.append("/proc/stat 格式不认识：CPU 读数不可得。")
                yield MetricReading.unknown(
                    "node.cpu.utilization_percent",
                    source=self.source,
                    reason=REASON_UNSUPPORTED,
                    now=moment,
                )
            else:
                previous, self._previous_cpu = self._previous_cpu, counters
                if previous is None:
                    # 一个快照算不出速率 —— 如实报 no_sample。
                    yield MetricReading.unknown(
                        "node.cpu.utilization_percent",
                        source=self.source,
                        reason=REASON_NO_SAMPLE,
                        now=moment,
                    )
                else:
                    percent = cpu_utilization_percent(previous, counters)
                    if percent is None:
                        yield MetricReading.unknown(
                            "node.cpu.utilization_percent",
                            source=self.source,
                            reason=REASON_NO_SAMPLE,
                            now=moment,
                        )
                    else:
                        yield MetricReading.from_metric(
                            "node.cpu.utilization_percent",
                            value=percent,
                            source=self.source,
                            observed_at=moment,
                            now=moment,
                        )

        load_text = self._read_text(os.path.join(self._proc_root, "loadavg"))
        parsed_load = parse_loadavg(load_text) if load_text else None
        if parsed_load is None:
            yield MetricReading.unknown(
                "node.cpu.load1",
                source=self.source,
                reason=REASON_SOURCE_UNAVAILABLE,
                now=moment,
            )
        else:
            yield MetricReading.from_metric(
                "node.cpu.load1",
                value=parsed_load[0],
                source=self.source,
                observed_at=moment,
                now=moment,
            )

    # -- 内存 --------------------------------------------------------------

    def _memory_readings(self, notes: list[str], moment: datetime):
        text = self._read_text(os.path.join(self._proc_root, "meminfo"))
        parsed = (
            parse_meminfo(
                text, keys=("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached")
            )
            if text
            else {}
        )
        total = parsed.get("MemTotal")
        available = parsed.get("MemAvailable")
        if total is None:
            notes.append("读不到 MemTotal：内存读数不可得。")
            for metric_id in (
                "node.memory.total_bytes",
                "node.memory.available_bytes",
                "node.memory.used_bytes",
            ):
                yield MetricReading.unknown(
                    metric_id,
                    source=self.source,
                    reason=REASON_SOURCE_UNAVAILABLE,
                    now=moment,
                )
            return

        yield MetricReading.from_metric(
            "node.memory.total_bytes",
            value=total,
            source=self.source,
            observed_at=moment,
            now=moment,
        )
        if available is None:
            yield MetricReading.unknown(
                "node.memory.available_bytes",
                source=self.source,
                reason=REASON_SOURCE_UNAVAILABLE,
                now=moment,
            )
            yield MetricReading.unknown(
                "node.memory.used_bytes",
                source=self.source,
                reason=REASON_SOURCE_UNAVAILABLE,
                now=moment,
            )
            return
        yield MetricReading.from_metric(
            "node.memory.available_bytes",
            value=available,
            source=self.source,
            observed_at=moment,
            now=moment,
        )
        yield MetricReading.from_metric(
            "node.memory.used_bytes",
            value=max(0, total - available),
            source=self.source,
            observed_at=moment,
            now=moment,
        )

    # -- 磁盘 --------------------------------------------------------------

    def _disk_readings(self, notes: list[str], moment: datetime):
        try:
            usage = self._disk_usage(self._disk_mount)
        except OSError:
            usage = None
        total = getattr(usage, "total", None) if usage is not None else None
        used = getattr(usage, "used", None) if usage is not None else None
        if total is None or used is None:
            notes.append(f"读不到 {self._disk_mount} 的容量：磁盘读数不可得。")
            for metric_id in ("node.disk.total_bytes", "node.disk.used_bytes"):
                yield MetricReading.unknown(
                    metric_id,
                    source=self.source,
                    reason=REASON_SOURCE_UNAVAILABLE,
                    now=moment,
                )
            return
        yield MetricReading.from_metric(
            "node.disk.total_bytes",
            value=total,
            source=self.source,
            observed_at=moment,
            now=moment,
        )
        yield MetricReading.from_metric(
            "node.disk.used_bytes",
            value=used,
            source=self.source,
            observed_at=moment,
            now=moment,
        )

    # -- 驱动 --------------------------------------------------------------

    def _driver_readings(self, *, now: datetime):
        text = self._read_text(self._nvidia_version_path)
        module_version: str | None = None
        compiler_version: str | None = None
        if text is not None:
            module_version, compiler_version = parse_nvidia_version(text)

        userspace_version = None
        getter = getattr(self._gpu_reader, "driver_version", None)
        if callable(getter):
            try:
                userspace_version = getter()
            except Exception:  # noqa: BLE001
                userspace_version = None

        # 读取器若记录了失败成因，把它带进判定 —— 否则「无权读取 NVML」会被
        # 「只有一边读到 -> ok」静静吞掉，一次权限问题就长成了「驱动健康」。
        error_kind = None
        kind_getter = getattr(self._gpu_reader, "error_kind", None)
        if callable(kind_getter):
            try:
                error_kind = kind_getter()
            except Exception:  # noqa: BLE001
                error_kind = None

        status = driver_status(
            module_version, userspace_version, userspace_error=error_kind
        )
        # 两边都没读到、且读取器也没报出成因 = 这台机器上没有可读的 NVIDIA 信息
        # -> 状态**无从测得**（不是 ``no_device`` 这个断言）。
        # 注意「版本不一致」与「无权读取」都**不是** unknown：状态确实测到了，
        # 只是结果是「不可信」/「打不开」。
        status_quality = (
            QUALITY_UNKNOWN
            if text is None and userspace_version is None and error_kind is None
            else QUALITY_MEASURED
        )

        yield StateReading.from_metric(
            "node.gpu.driver.status",
            state=status if status_quality != QUALITY_UNKNOWN else None,
            allowed_states=DRIVER_STATES,
            source=self.source,
            quality=status_quality,
            observed_at=now if status_quality != QUALITY_UNKNOWN else None,
            reason=None if status_quality != QUALITY_UNKNOWN else REASON_SOURCE_UNAVAILABLE,
            now=now,
        )

        for metric_id, value in (
            ("node.gpu.driver.kernel_module_version", module_version),
            ("node.gpu.driver.userspace_version", userspace_version),
            ("node.gpu.driver.compiler_version", compiler_version),
        ):
            if value is None:
                yield TextReading.from_metric(
                    metric_id,
                    text=None,
                    source=self.source,
                    quality=QUALITY_UNKNOWN,
                    reason=REASON_SOURCE_UNAVAILABLE,
                    now=now,
                )
            else:
                yield TextReading.from_metric(
                    metric_id,
                    text=value,
                    source=self.source,
                    observed_at=now,
                    now=now,
                )

    def _gpu_count_reading(self, devices: tuple, moment: datetime) -> MetricReading:
        """GPU 数量。

        **不能**用 ``len(devices)`` 直接当计数：没有读取能力时 ``devices()`` 返回空元组，
        于是 ``len`` 是 0 —— 那会把「我没有 NVML，说不上来」渲染成「这台机器没有 GPU」。
        所以先问读取器「你数得出来吗」，数不出来就如实报 unknown。
        """
        counter = getattr(self._gpu_reader, "device_count", None)
        count = None
        if callable(counter):
            try:
                count = counter()
            except Exception:  # noqa: BLE001
                count = None

        if count is None:
            if devices:
                # 读取器不肯报数，但确实给了卡 —— 用实际拿到的数量，并说明来源。
                count = len(devices)
            else:
                return MetricReading.unknown(
                    "node.gpu.count",
                    source=self.source,
                    reason=REASON_UNSUPPORTED,
                    now=moment,
                )
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return MetricReading.unknown(
                "node.gpu.count",
                source=self.source,
                reason=REASON_UNSUPPORTED,
                now=moment,
            )
        return MetricReading.from_metric(
            "node.gpu.count",
            value=count,
            source=self.source,
            observed_at=moment,
            now=moment,
        )


# ---------------------------------------------------------------------------
# Mock 采集器 —— 供测试与预览使用
# ---------------------------------------------------------------------------


class MockHostCollector:
    """全合成的固定主机报告。``synthetic`` 恒为 ``True``。

    与 :class:`MockMetricProvider` 同一条纪律：合成数据的纯度是常量，不是调用方选项。
    """

    source = "mock-host"

    def __init__(self, *, enabled: bool = True, now: datetime | None = None) -> None:
        self._enabled = bool(enabled)
        self._now = now

    def collect(self, now: datetime | None = None, *, synthetic: bool = True) -> HostReport:
        if not self._enabled:
            return ProcHostCollector(enabled=False).collect(now, synthetic=True)

        moment = now or self._now or datetime.now(timezone.utc)
        node = (
            MetricReading.from_metric(
                "node.cpu.utilization_percent", value=37.5, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.cpu.load1", value=2.25, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.memory.total_bytes", value=33554432000, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.memory.available_bytes", value=19418112000, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.memory.used_bytes", value=14136320000, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.disk.total_bytes", value=1024000000000, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.disk.used_bytes", value=512000000000, source=self.source,
                observed_at=moment, now=moment,
            ),
            MetricReading.from_metric(
                "node.gpu.count", value=1, source=self.source,
                observed_at=moment, now=moment,
            ),
        )
        driver = (
            StateReading.from_metric(
                "node.gpu.driver.status",
                state="version_mismatch",
                allowed_states=DRIVER_STATES,
                source=self.source,
                observed_at=moment,
                now=moment,
            ),
            TextReading.from_metric(
                "node.gpu.driver.kernel_module_version", text="595.58.03",
                source=self.source, observed_at=moment, now=moment,
            ),
            TextReading.from_metric(
                "node.gpu.driver.userspace_version", text="595.99.02",
                source=self.source, observed_at=moment, now=moment,
            ),
            TextReading.from_metric(
                "node.gpu.driver.compiler_version", text="13.3.0",
                source=self.source, observed_at=moment, now=moment,
            ),
        )
        device = DeviceBlock(
            index=0,
            name="NVIDIA RTX A6000",
            readings=(
                MetricReading.from_metric(
                    "device.gpu.memory.total_bytes", value=51527106560,
                    source=self.source, observed_at=moment, now=moment,
                ),
                MetricReading.from_metric(
                    "device.gpu.memory.used_bytes", value=47647023104,
                    source=self.source, observed_at=moment, now=moment,
                ),
                MetricReading.from_metric(
                    "device.gpu.utilization_percent", value=12.0,
                    source=self.source, observed_at=moment, now=moment,
                ),
                MetricReading.from_metric(
                    "device.gpu.temperature_celsius", value=48.0,
                    source=self.source, observed_at=moment, now=moment,
                ),
            ),
        )
        return HostReport(
            source=self.source,
            synthetic=True,
            node_readings=node,
            driver_readings=driver,
            devices=(device,),
            notes=(
                "SYNTHETIC / DEMO 主机读数：数值为虚构，不得作为容量或告警依据。",
                "整卡显存为**共享量**，与任何用户的计费口径无关。",
            ),
        )
