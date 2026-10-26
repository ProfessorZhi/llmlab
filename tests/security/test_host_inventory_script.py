"""管理员代跑：root 只决定「轮到谁」，扫描仍然以**那个人自己的 uid** 运行。

``deploy/inventory/host-inventory.sh`` 的全部正当性系于一点：每个账户的扫描都由
``runuser -u <name>`` 发起，于是 unix socket 的 ``SO_PEERCRED`` 里是内核给的
**真实 uid**，服务端照旧按这个 uid 去主体目录里找人。协议一个字节都没改，报告也
仍然由服务端拿 ``/proc/net/tcp`` 与 ``stat`` 逐条复核。

这条用例守的就是「只有一个身份」这件事：替身 ``runuser`` 记下每次调用的完整
argv，用例逐条断言里面写着 ``-u <那个账户>``、``HOME=<那个人的家>``、
``PYTHONPATH=<代码目录>``、``--root <那个人的家>``。任何一次「以 fjd 的身份扫
别人的目录」都会在这里变成一条看得见的 argv。

同时守两件边界：

* 它不是安装器：不建目录、不写任何人的 HOME、不碰 systemd —— 装 timer 是清单里的
  运维动作（``llmlab-install.sh --host-scan`` 只打印）；
* 不是 root 就拒绝跑：以普通账户跑出来的报告会把归属写成那个人，那是伪造。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "inventory" / "host-inventory.sh"

#: 替身 ``id`` 报的 uid。0 = 以 root 跑；1000 = 普通账户跑。
FAKE_ROOT_UID = 0
FAKE_NORMAL_UID = 1000


class HostInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.marker = self.tmpdir / "runuser-calls"
        self.bin = self.tmpdir / "bin"
        self.bin.mkdir()
        self.homes = {}
        for name in ("alice", "bob", "carol"):
            home = self.tmpdir / "home" / name
            home.mkdir(parents=True)
            self.homes[name] = home
        # 不在扫描范围内的账户：home 不是目录，脚本会跳过。
        self.fake_passwd = [
            "root:x:0:0:root:/root:/bin/bash",
            f"alice:x:1000:1000::{self.homes['alice']}:/bin/bash",
            f"bob:x:1001:1001::{self.homes['bob']}:/bin/bash",
            f"carol:x:1002:1002::{self.homes['carol']}:/bin/bash",
            "nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin",
        ]

    # ---- 替身 -------------------------------------------------------------

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def install_fakes(self, *, uid: int = FAKE_ROOT_UID, agent_rc: int = 0, readable: bool = True) -> None:
        self.fake("id", f'printf "%s\\n" {uid}\n')
        passwd = self.tmpdir / "passwd"
        passwd.write_text("\n".join(self.fake_passwd) + "\n", encoding="utf-8")
        self.fake("getent", f'cat "{passwd}"\n')
        preflight_rc = 0 if readable else 1
        self.fake(
            "runuser",
            # 预检那一次是 `runuser -u <某> -- test -r <代码>`，它不该被记成
            # 「一次扫描」；但仍然记下来，免得有人把它挪去干别的。
            f'echo "$0 $*" >> "{self.marker}"\n'
            'case " $* " in\n'
            f'  *" -- test -r "*) exit {preflight_rc} ;;\n'
            "esac\n"
            f'exit "${{FAKE_AGENT_RC:-{agent_rc}}}"\n',
        )
        # 让每个退出码都带一行「回执」，脚本会取最后一行贴到输出里。
        self.fake(
            "python3",
            'printf "agent: 已上报（替身）\\n"\n'
            'case "${FAKE_AGENT_RC:-0}" in\n'
            '  3) printf "agent: agent.uid_not_mapped\\n" >&2 ;;\n'
            '  7) printf "agent: agent.socket_unavailable\\n" >&2 ;;\n'
            "esac\n"
            'exit "${FAKE_AGENT_RC:-0}"\n',
        )

    # ---- 运行 -------------------------------------------------------------

    def run_script(self, *argv: str, expect: int = 0, agent_rc: int = 0,
                   uid: int = FAKE_ROOT_UID, readable: bool = True) -> subprocess.CompletedProcess:
        self.install_fakes(uid=uid, agent_rc=agent_rc, readable=readable)
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        env["FAKE_AGENT_RC"] = str(agent_rc)
        result = subprocess.run(
            ["bash", str(SCRIPT), *argv],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def calls(self) -> list[str]:
        if not self.marker.exists():
            return []
        return [
            line for line in self.marker.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def scans(self) -> list[str]:
        """真正跑扫描的那些调用（排除预检的 ``test -r``）。"""

        return [call for call in self.calls() if " -- test -r " not in call]

    # ---- 身份：每个账户都以自己运行 ---------------------------------------

    def test_each_account_is_scanned_as_itself(self) -> None:
        self.run_script("--repo", str(ROOT))

        scans = self.scans()
        self.assertEqual(len(scans), 3, scans)
        for name, home in self.homes.items():
            call = next((c for c in scans if f" -u {name} " in c), None)
            self.assertIsNotNone(call, f"{name} 没有以自己身份被扫过：\n" + "\n".join(scans))
            self.assertIn(f"HOME={home}", call)
            self.assertIn(f"PYTHONPATH={ROOT}", call)
            self.assertIn(f"--root {home}", call)
            self.assertIn("-m cli.llmlab agent", call)
            # 报告要能定位到通道；路径由 --socket 决定，默认在 /run 下。
            self.assertIn("--socket /run/llmlab/agent.sock", call)

    def test_nobody_scans_another_persons_home(self) -> None:
        """一条 argv 里只能出现一个人的名字与一个人的 HOME。"""

        self.run_script("--repo", str(ROOT))

        for call in self.scans():
            others = [h for name, h in self.homes.items() if f" -u {name} " not in call]
            for home in others:
                self.assertNotIn(f"HOME={home}", call, call)
                self.assertNotIn(f"--root {home}", call, call)

    def test_an_account_with_no_platform_identity_is_not_a_failure(self) -> None:
        """没有平台账户的人不该让整轮变红——他只是还没被批准。"""

        result = self.run_script("--repo", str(ROOT), agent_rc=3)

        self.assertIn("没有平台身份", result.stdout)
        self.assertIn("上报 0 · 跳过 0 · 无平台身份 3 · 失败 0", result.stdout)

    # ---- 通道 -------------------------------------------------------------

    def test_the_channel_being_down_aborts_the_whole_run(self) -> None:
        """通道没开时逐人重试只是把同一个错误刷三遍；整轮中止并给检查清单。"""

        result = self.run_script("--repo", str(ROOT), agent_rc=7, expect=7)

        self.assertIn("整轮中止", result.stderr)
        self.assertIn("agent_channel", result.stderr)
        # 第一个人失败就停手，不该继续往下跑。
        self.assertEqual(len(self.scans()), 1, self.scans())

    def test_dry_run_is_passed_through_and_reports_nothing_sent(self) -> None:
        result = self.run_script("--repo", str(ROOT), "--dry-run")

        self.assertTrue(self.scans())
        for call in self.scans():
            self.assertIn("--dry-run", call)
        self.assertIn("没有发送", result.stdout)

    # ---- 选择权仍在人手里 -------------------------------------------------

    def test_a_user_who_opts_out_is_skipped(self) -> None:
        marker = self.homes["bob"] / ".config" / "llmlab" / "no-inventory"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")

        result = self.run_script("--repo", str(ROOT))

        self.assertNotIn(" -u bob ", "\n".join(self.scans()))
        self.assertIn("no-inventory", result.stdout)
        self.assertIn("跳过 1", result.stdout)
        self.assertEqual(len(self.scans()), 2, self.scans())

    def test_only_named_accounts_are_scanned(self) -> None:
        self.run_script("--repo", str(ROOT), "--user", "carol")

        scans = self.scans()
        self.assertEqual(len(scans), 1, scans)
        self.assertIn(" -u carol ", scans[0])

    # ---- 边界 -------------------------------------------------------------

    def test_it_refuses_to_run_as_a_normal_user(self) -> None:
        result = self.run_script("--repo", str(ROOT), expect=2, uid=FAKE_NORMAL_UID)

        self.assertIn("需要 root", result.stderr)
        self.assertEqual(self.calls(), [], "以普通账户跑时会以那个人的身份入库——不是发现")

    def test_it_writes_nothing_into_anyones_home(self) -> None:
        before = {name: sorted(p.name for p in home.rglob("*")) for name, home in self.homes.items()}

        self.run_script("--repo", str(ROOT))

        after = {name: sorted(p.name for p in home.rglob("*")) for name, home in self.homes.items()}
        self.assertEqual(before, after)

    def test_an_unreadable_repo_is_named_before_anyone_runs(self) -> None:
        """最常见的失败原因是「别人读不到代码」；要在第一个账户上就说清楚。"""

        result = self.run_script("--repo", str(ROOT), expect=2, readable=False)

        self.assertIn("读不到", result.stderr)
        self.assertIn("chmod -R a+rX,go-w", result.stderr)
        self.assertEqual(self.scans(), [], "预检没过就不该开始扫")

    def test_a_bad_timeout_is_a_usage_error(self) -> None:
        for value in ("0", "often", ""):
            with self.subTest(value=value):
                self.run_script("--repo", str(ROOT), "--timeout", value, expect=2)


if __name__ == "__main__":
    unittest.main()
