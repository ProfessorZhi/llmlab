"""受限主机采集：纯解析函数、驱动状态、以及「未启用 ≠ 0」。

解析全部用**合成文本**测，采集器跑在一个临时目录搭出来的假 ``/proc`` 上 ——
所以本文件既不读这台机器的真实状态，也不碰 GPU（NVML 句柄是注入的替身）。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import NOW  # noqa: E402

from backend.observability import (  # noqa: E402
    DRIVER_STATES,
    QUALITY_UNKNOWN,
    REASON_NOT_COLLECTED,
    REASON_NO_SAMPLE,
    REASON_UNSUPPORTED,
    CpuCounters,
    MockHostCollector,
    NullGpuReader,
    NvmlGpuReader,
    ProcHostCollector,
    cpu_utilization_percent,
    driver_status,
    parse_loadavg,
    parse_meminfo,
    parse_nvidia_version,
    parse_proc_stat,
)

MEMINFO = """MemTotal:       32768000 kB
MemFree:         2048000 kB
MemAvailable:   18953000 kB
Buffers:          512000 kB
Cached:         12000000 kB
SwapTotal:             0 kB
SwapFree:              0 kB
"""

#: 两行 cpu 计数器，用来算一个确定的差分。
PROC_STAT_A = "cpu  1000 10 200 8000 50 0 20 0 0 0\ncpu0 500 5 100 4000 25 0 10 0 0 0\n"
PROC_STAT_B = "cpu  1100 10 220 8300 50 0 20 0 0 0\ncpu0 550 5 110 4150 25 0 10 0 0 0\n"

NVIDIA_VERSION = """NVRM version: NVIDIA UNIX x86_64 Kernel Module  595.58.03  Wed Feb 11 15:00:00 UTC 2026
GCC version:  gcc version 13.3.0
"""


class ParseMeminfoTest(unittest.TestCase):
    def test_kilobytes_are_converted_to_bytes(self) -> None:
        parsed = parse_meminfo(MEMINFO)
        self.assertEqual(parsed["MemTotal"], 32768000 * 1024)
        self.assertEqual(parsed["MemAvailable"], 18953000 * 1024)

    def test_key_filtering(self) -> None:
        parsed = parse_meminfo(MEMINFO, keys=("MemTotal",))
        self.assertEqual(set(parsed), {"MemTotal"})

    def test_unit_is_read_per_line_not_assumed(self) -> None:
        parsed = parse_meminfo("A: 10 MB\nB: 10 kB\nC: 10\n")
        self.assertEqual(parsed["A"], 10 * 1024 * 1024)
        self.assertEqual(parsed["B"], 10 * 1024)
        self.assertEqual(parsed["C"], 10)

    def test_unknown_unit_drops_the_line_rather_than_guessing(self) -> None:
        parsed = parse_meminfo("A: 10 furlongs\nB: 5 kB\n")
        self.assertNotIn("A", parsed)
        self.assertEqual(parsed["B"], 5120)

    def test_garbage_lines_are_skipped(self) -> None:
        parsed = parse_meminfo("no colon here\nB: notanumber kB\nC: -5 kB\nD: 1 kB\n")
        self.assertEqual(set(parsed), {"D"})

    def test_empty_input_yields_no_keys(self) -> None:
        self.assertEqual(parse_meminfo(""), {})


class ParseProcStatTest(unittest.TestCase):
    def test_first_cpu_line_is_parsed(self) -> None:
        counters = parse_proc_stat(PROC_STAT_A)
        self.assertIsInstance(counters, CpuCounters)
        self.assertEqual(counters.user, 1000)
        self.assertEqual(counters.idle, 8000)
        self.assertEqual(counters.iowait, 50)
        self.assertEqual(counters.steal, 0)

    def test_total_and_busy_exclude_idle_and_iowait(self) -> None:
        counters = parse_proc_stat(PROC_STAT_A)
        self.assertEqual(counters.total, 1000 + 10 + 200 + 8000 + 50 + 0 + 20 + 0)
        self.assertEqual(counters.busy, counters.total - 8000 - 50)

    def test_missing_or_short_line_returns_none(self) -> None:
        self.assertIsNone(parse_proc_stat("cpu0 1 2 3 4 5 6 7 8\n"))
        self.assertIsNone(parse_proc_stat("cpu  1 2 3\n"))
        self.assertIsNone(parse_proc_stat(""))

    def test_non_numeric_counters_return_none(self) -> None:
        self.assertIsNone(parse_proc_stat("cpu  1 2 3 four 5 6 7 8\n"))


class CpuUtilizationTest(unittest.TestCase):
    def test_known_delta(self) -> None:
        before = parse_proc_stat(PROC_STAT_A)
        after = parse_proc_stat(PROC_STAT_B)
        total_delta = after.total - before.total
        busy_delta = after.busy - before.busy
        expected = round(100.0 * busy_delta / total_delta, 3)
        self.assertEqual(cpu_utilization_percent(before, after), expected)

    def test_counter_reset_returns_none_not_zero(self) -> None:
        """计数器回绕时是「测不出来」，不是「0% 忙」。"""
        before = parse_proc_stat(PROC_STAT_B)
        after = parse_proc_stat(PROC_STAT_A)
        self.assertIsNone(cpu_utilization_percent(before, after))

    def test_identical_snapshots_return_none(self) -> None:
        counters = parse_proc_stat(PROC_STAT_A)
        self.assertIsNone(cpu_utilization_percent(counters, counters))

    def test_idle_only_delta_is_zero_percent_measured(self) -> None:
        before = CpuCounters(0, 0, 0, 100, 0, 0, 0, 0)
        after = CpuCounters(0, 0, 0, 200, 0, 0, 0, 0)
        self.assertEqual(cpu_utilization_percent(before, after), 0.0)

    def test_result_is_a_percentage(self) -> None:
        before = CpuCounters(0, 0, 0, 0, 0, 0, 0, 0)
        after = CpuCounters(50, 0, 50, 0, 0, 0, 0, 0)
        self.assertEqual(cpu_utilization_percent(before, after), 100.0)


class ParseLoadavgTest(unittest.TestCase):
    def test_first_three_fields(self) -> None:
        self.assertEqual(parse_loadavg("2.25 1.50 1.10 3/512 12345\n"), (2.25, 1.5, 1.1))

    def test_too_few_fields_returns_none(self) -> None:
        self.assertIsNone(parse_loadavg("1.0 2.0\n"))

    def test_non_numeric_returns_none(self) -> None:
        self.assertIsNone(parse_loadavg("busy busy busy\n"))


class ParseNvidiaVersionTest(unittest.TestCase):
    def test_kernel_module_and_gcc_versions(self) -> None:
        module, gcc = parse_nvidia_version(NVIDIA_VERSION)
        self.assertEqual(module, "595.58.03")
        self.assertEqual(gcc, "13.3.0")

    def test_missing_file_content_returns_nones(self) -> None:
        self.assertEqual(parse_nvidia_version(""), (None, None))

    def test_unrecognised_text_returns_nones(self) -> None:
        self.assertEqual(parse_nvidia_version("hello world\n"), (None, None))


class DriverStatusTest(unittest.TestCase):
    def test_matching_versions_are_ok(self) -> None:
        self.assertEqual(driver_status("595.58.03", "595.58.03"), "ok")

    def test_mismatched_versions_are_flagged(self) -> None:
        """本机真实踩过的形态：内核模块与用户态库版本不一致，NVML 仍能打开设备。"""
        self.assertEqual(driver_status("595.58.03", "595.99.02"), "version_mismatch")

    def test_no_information_at_all_is_no_device(self) -> None:
        self.assertEqual(driver_status(None, None), "no_device")

    def test_one_sided_information_is_ok_not_a_mismatch(self) -> None:
        """「没读到」不是「不一致」—— 把它升格成 mismatch 会造出一个假的告警。"""
        self.assertEqual(driver_status("595.58.03", None), "ok")
        self.assertEqual(driver_status(None, "595.99.02"), "ok")

    def test_a_permission_failure_is_not_reported_as_healthy(self) -> None:
        """「有卡但我读不到」与「这台机器没有卡」必须分开。

        NVML 打不开时只返回 ``None``，与「没有 NVML」在返回值上无法区分 —— 所以
        成因必须由读取器**显式**带过来。少了这一步，「只有一边读到 -> ok」会把一次
        权限问题静静地判成「驱动健康」。
        """
        from backend.observability import NVML_ERROR_PERMISSION

        self.assertEqual(
            driver_status("595.58.03", None, userspace_error=NVML_ERROR_PERMISSION),
            "permission_denied",
        )
        # 而没有成因时，同样输入仍是 ok —— 证明区别来自那个参数，不是巧合。
        self.assertEqual(driver_status("595.58.03", None), "ok")

    def test_permission_denied_wins_over_everything_else(self) -> None:
        from backend.observability import NVML_ERROR_PERMISSION

        for module, userspace in (("a", "a"), ("a", "b"), (None, None), (None, "a")):
            with self.subTest(pair=(module, userspace)):
                self.assertEqual(
                    driver_status(
                        module, userspace, userspace_error=NVML_ERROR_PERMISSION
                    ),
                    "permission_denied",
                )

    def test_every_result_is_in_the_declared_vocabulary(self) -> None:
        for pair in (
            ("a", "a"),
            ("a", "b"),
            (None, None),
            ("a", None),
            (None, "a"),
        ):
            with self.subTest(pair=pair):
                self.assertIn(driver_status(*pair), DRIVER_STATES)

    def test_permission_denied_is_reachable_from_some_code_path(self) -> None:
        """文档承诺了这个状态 —— 它必须是可达的，不能只活在一张表里。"""
        from backend.observability import NVML_ERROR_PERMISSION

        self.assertIn("permission_denied", DRIVER_STATES)
        self.assertEqual(NVML_ERROR_PERMISSION, "permission_denied")
        self.assertIn(
            driver_status("x", None, userspace_error=NVML_ERROR_PERMISSION),
            DRIVER_STATES,
        )


class NvmlErrorClassificationTest(unittest.TestCase):
    def test_permission_flavoured_errors_are_classified(self) -> None:
        from backend.observability import NVML_ERROR_PERMISSION, classify_nvml_error

        for exc in (
            PermissionError("read denied"),
            OSError(13, "Permission denied"),
            RuntimeError("NVMLError_InsufficientPermissions: insufficient permission"),
            RuntimeError("Operation not permitted"),
        ):
            with self.subTest(exc=exc):
                self.assertEqual(classify_nvml_error(exc), NVML_ERROR_PERMISSION)

    def test_other_errors_are_not_dressed_up_as_permission_problems(self) -> None:
        from backend.observability import classify_nvml_error

        for exc in (RuntimeError("driver not loaded"), ValueError("bad argument")):
            with self.subTest(exc=exc):
                self.assertEqual(classify_nvml_error(exc), "unknown")

    def test_the_reader_records_the_error_kind_instead_of_the_raw_text(self) -> None:
        """只保留**类别** —— 上游错误串可能带上主机路径之类的信息。"""

        class Denying:
            def nvmlSystemGetDriverVersion(self):
                raise PermissionError("cannot open /dev/nvidiactl: permission denied")

        reader = NvmlGpuReader(Denying(), now=NOW)
        self.assertIsNone(reader.driver_version())
        self.assertEqual(reader.error_kind(), "permission_denied")

    def test_a_successful_read_clears_a_previous_error(self) -> None:
        reader = NvmlGpuReader(FakeNvml([], count=0), now=NOW)
        reader.last_error = "unknown"
        self.assertEqual(reader.driver_version(), "595.99.02")
        self.assertIsNone(reader.error_kind())

    def test_a_reader_without_error_kind_is_tolerated(self) -> None:
        """``NullGpuReader`` 与任何自定义读取器都没有 ``error_kind``。"""
        reader = NullGpuReader()
        self.assertFalse(hasattr(reader, "error_kind") or callable(
            getattr(reader, "error_kind", None)
        ))


class FakeMemoryInfo:
    def __init__(self, total, used):
        self.total = total
        self.used = used


class FakeUtilization:
    def __init__(self, gpu):
        self.gpu = gpu


class FakeDevice:
    def __init__(self, name, total, used, gpu, temperature):
        self.__nvml_name__ = name
        self._memory = FakeMemoryInfo(total, used)
        self._utilization = FakeUtilization(gpu)
        self._temperature = temperature

    def memory_info(self):
        return self._memory

    def utilization(self):
        return self._utilization

    def temperature(self):
        return self._temperature


class FakeNvml:
    """NVML 句柄的替身。**不导入 pynvml，不接触任何真实设备。**"""

    def __init__(self, devices, *, driver_version=b"595.99.02", count=None):
        self._devices = devices
        self._driver = driver_version
        self._count = len(devices) if count is None else count

    def nvmlSystemGetDriverVersion(self):
        return self._driver

    def nvmlDeviceGetCount(self):
        return self._count

    def nvmlDeviceGetHandleByIndex(self, index):
        if index >= len(self._devices):
            return None
        return self._devices[index]


class NvmlGpuReaderTest(unittest.TestCase):
    def test_device_readings_carry_the_full_envelope(self) -> None:
        reader = NvmlGpuReader(
            FakeNvml([FakeDevice("NVIDIA RTX A6000", 51527106560, 47647023104, 12, 48)]),
            now=NOW,
        )
        devices = reader.devices()
        self.assertEqual(len(devices), 1)
        payload = devices[0].to_dict()
        self.assertEqual(payload["scope"], "device")
        self.assertEqual(payload["name"], "NVIDIA RTX A6000")
        readings = payload["readings"]
        self.assertEqual(readings["device.gpu.memory.total_bytes"]["value"], 51527106560)
        self.assertEqual(readings["device.gpu.utilization_percent"]["value"], 12)
        self.assertEqual(readings["device.gpu.temperature_celsius"]["value"], 48)
        for reading in readings.values():
            self.assertIn("freshness", reading)
            self.assertIn("source", reading)
            self.assertTrue(reading["physical"])
            self.assertNotIn("uid", reading)

    def test_a_device_block_has_no_uid_key(self) -> None:
        reader = NvmlGpuReader(FakeNvml([FakeDevice("X", 1, 1, 1, 1)]), now=NOW)
        payload = reader.devices()[0].to_dict()
        self.assertNotIn("uid", payload)

    def test_missing_temperature_sensor_is_unsupported_not_zero(self) -> None:
        reader = NvmlGpuReader(
            FakeNvml([FakeDevice("X", 100, 50, 10, None)]), now=NOW
        )
        readings = reader.devices()[0].to_dict()["readings"]
        temperature = readings["device.gpu.temperature_celsius"]
        self.assertIsNone(temperature["value"])
        self.assertIsNot(temperature["value"], 0)
        self.assertEqual(temperature["reason"], REASON_UNSUPPORTED)

    def test_missing_device_handle_yields_readings_without_a_name(self) -> None:
        reader = NvmlGpuReader(FakeNvml([], count=2), now=NOW)
        devices = reader.devices()
        self.assertEqual([device.index for device in devices], [0, 1])
        self.assertIsNone(devices[0].name)
        for reading in devices[0].to_dict()["readings"].values():
            self.assertIsNone(reading["value"])

    def test_zero_devices_yields_no_blocks(self) -> None:
        reader = NvmlGpuReader(FakeNvml([], count=0), now=NOW)
        self.assertEqual(reader.devices(), ())
        self.assertEqual(reader.device_count(), 0)

    def test_driver_version_is_decoded(self) -> None:
        reader = NvmlGpuReader(FakeNvml([], count=0), now=NOW)
        self.assertEqual(reader.driver_version(), "595.99.02")

    def test_a_raising_handle_does_not_propagate(self) -> None:
        class Exploding:
            def nvmlDeviceGetCount(self):
                raise RuntimeError("NVML blew up")

        reader = NvmlGpuReader(Exploding(), now=NOW)
        self.assertIsNone(reader.device_count())
        self.assertEqual(reader.devices(), ())

    def test_null_reader_reports_nothing_and_invents_nothing(self) -> None:
        reader = NullGpuReader()
        self.assertIsNone(reader.device_count())
        self.assertIsNone(reader.driver_version())
        self.assertEqual(reader.devices(), ())


class CollectorDisabledTest(unittest.TestCase):
    """默认形态：生产不启用 -> 每一项都是 not_collected，不是 0。"""

    def test_disabled_collector_reports_not_collected_for_every_node_metric(self) -> None:
        report = ProcHostCollector(enabled=False).collect(NOW)
        node = report.node_map()
        self.assertEqual(
            set(node),
            {
                "node.cpu.utilization_percent",
                "node.cpu.load1",
                "node.memory.total_bytes",
                "node.memory.available_bytes",
                "node.memory.used_bytes",
                "node.disk.total_bytes",
                "node.disk.used_bytes",
                "node.gpu.count",
            },
        )
        for metric_id, reading in node.items():
            with self.subTest(metric=metric_id):
                self.assertIsNone(reading["value"])
                self.assertIsNot(reading["value"], 0)
                self.assertEqual(reading["reason"], REASON_NOT_COLLECTED)
                self.assertEqual(reading["display_quality"], QUALITY_UNKNOWN)

    def test_disabled_collector_reports_driver_status_not_collected(self) -> None:
        driver = ProcHostCollector(enabled=False).collect(NOW).driver_map()
        status = driver["node.gpu.driver.status"]
        self.assertIsNone(status["state"])
        self.assertEqual(status["reason"], REASON_NOT_COLLECTED)
        self.assertEqual(status["allowed_states"], list(DRIVER_STATES))

    def test_disabled_collector_touches_no_devices(self) -> None:
        report = ProcHostCollector(enabled=False).collect(NOW)
        self.assertEqual(report.devices, ())

    def test_mock_host_collector_disabled_delegates_honestly(self) -> None:
        report = MockHostCollector(enabled=False).collect(NOW)
        self.assertTrue(report.synthetic)
        for reading in report.node_map().values():
            self.assertIsNone(reading["value"])

    def test_synthetic_flag_is_reported(self) -> None:
        self.assertTrue(MockHostCollector().collect(NOW).synthetic)
        self.assertFalse(ProcHostCollector(enabled=False).collect(NOW).synthetic)


class CollectorEnabledAgainstAFakeProcTest(unittest.TestCase):
    """把假 ``/proc`` 搭在临时目录里跑真采集器 —— 不读这台机器的任何真实状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "stat").write_text(PROC_STAT_A, encoding="utf-8")
        (self.root / "loadavg").write_text("2.25 1.50 1.10 3/512 12345\n", encoding="utf-8")
        (self.root / "meminfo").write_text(MEMINFO, encoding="utf-8")
        self.nvidia_path = self.root / "nvidia-version"
        self.nvidia_path.write_text(NVIDIA_VERSION, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _collector(self, **kwargs):
        options = {
            "proc_root": str(self.root),
            "nvidia_version_path": str(self.nvidia_path),
            "disk_usage": lambda mount: type(
                "Usage", (), {"total": 1_000_000_000, "used": 400_000_000}
            )(),
            "enabled": True,
        }
        options.update(kwargs)
        return ProcHostCollector(**options)

    def test_first_collection_cannot_compute_cpu_utilisation(self) -> None:
        """一个计数器快照算不出速率 —— 如实报 no_sample。"""
        node = self._collector().collect(NOW).node_map()
        cpu = node["node.cpu.utilization_percent"]
        self.assertIsNone(cpu["value"])
        self.assertEqual(cpu["reason"], REASON_NO_SAMPLE)

    def test_second_collection_computes_a_utilisation(self) -> None:
        collector = self._collector()
        collector.collect(NOW)
        (self.root / "stat").write_text(PROC_STAT_B, encoding="utf-8")
        node = collector.collect(NOW).node_map()
        cpu = node["node.cpu.utilization_percent"]
        self.assertIsNotNone(cpu["value"])
        self.assertEqual(cpu["quality"], "measured")

    def test_memory_readings_are_measured_and_used_is_derived(self) -> None:
        node = self._collector().collect(NOW).node_map()
        self.assertEqual(node["node.memory.total_bytes"]["value"], 32768000 * 1024)
        self.assertEqual(node["node.memory.available_bytes"]["value"], 18953000 * 1024)
        self.assertEqual(
            node["node.memory.used_bytes"]["value"],
            (32768000 - 18953000) * 1024,
        )

    def test_disk_readings_come_from_the_injected_usage_callable(self) -> None:
        node = self._collector().collect(NOW).node_map()
        self.assertEqual(node["node.disk.total_bytes"]["value"], 1_000_000_000)
        self.assertEqual(node["node.disk.used_bytes"]["value"], 400_000_000)

    def test_driver_version_mismatch_is_surfaced(self) -> None:
        """内核模块 595.58.03 vs 用户态 595.99.02 —— 状态是 version_mismatch。"""
        collector = self._collector(
            gpu_reader=NvmlGpuReader(FakeNvml([], count=1), now=NOW)
        )
        report = collector.collect(NOW)
        driver = report.driver_map()
        self.assertEqual(
            driver["node.gpu.driver.kernel_module_version"]["text"], "595.58.03"
        )
        self.assertEqual(
            driver["node.gpu.driver.userspace_version"]["text"], "595.99.02"
        )
        self.assertEqual(driver["node.gpu.driver.status"]["state"], "version_mismatch")

    def test_the_compiler_version_from_the_same_file_is_surfaced(self) -> None:
        """解析出的第二个值必须**被用上** —— 否则它只是一次无意义的 parse。"""
        report = self._collector().collect(NOW)
        driver = report.driver_map()
        self.assertEqual(driver["node.gpu.driver.compiler_version"]["text"], "13.3.0")
        self.assertEqual(driver["node.gpu.driver.compiler_version"]["quality"], "measured")

    def test_a_permission_failure_shows_up_as_permission_denied_not_ok(self) -> None:
        """``/proc`` 读得到、NVML 打不开 —— 这正是权限问题的真实形态。"""

        class Denying:
            def nvmlSystemGetDriverVersion(self):
                raise PermissionError("open /dev/nvidiactl: permission denied")

            def nvmlDeviceGetCount(self):
                raise PermissionError("open /dev/nvidiactl: permission denied")

        collector = self._collector(gpu_reader=NvmlGpuReader(Denying(), now=NOW))
        driver = collector.collect(NOW).driver_map()
        self.assertEqual(
            driver["node.gpu.driver.kernel_module_version"]["text"], "595.58.03"
        )
        self.assertIsNone(driver["node.gpu.driver.userspace_version"]["text"])
        self.assertEqual(driver["node.gpu.driver.status"]["state"], "permission_denied")

    def test_the_compiler_version_is_not_collected_when_the_collector_is_off(self) -> None:
        driver = ProcHostCollector(enabled=False).collect(NOW).driver_map()
        compiler = driver["node.gpu.driver.compiler_version"]
        self.assertIsNone(compiler["text"])
        self.assertEqual(compiler["reason"], REASON_NOT_COLLECTED)

    def test_driver_status_is_measured_not_stale_prone(self) -> None:
        report = self._collector().collect(NOW)
        status = report.driver_map()["node.gpu.driver.status"]
        self.assertEqual(status["quality"], "measured")
        self.assertIsNone(status["freshness"]["threshold_seconds"])

    def test_absent_nvidia_file_marks_the_status_unknown(self) -> None:
        """两边都读不到 -> 状态**无从测得**，而不是 ``no_device`` 这个断言。"""
        collector = self._collector(
            nvidia_version_path=str(self.root / "does-not-exist")
        )
        driver = collector.collect(NOW).driver_map()
        status = driver["node.gpu.driver.status"]
        self.assertIsNone(status["state"])
        self.assertEqual(status["quality"], QUALITY_UNKNOWN)
        self.assertEqual(
            driver["node.gpu.driver.kernel_module_version"]["text"], None
        )

    def test_unreadable_proc_files_do_not_crash_and_do_not_fake_zero(self) -> None:
        collector = ProcHostCollector(
            proc_root=str(self.root / "missing"),
            nvidia_version_path=str(self.nvidia_path),
            disk_usage=lambda mount: (_ for _ in ()).throw(OSError("nope")),
            enabled=True,
        )
        node = collector.collect(NOW).node_map()
        for metric_id, reading in node.items():
            with self.subTest(metric=metric_id):
                self.assertIsNone(reading["value"])
                self.assertIsNot(reading["value"], 0)

    def test_notes_explain_what_could_not_be_read(self) -> None:
        collector = ProcHostCollector(
            proc_root=str(self.root / "missing"),
            nvidia_version_path=str(self.nvidia_path),
            enabled=True,
        )
        report = collector.collect(NOW)
        self.assertTrue(any("读不到" in note for note in report.notes))

    def test_gpu_count_is_measured_from_the_injected_reader(self) -> None:
        collector = self._collector(gpu_reader=NvmlGpuReader(FakeNvml([], count=2), now=NOW))
        node = collector.collect(NOW).node_map()
        self.assertEqual(node["node.gpu.count"]["value"], 2)

    def test_report_is_json_serialisable(self) -> None:
        import json

        payload = self._collector().collect(NOW).to_dict()
        json.dumps(payload, sort_keys=True)
        self.assertIn("node", payload)
        self.assertIn("driver", payload)
        self.assertIn("devices", payload)


class MockHostCollectorTest(unittest.TestCase):
    def test_mock_report_is_synthetic_and_flagged_in_notes(self) -> None:
        report = MockHostCollector().collect(NOW)
        self.assertTrue(report.synthetic)
        self.assertTrue(any("SYNTHETIC" in note for note in report.notes))

    def test_mock_report_has_a_device_with_physical_readings(self) -> None:
        report = MockHostCollector().collect(NOW)
        self.assertEqual(len(report.devices), 1)
        readings = report.devices[0].to_dict()["readings"]
        self.assertEqual(
            readings["device.gpu.memory.used_bytes"]["unit"], "bytes"
        )
        self.assertTrue(readings["device.gpu.memory.used_bytes"]["physical"])

    def test_mock_report_shows_a_version_mismatch_deliberately(self) -> None:
        driver = MockHostCollector().collect(NOW).driver_map()
        self.assertEqual(driver["node.gpu.driver.status"]["state"], "version_mismatch")

    def test_mock_notes_say_the_device_memory_is_shared(self) -> None:
        report = MockHostCollector().collect(NOW)
        self.assertTrue(any("共享量" in note for note in report.notes))


if __name__ == "__main__":
    unittest.main()
