#!/usr/bin/env bash
# llmlab-install.sh —— 代理通道的一次性安装**计划打印器**。
#
# 这个脚本只打印，不动系统：它不建目录、不写 /etc、不装单元、不 enable、不
# linger 任何人。理由写在 docs/access-boundaries.md §6 与设计规格 D8 里 ——
# 「让服务跑起来」是独立的运维动作，不属于本仓库的初始化范围；本仓库既有的
# 服务文件也一律以「文档内嵌示例 + 只打印的演练脚本」形式存在。
#
# 它存在的价值是把「管理员到底要做什么」变成一份可审阅、可逐条复核的清单：
# 每一条都写明由谁执行、为什么需要它、验收看什么。--apply 会被明确拒绝，
# 并告诉你去哪里找批准；真正的执行由获得授权的管理员照清单手工完成。
#
# 用法：
#   deploy/install/llmlab-install.sh                  # 打印安装计划
#   deploy/install/llmlab-install.sh --uninstall      # 打印逆序卸载计划
#   deploy/install/llmlab-install.sh --repo PATH \\
#       --daemon-user NAME --socket-dir PATH --interval SECONDS
#   deploy/install/llmlab-install.sh --apply          # 拒绝（退出码 3）
#
# 退出码：0 打印完成；2 用法错误；3 拒绝执行。

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
DAEMON_USER="llmlab"
SOCKET_DIR="/run/llmlab"
INTERVAL="300"
MODE="install"

usage() {
  cat <<'EOF'
用法：llmlab-install.sh [选项]

选项（全部只影响打印出来的计划，不改动系统）：
  --repo PATH         平台检出的路径（默认：本脚本所在的仓库）
  --daemon-user NAME  运行 BFF 的系统服务账户（默认：llmlab）
  --socket-dir PATH   代理通道目录（默认：/run/llmlab）
  --interval SECONDS  代理的上报间隔，最小 10（默认：300）
  --uninstall         打印逆序卸载计划，而不是安装计划
  --host-scan         打印「管理员代跑」计划（root 发起，见 §22.8.1）
  --apply             会被拒绝：本仓库不执行安装（退出码 3）
  -h, --help          显示这段说明

本脚本只打印计划。安装/卸载由获得授权的管理员照清单手工执行。
EOF
}

die_usage() {
  printf 'llmlab-install: %s\n\n' "$1" >&2
  usage >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)         [ $# -ge 2 ] || die_usage "--repo 需要一个路径"; REPO=$2; shift 2 ;;
    --daemon-user)  [ $# -ge 2 ] || die_usage "--daemon-user 需要一个名字"; DAEMON_USER=$2; shift 2 ;;
    --socket-dir)   [ $# -ge 2 ] || die_usage "--socket-dir 需要一个路径"; SOCKET_DIR=$2; shift 2 ;;
    --interval)     [ $# -ge 2 ] || die_usage "--interval 需要一个秒数"; INTERVAL=$2; shift 2 ;;
    --uninstall)    MODE="uninstall"; shift ;;
    --host-scan)    MODE="host-scan"; shift ;;
    --apply)
      cat >&2 <<'EOF'
llmlab-install: 拒绝执行（--apply）。

本脚本只打印计划。这里刻意不提供执行路径，因为安装要动的东西都在本仓库的
禁区里（docs/access-boundaries.md §6）：systemd 单元、/etc/tmpfiles.d、
loginctl linger、以及「以谁的账户跑」。这些是独立的运维动作，需要单独的
书面批准与一次真实执行，而不是一次开发提交的副作用。

请把上面打印的清单交给获得授权的管理员，逐条手工执行并留存记录。
项目的方案与决策见 docs/design-lab-resource-agent-and-service-sharing-2026-10-23.md §2。
EOF
      exit 3 ;;
    -h|--help)      usage; exit 0 ;;
    *)              die_usage "认不出的参数：$1" ;;
  esac
done

case "$INTERVAL" in
  ''|*[!0-9]*) die_usage "--interval 必须是整数秒" ;;
esac
if [ "$INTERVAL" -lt 10 ]; then
  die_usage "--interval 不能小于 10 秒：扫描会走目录树，太勤没有意义"
fi

SOCKET="${SOCKET_DIR%/}/agent.sock"
UNIT_FILE="/etc/systemd/user/llmlab-agent.service"
TMPFILES_FILE="/etc/tmpfiles.d/llmlab.conf"

# 打印用的分隔小工具：计划要能被逐条引用，所以每一步都有编号与标题。
step() { printf '\n== %s ==\n' "$1"; }
note() { printf '   %s\n' "$1"; }
cmd()  { printf '     $ %s\n' "$1"; }
file() { printf '     # %s\n' "$1"; }

print_header() {
  cat <<EOF
llmlab 代理通道 · 计划（只打印，不执行）
   仓库          ${REPO}
   通道 socket   ${SOCKET}
   socket 目录   ${SOCKET_DIR}
   服务账户      ${DAEMON_USER}（BFF 以它运行；目录归它所有，它才能 bind）
   上报间隔      ${INTERVAL} 秒
EOF
}

