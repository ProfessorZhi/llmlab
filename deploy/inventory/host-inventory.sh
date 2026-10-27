#!/usr/bin/env bash
# host-inventory.sh —— 管理员以 root 发起，把**每个本机用户**的
# 「我这儿跑着什么、装了什么」送进平台的代理通道。
#
# 它不改变信任模型，这是这个脚本唯一的理由。每个账户的扫描仍然以**该账户自己的
# uid** 运行（`runuser -u <name>`），unix socket 的 `SO_PEERCRED` 里填的是内核给
# 的真实 uid，服务端照旧只认这个 uid、照旧用它去主体目录里找人。root 只做一件事：
# 决定「现在轮到谁」。所以：
#
#   · 身份字段仍由服务端认证；扫描可附带内容摘要和位置元数据；
#   · 报告里的端口与路径仍然由服务端拿 /proc/net/tcp 和 stat 逐条复核；
#   · 对应平台账户存在时自动登记私有模型、安装环境和运行服务；API 接入仍由本人创建。
#
# 它同样不写任何东西：不建目录、不 chmod、不动任何人的 HOME，扫描读取模型元数据与有界的权重首尾片段，不全量读取权重。它自己不常驻——要定时跑，由管理员另建 timer 单元（见 --help 末尾）。
#
# 每个账户可以自己退出：在自己的 HOME 里放
#   ~/.config/llmlab/no-inventory
# 这个文件存在，本脚本就跳过该账户。默认是「被看见」，但选择权仍然在人手里。
#
# 用法：
#   sudo deploy/inventory/host-inventory.sh                 # 扫所有人，各报一次
#   sudo deploy/inventory/host-inventory.sh --dry-run       # 只打印，不上报
#   sudo deploy/inventory/host-inventory.sh --user kaidong  # 只扫一个人
#
# 退出码：0 全部成功（个别账户没有平台身份也算成功）；2 用法错误或不是 root；
# 7 代理通道没有开（整轮中止，不是逐人失败）。

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOCKET="/run/llmlab/agent.sock"
TIMEOUT_PER_USER="120"
MODE="report"
ONLY_USERS=()

usage() {
  cat <<'EOF'
用法：sudo host-inventory.sh [选项]

选项：
  --socket PATH      平台的代理 socket（默认：/run/llmlab/agent.sock）
  --repo PATH        平台代码的位置（默认：/opt/llmlab，不存在时退回本脚本所在仓库）
  --user NAME        只扫这个账户（可重复；默认扫 uid 1000..65533 的全部）
  --timeout SECONDS  每个账户的扫描上限（默认 120）
  --dry-run          只扫描并打印，不上报
  -h, --help         显示这段说明

它必须以 root 运行：root 是唯一能 `runuser -u <别人>` 的身份，也正是因此每个
报告里的 uid 才仍然是**内核给的那个人的 uid**。账户可以用
~/.config/llmlab/no-inventory 退出扫描。

要定时跑，由管理员另建两个文件（本脚本不创建它们）：

  /etc/systemd/system/llmlabd-inventory.service
    [Service]
    Type=oneshot
    ExecStart=/opt/llmlab/deploy/inventory/host-inventory.sh

  /etc/systemd/system/llmlabd-inventory.timer
    [Timer]
    OnUnitActiveSec=5min
    OnBootSec=2min
    [Install]
    WantedBy=timers.target

  sudo systemctl enable --now llmlabd-inventory.timer
EOF
}

REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --socket)  [ $# -ge 2 ] || { printf 'host-inventory: --socket 需要一个路径\n' >&2; exit 2; }
               SOCKET=$2; shift 2 ;;
    --repo)    [ $# -ge 2 ] || { printf 'host-inventory: --repo 需要一个路径\n' >&2; exit 2; }
               REPO=$2; shift 2 ;;
    --user)    [ $# -ge 2 ] || { printf 'host-inventory: --user 需要一个名字\n' >&2; exit 2; }
               ONLY_USERS+=("$2"); shift 2 ;;
    --timeout) [ $# -ge 2 ] || { printf 'host-inventory: --timeout 需要一个秒数\n' >&2; exit 2; }
               TIMEOUT_PER_USER=$2; shift 2 ;;
    --once)    shift ;;   # 缺省就是跑一轮；写出来是为了让脚本更好读
    --dry-run) MODE="dry"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         printf 'host-inventory: 认不出的参数：%s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$TIMEOUT_PER_USER" in
  ''|*[!0-9]*) printf 'host-inventory: --timeout 必须是整数秒\n' >&2; exit 2 ;;
esac
if [ "$TIMEOUT_PER_USER" -lt 1 ]; then
  # `timeout 0` 是「不限时」。整轮是由 timer 拉起的 oneshot，卡住的账户会把下一次
  # 触发一直挡在后面 —— 所以要一个上限，而不是没有上限。
  printf 'host-inventory: --timeout 至少 1 秒（0 表示不限时，会把定时器堵住）\n' >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  cat >&2 <<'EOF'
host-inventory: 需要 root。

不是因为 root 能读别人的东西，而是因为只有 root 能 `runuser -u <别人>`：扫描要
以**那个人的 uid** 运行，socket 那边的 SO_PEERCRED 才有意义。以 fjd 或别的账户
直接跑，报告会以你的身份入库——那是伪造归属，不是发现。
EOF
  exit 2
