#!/usr/bin/env bash
# Publish only Agent Python code, preserving all configuration and user data.
# Usage: sudo bash deploy/inventory/update-runtime.sh
set -euo pipefail

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TARGET=/opt/llmlab
TIMER=llmlabd-inventory.timer
SERVICE=llmlabd-inventory.service

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' '需要管理员在终端使用 sudo 执行，以更新 /opt/llmlab 的系统扫描代码。' >&2
  exit 2
fi
command -v rsync >/dev/null
[ -d "$TARGET/backend" ] && [ -d "$TARGET/cli" ] || {
  printf '%s\n' '目标不是已有的 /opt/llmlab 安装；本脚本只更新已有安装。' >&2
  exit 2
}
[ "$SOURCE" != "$TARGET" ] || { printf '%s\n' '请从已验证的工作仓库运行。' >&2; exit 2; }

# Do not copy external symlink targets or generated caches into privileged code.
python3 - "$SOURCE" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1])
for package in ('backend', 'cli'):
    root = source / package
    if root.is_symlink() or not root.is_dir():
        raise SystemExit('Invalid source package: ' + package)
    for path in root.rglob('*'):
        if path.is_symlink():
            raise SystemExit('Refusing a symlink in source code: ' + str(path))
PY

WAS_ACTIVE=0
if systemctl is-active --quiet "$TIMER"; then WAS_ACTIVE=1; fi
restore_timer() {
  if [ "$WAS_ACTIVE" -eq 1 ]; then systemctl start "$TIMER"; fi
}
trap restore_timer EXIT
systemctl stop "$TIMER"
if systemctl is-active --quiet "$SERVICE"; then
  printf '%s\n' '当前扫描尚未结束，请稍后重试；没有中断扫描或修改代码。' >&2
  exit 3
fi

BACKUP_DIR=/var/backups/llmlab-inventory
install -d -m 0700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/code-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar"
umask 077
tar -cf "$BACKUP" -C "$TARGET" backend cli
for package in backend cli; do
  rsync -a --chown=root:root --chmod=D755,F644 --exclude=__pycache__ --exclude='*.pyc' \
    "$SOURCE/$package/" "$TARGET/$package/"
done
printf '代码已更新，备份：%s\n' "$BACKUP"
printf '%s\n' '配置、数据库、密钥和用户目录保持原位。现在触发一轮所有者身份扫描。'
systemctl start "$SERVICE"