# 说明为什么每一步存在。清单的价值一半在命令，一半在「不做会怎样」。
print_why() {
  cat <<'EOF'

为什么需要这几步：
   BFF 以服务账户运行，读不到其他用户的 0700 HOME，所以「别人机器上跑着什么」
   它自己扫不出来。每用户代理以**用户本人身份**运行，内核在 unix socket 的
   connect() 时把 uid 填进 SO_PEERCRED；平台只认这个 uid，代理不持有任何令牌。
   目录由 root 建、归服务账户所有，是为了让 socket 只有一个合法的创建者；
   socket 文件本身是 0666 —— 本机谁都能连，但只有受信 uid 能过身份这一步。
EOF
}

print_install() {
  print_header
  print_why

  step "1/4 建目录（执行者：root）"
  note "tmpfs 里的目录，重启后由 systemd-tmpfiles 重建，不留残留。"
  file "写入 ${TMPFILES_FILE}（下一行起重一个字符都不要改）："
  printf '%s\n\n' "d ${SOCKET_DIR} 0755 ${DAEMON_USER} ${DAEMON_USER} -"
  cmd "sudo systemd-tmpfiles --create ${TMPFILES_FILE}"
  note "验收：ls -ld ${SOCKET_DIR}  → 属主 ${DAEMON_USER}，权限 drwxr-xr-x"

  step "2/4 全局用户级单元（执行者：root 写入，每个用户各自加载）"
  note "放在 /etc/systemd/user/ 下，所有账户共享同一份定义；它没有 User= ——"
  note "用户级单元本来就以该用户自己的身份运行，这正是 SO_PEERCRED 要的事实。"
  file "写入 ${UNIT_FILE}（下一行起原样复制）："
  cat <<EOF
[Unit]
Description=llmlab agent —— 以本账户身份上报本机推理服务与模型
Documentation=file:${REPO}/docs/registry-cli-contract.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO}
Environment=PYTHONPATH=${REPO}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m cli.llmlab agent --watch --interval ${INTERVAL} --socket ${SOCKET}
Restart=on-failure
RestartSec=120
# 它没有令牌，也不需要；不应该有任何方式让它提权。
NoNewPrivileges=yes
PrivateTmp=yes
EOF
  printf '\n'
  note "它是只读的观察者：--watch 每 ${INTERVAL} 秒扫一次自家目录与 /proc/net/tcp，"
  note "以 restrict_uid 只看自己的监听。不写任何用户文件，不登记任何服务。"
  note "ExecStart 里的 /usr/bin/python3 是本仓库实际验证过的那条路径；"
  note "换机器时先 command -v python3 对一下，别让单元指向一个不存在的解释器。"

  step "3/4 逐用户启用（执行者：该用户本人或管理员，一人一次）"
  note "本仓库不代任何用户 enable，也不循环遍历用户列表："
  note "「谁被看见」是那个人的选择，不是一次批量操作的结果。"
  note "对每一个要上报的账户（<user>）执行："
  cmd "sudo loginctl enable-linger <user>"
  cmd "sudo -u <user> systemctl --user enable llmlab-agent"
  cmd "sudo -u <user> systemctl --user start  llmlab-agent"
  note "enable-linger 让用户不登录时单元也能跑；不 enable 过的人不会被看见。"

  step "4/4 验收（执行者：该用户）"
  cmd "llmlab agent --dry-run        # 打印将要上报的载荷，不连 socket"
  cmd "llmlab agent --once           # 真发一次，看回执"
  cmd "llmlab discover               # 在「代理上报」表里看到自己"
  note "平台侧（BFF）：目录不存在时 /discovery 会写 agent_socket_dir_missing，"
  note "而 BFF 照常运行 —— 通道绑不上只记状态，不会让服务起不来。"
  if [ "${SOCKET}" != "/run/llmlab/agent.sock" ]; then
    note "这里用的是自定义路径，所以 BFF 启动时要带上：--agent-socket ${SOCKET}"
  fi

  cat <<'EOF'

不在本清单里的事（明确不做）：
   · 不改任何用户 HOME 的权限；不建共享组；不加 sudoers 规则。
   · 不碰 Caddy、Strata、Gateway、PAM 或任何既有服务的进程与配置。
   · 不移动、不删除任何人的模型文件；代理只上报它 stat 得到的路径。
EOF
}