fi

# 代码必须放在**任何账户都读得到**的位置：默认 /opt/llmlab。仓库本身通常在被
# 家目录保护的路径下（0700/0750），别的账户连 `import cli` 都做不到。
if [ -z "$REPO" ]; then
  if [ -d /opt/llmlab/backend ]; then
    REPO=/opt/llmlab
  else
    REPO=$(cd -- "${SCRIPT_DIR}/../.." && pwd)
  fi
fi

if [ ! -d "${REPO}/cli/llmlab" ]; then
  printf 'host-inventory: %s 里没有 cli/llmlab —— 用 --repo 指向代码\n' "$REPO" >&2
  exit 2
fi

note() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# 「别人能不能读这个仓库」是这条通道最常见的失败原因，所以先自己查一遍，
# 而不是让每个账户各撞一次 ModuleNotFoundError。
first_other=""
while IFS=: read -r name _ uid _ _ home _; do
  case "$uid" in ''|*[!0-9]*) continue ;; esac
  [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ] || continue
  [ -n "$home" ] && [ -d "$home" ] || continue
  first_other=$name
  break
done < <(getent passwd)

if [ -n "$first_other" ] && ! runuser -u "$first_other" -- test -r "${REPO}/cli/llmlab/agent.py" 2>/dev/null; then
  cat >&2 <<EOF
host-inventory: $first_other 读不到 ${REPO}/cli/llmlab —— 扫描会以「找不到模块」失败。

先把代码发布到任何账户都能读的位置（root 所有、不给别人写权限）：

  sudo rsync -a --delete ${SCRIPT_DIR}/../../ ${REPO}/          # 或你批准的发布流程
  sudo chown -R root:root ${REPO}
  sudo chmod -R a+rX,go-w ${REPO}

然后重跑本脚本。
EOF
  exit 2
fi

users_matching() {
  local wanted
  for wanted in "${ONLY_USERS[@]}"; do
    if [ "$wanted" = "$1" ]; then return 0; fi
  done
  return 1
}

mode_flag=()
if [ "$MODE" = "dry" ]; then mode_flag=(--dry-run); fi

note "host-inventory: 代码 $REPO"
note "host-inventory: socket $SOCKET $( [ "$MODE" = "dry" ] && echo '（dry-run：不连 socket）' )"
note

ok=0 skipped=0 no_identity=0 failed=0

while IFS=: read -r name _ uid _ _ home _; do
  case "$uid" in ''|*[!0-9]*) continue ;; esac
  [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ] || continue
  [ -n "$home" ] && [ -d "$home" ] || continue
  if [ ${#ONLY_USERS[@]} -gt 0 ] && ! users_matching "$name"; then continue; fi

  if [ -e "${home}/.config/llmlab/no-inventory" ]; then
    printf '  %-12s %-6s 跳过（自己放下了 no-inventory）\n' "$name" "$uid"
    skipped=$((skipped + 1))
    continue
  fi

  roots=(--root "$home")
  if [ -d "/data/home/${name}" ]; then roots+=(--root "/data/home/${name}"); fi

  rc=0
  out=$(timeout --foreground -s TERM "$TIMEOUT_PER_USER" \
          runuser -u "$name" -- \
          env -i HOME="$home" PATH=/usr/bin:/bin PYTHONPATH="$REPO" \
              PYTHONDONTWRITEBYTECODE=1 \
          /usr/bin/python3 -s -B -m cli.llmlab agent \
              --socket "$SOCKET" "${mode_flag[@]}" "${roots[@]}" 2>&1) || rc=$?

  case "$rc" in
    0)
      printf '  %-12s %-6s 已上报  %s\n' "$name" "$uid" "$(printf '%s\n' "$out" | tail -n 1)"
      ok=$((ok + 1)) ;;
    3)
      printf '  %-12s %-6s 没有平台身份（服务端拒了：先给他开账户/批准）\n' "$name" "$uid"
      no_identity=$((no_identity + 1)) ;;
    7)
      printf '  %-12s %-6s 通道不通：%s\n' "$name" "$uid" "$(printf '%s\n' "$out" | tail -n 1)" >&2
      warn ""
      warn "代理通道没有开，整轮中止。依次确认："
      warn "  1) ${SOCKET%/*} 存在且属主是运行 BFF 的账户；"
      warn "  2) BFF 是以**带这条通道的代码**启动的（/discovery 里应有 agent_channel）；"
      warn "  3) 没有给 BFF 加 --no-agent-socket。"
      exit 7 ;;
    *)
      printf '  %-12s %-6s 失败（退出码 %s）：%s\n' "$name" "$uid" "$rc" "$(printf '%s\n' "$out" | tail -n 2 | tr '\n' ' ')" >&2
      failed=$((failed + 1)) ;;
  esac
done < <(getent passwd)

note
note "host-inventory: 上报 $ok · 跳过 $skipped · 无平台身份 $no_identity · 失败 $failed"

if [ "$MODE" = "dry" ]; then
  note "（dry-run：上面每一项的载荷都没有发送）"
fi
