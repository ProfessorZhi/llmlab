"""``/proc`` 与 ``/sys`` 的解析：整机读数这一支的口径。

这一组测的不是「读出来像不像真的」，而是三条容易被顺手写错的规则：

1. **iowait 不算忙。** 一次慢 I/O 抢走的是 CPU 的等待时间，把它算成负载会让
   排查一路跑到错误的方向。
2. **同一批字节只算一次。** 分区（``nvme0n1p1``）和整盘（``nvme0n1``）同时出现在
   ``diskstats`` 里，加在一起就是凭空翻倍。
3. **读不到 ≠ 0。** 引擎没返回按进程的显存清单时，``vram_mib`` 是 ``None`` 并附
   一条 note，而不是「这台机没人用显存」。
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend.bff import live_sources as L


def _patch_proc(mapping):
    """把 ``_proc_text`` 换成一张「路径 → 内容」的表；表里没有的路径当读不到。"""
    def _read(path):
        if path not in mapping:
            raise OSError("no such file: " + path)
        return mapping[path]
    return mock.patch.object(L, "_proc_text", side_effect=_read)


class CpuTests(unittest.TestCase):
    STAT = (
        "cpu  100 0 50 800 30 5 5 10 0 0\n"
        "cpu0 50 0 25 400 15 2 2 5 0 0\n"
        "intr 1\n"
    )

    def test_the_first_line_is_the_whole_machine(self):
        with _patch_proc({"/proc/stat": self.STAT}):
            out = L._read_cpu_times()
        self.assertEqual(out["total"], 100 + 0 + 50 + 800 + 30 + 5 + 5 + 10)
        self.assertEqual(out["idle"], 800)

    def test_iowait_is_not_busy(self):
        """800 idle + 30 iowait 都不算忙，只有 user/nice/system 等算。"""
        with _patch_proc({"/proc/stat": self.STAT}):
            out = L._read_cpu_times()
        self.assertEqual(out["busy"], out["total"] - 800 - 30)

    def test_a_missing_file_surfaces_instead_of_reading_as_zero(self):
        """``/proc`` 读不到时抛出，由 BFF 翻成 503；不能悄悄变成「CPU 空闲」。"""
        with _patch_proc({}):
            with self.assertRaises(OSError):
                L._read_cpu_times()


class MemoryTests(unittest.TestCase):
    MEMINFO = (
        "MemTotal:       32768000 kB\n"
        "MemAvailable:   16384000 kB\n"
        "SwapTotal:             0 kB\n"
        "Bogus: notanumber kB\n"
        "NoUnit:         42\n"
    )

    def test_kilobytes_become_bytes(self):
        with _patch_proc({"/proc/meminfo": self.MEMINFO}):
            out = L._read_meminfo()
        self.assertEqual(out["MemTotal"], 32768000 * 1024)
        self.assertEqual(out["NoUnit"], 42)

    def test_a_malformed_line_is_skipped_not_invented(self):
        with _patch_proc({"/proc/meminfo": self.MEMINFO}):
            out = L._read_meminfo()
        self.assertNotIn("Bogus", out)
        self.assertEqual(out["SwapTotal"], 0)


class DiskTests(unittest.TestCase):
    DISKSTATS = (
        # major minor name reads merged sectors ms writes merged sectors ms inprog io_ms weighted
        "259 0 nvme0n1 100 0 2048 40 200 0 4096 80 0 600 1000 0 0 0 0\n"
        "259 1 nvme0n1p1 50 0 1024 20 100 0 2048 40 0 300 500 0 0 0 0\n"
        "7 0 loop0 5 0 64 1 5 0 64 1 0 2 4 0 0 0 0\n"
        "253 0 dm-0 5 0 64 1 5 0 64 1 0 2 4 0 0 0 0\n"
        # 空读卡器：永远读写 0 字节。
        "8 2 sdc 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
    )

    def _read(self, real_names=("nvme0n1",), size="500"):
        exists = lambda path: path in {"/sys/block/" + n for n in real_names}  # noqa: E731
        with _patch_proc({"/proc/diskstats": self.DISKSTATS}), \
                mock.patch.object(L.os.path, "exists", side_effect=exists), \
                mock.patch("builtins.open", mock.mock_open(read_data=size + "\n")):
            return L._read_diskstats()

    def test_sectors_and_io_milliseconds_are_at_the_documented_offsets(self):
        out = self._read()
        self.assertEqual(out["nvme0n1"], {
            "read_sectors": 2048, "write_sectors": 4096, "io_ms": 600,
        })

    def test_partitions_and_pseudo_devices_are_skipped(self):
        out = self._read()
        self.assertEqual(sorted(out), ["nvme0n1"], "分区与 loop/dm 都不能进这张表")

    def test_a_zero_sized_device_is_skipped(self):
        exists = lambda path: True  # noqa: E731  # 每个名字都在 /sys/block 下
        with _patch_proc({"/proc/diskstats": self.DISKSTATS}), \
                mock.patch.object(L.os.path, "exists", side_effect=exists), \
                mock.patch("builtins.open", mock.mock_open(read_data="0\n")):
            out = L._read_diskstats()
        self.assertEqual(out, {})


class NetdevTests(unittest.TestCase):
    NETDEV = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo: 999999 1 0 0 0 0 0 0 999999 1 0 0 0 0 0 0\n"
        "enp1s0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n"
    )

    def test_loopback_is_not_external_bandwidth(self):
        with _patch_proc({"/proc/net/dev": self.NETDEV}):
            out = L._read_netdev()
        self.assertEqual(list(out), ["enp1s0"])
        self.assertEqual(out["enp1s0"], {"rx_bytes": 1000, "tx_bytes": 2000})


class ConnectionTests(unittest.TestCase):
    def test_states_are_counted_from_both_families(self):
        tcp = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
            "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000\n"
            "   1: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000\n"
        )
        tcp6 = (
            "  sl  local_address                         remote_address                        st\n"
            "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A\n"
        )
        with _patch_proc({"/proc/net/tcp": tcp, "/proc/net/tcp6": tcp6}):
            out = L._read_net_connections()
        self.assertEqual(out["listening"], 2, "IPv4 与 IPv6 都要算")
        self.assertEqual(out["established"], 1)
        self.assertEqual(out["total"], 3)


class ThermalTests(unittest.TestCase):
    def test_the_hottest_core_wins_not_the_average(self):
        files = {
            "/sys/class/hwmon/hwmon0/name": "coretemp\n",
            "/sys/class/hwmon/hwmon0/temp1_input": "45000\n",
            "/sys/class/hwmon/hwmon0/temp2_input": "95000\n",
            "/sys/class/hwmon/hwmon1/name": "nvme\n",
            "/sys/class/hwmon/hwmon1/temp1_input": "41000\n",
            "/sys/class/hwmon/hwmon1/fan1_input": "1800\n",
        }

        def listdir(path):
            if path == "/sys/class/hwmon":
                return ["hwmon0", "hwmon1"]
            return [name for name in ("temp1_input", "temp2_input", "fan1_input")
                    if path + "/" + name in files]

        with _patch_proc(files), mock.patch.object(L.os, "listdir", side_effect=listdir):
            out = L._read_thermal()
        self.assertEqual(out["cpu_c"], 95.0)
        self.assertEqual(out["nvme_c"], 41.0)
        self.assertEqual(out["fans_rpm"], [1800])

    def test_no_sensors_is_none_not_zero(self):
        with mock.patch.object(L.os, "listdir", side_effect=OSError("nope")):
            out = L._read_thermal()
        self.assertIsNone(out["cpu_c"])
        self.assertIsNone(out["nvme_c"])
        self.assertEqual(out["sensors"], [])


class RateTests(unittest.TestCase):
    def test_a_counter_that_went_backwards_is_unknown(self):
        """设备被重置或计数器回绕时，差值是负的；那不是一次负吞吐。"""
        self.assertIsNone(L._rate(10, 20, 1.0))

    def test_a_normal_delta_is_per_second(self):
        self.assertEqual(L._rate(120, 20, 2.0), 50.0)

    def test_a_zero_window_is_unknown(self):
        self.assertIsNone(L._rate(120, 20, 0))


class _ScriptedReader(L.SystemResourceReader):
    """把采样换成脚本：一次 ``summary()`` 要两份样本，按顺序发。"""

    def __init__(self, samples):
        super().__init__()
        self._samples = list(samples)

    def _sample(self):
        sample = self._samples.pop(0)
        sample = dict(sample)
        sample["at"] = self._samples_count
        self._samples_count += 1
        return sample

    _samples_count = 0


def _sample(*, cpu=(100, 100), disk=None, net=None, procs=None):
    return {
        "cpu": {"total": cpu[0], "busy": cpu[1], "idle": cpu[0] - cpu[1], "iowait": 0},
        "mem": {"MemTotal": 16 * 1024 ** 2, "MemAvailable": 8 * 1024 ** 2,
                "SwapTotal": 0, "SwapFree": 0},
        "disk": disk or {},
        "net": net or {},
        "procs": procs or {},
    }


class SummaryTests(unittest.TestCase):
    def _summary(self, reader, *, gpu=None, disk_usage=None, thermal=None,
                 connections=None, engine=None):
        patches = [
            mock.patch.object(L, "_read_gpu_processes", return_value=gpu),
            mock.patch.object(L, "_read_disk_usage", return_value=disk_usage or []),
            mock.patch.object(L, "_read_thermal", return_value=thermal or {
                "cpu_c": None, "nvme_c": None, "fans_rpm": [], "sensors": []}),
            mock.patch.object(L, "_read_net_connections", return_value=connections or {
                "listening": 0, "established": 0, "time_wait": 0, "total": 0}),
            mock.patch.object(L, "_read_engine_metrics", return_value=engine or {
                "reachable": False}),
            mock.patch.object(L.os, "getloadavg", return_value=(0.5, 0.4, 0.3)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return reader.summary()

    def test_the_shape_says_where_it_came_from_and_that_it_is_read_only(self):
        reader = _ScriptedReader([_sample(), _sample(cpu=(300, 250))])
        out = self._summary(reader)
        self.assertEqual(out["schema"], L.SYSTEM_SCHEMA)
        self.assertEqual(out["source"], L.SYSTEM_SOURCE)
        self.assertFalse(out["synthetic"])
        self.assertTrue(out["readonly"])

    def test_cpu_utilization_comes_from_the_delta_not_the_total(self):
        reader = _ScriptedReader([_sample(), _sample(cpu=(300, 250))])
        out = self._summary(reader)
        # 200 tick 里 150 忙 ⇒ 0.75
        self.assertAlmostEqual(out["cpu"]["utilization_ratio"], 0.75, places=4)

    def test_an_unknown_vram_attribution_is_null_with_a_note(self):
        """nvidia-smi 没给按进程清单时，页面不能显示成「没人用显存」。"""
        reader = _ScriptedReader([
            _sample(procs={1000: {"cpu_ticks": 10, "rss_bytes": 1024 ** 2, "processes": 1}}),
            _sample(procs={1000: {"cpu_ticks": 20, "rss_bytes": 1024 ** 2, "processes": 1}}),
        ])
        out = self._summary(reader, gpu=None)
        self.assertIsNone(out["gpu_processes"])
        self.assertIsNone(out["per_user"][0]["vram_mib"])
        self.assertTrue(any("显存" in note for note in out["notes"]))

    def test_a_known_empty_gpu_is_an_empty_list_not_null(self):
        """读到了、卡上确实没有计算进程 ⇒ ``[]``；这和「没读到」不是一件事。"""
        reader = _ScriptedReader([_sample(), _sample()])
        out = self._summary(reader, gpu=[])
        self.assertEqual(out["gpu_processes"], [])


if __name__ == "__main__":
    unittest.main()