print_uninstall() {
  print_header

  cat <<'EOF'

llmlab 代理通道 · 卸载计划（逆序，只打印，不执行）
EOF

  step "1/4 停用（执行者：每个启用过它的用户）"
  cmd "sudo -u <user> systemctl --user disable --now llmlab-agent"
  cmd "sudo loginctl disable-linger <user>"
  note "先停用户单元，再撤 linger：反过来的话单元会被重新拉起一次。"

  step "2/4 移除全局单元（执行者：root，之后各用户各自 daemon-reload）"
  cmd "sudo rm -f ${UNIT_FILE}"
  note "已加载的定义留在各用户的会话里，直到他们下一次："
  cmd "systemctl --user daemon-reload"

  step "3/4 移除目录定义（执行者：root）"
  cmd "sudo rm -f ${TMPFILES_FILE}"
  note "已经存在的目录留到下次重启；要立刻清掉就用 rmdir —— 目录非空它会失败，"
  note "不会顺手删掉任何意料之外的东西："
  cmd "sudo rm -f ${SOCKET}"
  cmd "sudo rmdir ${SOCKET_DIR}"

  step "4/4 平台侧（执行者：管理员，按发布流程）"
  note "BFF 停止时会自己 unlink socket（AgentChannel.stop），不需要手工删。"
  note "启停 BFF 属于发布与回滚，单独审批；本脚本不碰任何进程。"

  cat <<'EOF'

注意：卸载不会删除任何上报数据。agent_reports 是「发现」，不是「登记」——
   要撤销一条上报，是在平台上删那张表的行，不是动别人的文件。
EOF
}

# 上面两段里的 cmd 调用只是打印；这里把计划真正送出去。
print_apply_hint() {
  printf '\n这一步到此为止。需要执行时，请把上面清单交管理员逐条手工执行。\n'
}

# 「管理员代跑」：root 定时把每个账户的扫描送进通道。信任模型不变 —— 扫描仍以
# 本人 uid 运行（runuser），内核给的就是那个人的 uid。
print_host_scan() {
  print_header

  cat <<'EOF'

为什么是这条路：
   每用户代理要求「本人先 enable」。管理员代跑不改变归属——每份报告仍然由
   runuser -u <本人> 发起，SO_PEERCRED 里是内核给的那个人的 uid，服务端照旧
   用它去主体目录里找人。root 只决定「现在轮到谁」。
   代价是它默认扫所有人：不想要的人可以在自己家里放 ~/.config/llmlab/no-inventory。
EOF

  step "0/4 前置：代码放到任何账户都读得到的位置"
  note "仓库通常在某个人的家目录里（0700/0750），别的账户连 import cli 都做不到。"
  cmd "sudo rsync -a --delete ${REPO}/ /opt/llmlab/"
  cmd "sudo chown -R root:root /opt/llmlab"
  cmd "sudo chmod -R a+rX,go-w /opt/llmlab"
  note "验收（随便挑一个非 root 账户）："
  cmd "sudo -u <user> test -r /opt/llmlab/cli/llmlab/agent.py && echo readable"

  step "1/4 通道目录（执行者：root）"
  note "属主必须是**运行 BFF 的那个账户**：socket 由它 bind。现在这台机器上 BFF 是"
  note "fjd 的 user 单元，所以是 fjd；若按 §2 改成 llmlab 服务账户，这里同步改成 llmlab。"
  file "写入 ${TMPFILES_FILE}："
  printf '%s\n\n' "d ${SOCKET_DIR} 0755 fjd fjd -"
  cmd "sudo systemd-tmpfiles --create ${TMPFILES_FILE}"

  step "2/4 系统单元 + 定时器（执行者：root）"
  file "写入 /etc/systemd/system/llmlabd-inventory.service（原样复制）："
  cat <<EOF
[Unit]
Description=llmlab 主机盘点：以各账户本人身份上报模型与监听端口
Documentation=file:/opt/llmlab/docs/registry-cli-contract.md

[Service]
Type=oneshot
ExecStart=/opt/llmlab/deploy/inventory/host-inventory.sh
TimeoutStartSec=15min
EOF
  printf '\n'
  file "写入 /etc/systemd/system/llmlabd-inventory.timer："
  cat <<'EOF'
[Unit]
Description=每 5 分钟盘点一次本机各账户

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  printf '\n'
  cmd "sudo systemctl daemon-reload"
  cmd "sudo systemctl enable --now llmlabd-inventory.timer"
  note "没有 After=llmlab-bff.service：BFF 是**用户级**单元，跨 system/user 两个范围"
  note "排不了序。开机时谁先起来都一样——通道没开的那一轮以退出码 7 整轮中止，"
  note "5 分钟后自然重试。"

  cat <<EOF

注意：这个 timer 是本清单里的**运维动作**，不是本仓库的初始化行为
（docs/access-boundaries.md §6）。想先手工看一遍，就别装 timer：
  sudo ${REPO}/deploy/inventory/host-inventory.sh --dry-run

== 3/4 验收 ==
     sudo ${REPO}/deploy/inventory/host-inventory.sh --once     # 每个人一行
     curl -s localhost:8790/api/v2/registry/discovery | grep -o '"listening":[a-z]*'
     llmlab discover                                            # 表格里应出现各账户

== 4/4 卸载 ==
     sudo systemctl disable --now llmlabd-inventory.timer
     sudo rm -f /etc/systemd/system/llmlabd-inventory.{service,timer}
     sudo systemctl daemon-reload
     sudo rm -rf /opt/llmlab      # 只是代码副本，随时可从仓库重发
EOF
}

if [ "$MODE" = "uninstall" ]; then
  print_uninstall
  print_apply_hint
  exit 0
fi

if [ "$MODE" = "host-scan" ]; then
  print_host_scan
  print_apply_hint
  exit 0
fi

print_install
print_apply_hint
