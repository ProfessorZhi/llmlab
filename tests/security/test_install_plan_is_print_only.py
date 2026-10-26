"""装机脚本必须是**打印器**，不是执行器。

``deploy/install/llmlab-install.sh`` 要动的东西全在仓库禁区里：
``/etc/systemd/user`` 的单元、``/etc/tmpfiles.d``、``loginctl`` 的 linger。
``docs/access-boundaries.md`` §6 说得很直接——本仓库不创建 systemd 单元、不注册
定时任务，「让服务跑起来」属于部署流程，需要独立授权。

所以这条用例不看脚本「写得对不对」，看它**有没有可能执行**：测试把 ``sudo`` /
``systemctl`` / ``loginctl`` / ``systemd-tmpfiles`` / ``rm`` / ``mkdir`` 全换成
会留下标记的命令，放进 PATH 最前面，再跑一遍。只要脚本真的调用了其中任何一个，
标记文件就会出现——那时这条用例就该红，而不是等到某次试跑时才发现。

``--apply`` 必须被拒绝：一个「先跑跑看」的开关是这类脚本最常见的破口。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "install" / "llmlab-install.sh"

#: 如果脚本真的执行了安装，它会用到这些当中的一个（或它们的绝对路径版本）。
_POISONED = ("sudo", "systemctl", "loginctl", "systemd-tmpfiles", "rm", "mkdir", "install")


class PrintOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.marker = self.tmpdir / "executed-anything"
        self.bin = self.tmpdir / "bin"
        self.bin.mkdir()
        for name in _POISONED:
            script = self.bin / name
            script.write_text(
                "#!/bin/sh\n"
                f'echo "$0 $*" >> "{self.marker}"\n'
                "exit 0\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

    def run_plan(self, *argv: str, expect: int = 0) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        result = subprocess.run(
            ["bash", str(SCRIPT), *argv],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        self.assertFalse(
            self.marker.exists(),
            "装机脚本执行了系统命令：\n" + (self.marker.read_text(encoding="utf-8") if self.marker.exists() else ""),
        )
        return result

    def test_the_install_plan_prints_and_runs_nothing(self) -> None:
        out = self.run_plan().stdout

        self.assertIn("只打印，不执行", out)
        self.assertIn("d /run/llmlab 0755", out)
        self.assertIn("/etc/systemd/user/llmlab-agent.service", out)
        self.assertIn("/etc/tmpfiles.d/llmlab.conf", out)
        self.assertIn("/run/llmlab/agent.sock", out)
        self.assertIn("cli.llmlab agent --watch", out)

    def test_the_plan_never_enables_anyone_on_its_own(self) -> None:
        """enable 的命令是**给用户看的**，不是脚本会跑的。"""

        out = self.run_plan().stdout

        self.assertIn("systemctl --user enable llmlab-agent", out)
        self.assertIn("loginctl enable-linger <user>", out)
        self.assertIn("不代任何用户 enable", out)
        # 一个人名都不该出现在计划里，更不该出现在一条循环里。
        self.assertNotIn("for user in", out)

    def test_the_uninstall_plan_is_the_reverse_order(self) -> None:
        out = self.run_plan("--uninstall").stdout

        stop = out.index("disable --now llmlab-agent")
        remove_unit = out.index("rm -f /etc/systemd/user/llmlab-agent.service")
        remove_conf = out.index("rm -f /etc/tmpfiles.d/llmlab.conf")
        self.assertLess(stop, remove_unit)
        self.assertLess(remove_unit, remove_conf)

    def test_apply_is_refused_with_a_reason(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--apply"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )

        self.assertEqual(result.returncode, 3)
        self.assertIn("拒绝执行", result.stderr)
        self.assertIn("access-boundaries", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_cadence_that_makes_no_sense_is_a_usage_error(self) -> None:
        for argv in (("--interval", "5"), ("--interval", "often"), ("--nope",)):
            with self.subTest(argv=argv):
                self.run_plan(*argv, expect=2)

    def test_the_plan_follows_the_arguments_it_is_given(self) -> None:
        elsewhere = str(self.tmpdir / "checkout")
        out = self.run_plan(
            "--repo", elsewhere, "--socket-dir", "/tmp/llmlab-run", "--daemon-user", "labd"
        ).stdout

        self.assertIn(f"WorkingDirectory={elsewhere}", out)
        self.assertIn("d /tmp/llmlab-run 0755 labd labd -", out)
        self.assertIn("--socket /tmp/llmlab-run/agent.sock", out)
        # 换了目录，平台侧也得知道去找哪儿，否则两边各说各的。
        self.assertIn("--agent-socket /tmp/llmlab-run/agent.sock", out)

    def test_the_host_scan_plan_is_printed_and_runs_nothing(self) -> None:
        """「管理员代跑」同样只是清单：root 的每一件事都由人去做。"""

        out = self.run_plan("--host-scan").stdout

        # 前置：代码得放到谁都读得到的地方，否则每个账户各撞一次 ModuleNotFoundError。
        self.assertIn("/opt/llmlab", out)
        self.assertIn("chmod -R a+rX,go-w", out)
        # 通道目录的属主是**运行 BFF 的那个账户**，不是服务账户 llmlab。
        self.assertIn("d /run/llmlab 0755 fjd fjd -", out)
        self.assertIn("/etc/systemd/system/llmlabd-inventory.service", out)
        self.assertIn("/etc/systemd/system/llmlabd-inventory.timer", out)
        self.assertNotIn("for user in", out)

    def test_the_host_scan_plan_stays_out_of_the_execution_path(self) -> None:
        """计划里出现的 `sudo systemctl enable --now` 是给人看的，不是脚本去跑的。"""

        self.run_plan("--host-scan")
        # run_plan 已经断言了标记文件不存在；这里再确认 --host-scan 也不会被
        # --apply 那种「先跑跑看」的开关绕过。
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        result = subprocess.run(
            ["bash", str(SCRIPT), "--host-scan", "--apply"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("拒绝执行", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_help_states_the_boundary(self) -> None:
        out = self.run_plan("--help").stdout

        self.assertIn("只打印计划", out)
        self.assertIn("--uninstall", out)


if __name__ == "__main__":
    unittest.main()
