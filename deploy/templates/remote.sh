#!/usr/bin/env bash
# Run a node-side script over the pinned SSH channel.
#
# The channel is the same one the portal uses: project key, project known_hosts,
# StrictHostKeyChecking=yes, BatchMode.  No accept-new, ever -- a swapped host
# must fail loudly rather than be trusted.
#
# Usage:  deploy/templates/remote.sh deploy/templates/node/20-uv.sh
#         deploy/templates/remote.sh -c 'hostname; id -un'
#         deploy/templates/remote.sh --put <local> <remote-path>
#         deploy/templates/remote.sh --get <remote-path> <local>
#
# Runs from the repository root regardless of the caller's cwd.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEY="$ROOT/{{LAB_SSH_KEY_PATH}}"
KH="$ROOT/{{LAB_KNOWN_HOSTS_PATH}}"
HOST="${DELL_HOST:-{{LAB_HOST}}}"
USER_="${DELL_USER:-{{LAB_USER}}}"

for f in "$KEY" "$KH"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 2; }
done

SSH_OPTS=(
  -i "$KEY"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$KH"
  -o ConnectTimeout=20
  -o LogLevel=ERROR
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=12
)

if [ "${1:-}" = "-c" ]; then
  shift
  exec ssh "${SSH_OPTS[@]}" "$USER_@$HOST" "$@"
fi

# Streaming transfers over the same pinned channel, rather than scp.  scp needs
# its own option set, and a second option set is a second place for the key, the
# host-key policy or BatchMode to be set differently -- which is exactly the
# failure this wrapper exists to prevent.  The remote path is quoted for the
# *remote* shell, so a local `;` cannot become a remote command.
if [ "${1:-}" = "--put" ] || [ "${1:-}" = "--get" ]; then
  MODE="$1"; shift
  [ $# -eq 2 ] || { echo "usage: $0 --put <local> <remote> | --get <remote> <local>" >&2; exit 2; }
  case "$MODE" in
    --put)
      [ -f "$1" ] || { echo "no such local file: $1" >&2; exit 2; }
      case "$2" in /*) : ;; *) echo "remote path must be absolute: $2" >&2; exit 2 ;; esac
      exec ssh "${SSH_OPTS[@]}" "$USER_@$HOST" "cat > '$2'" < "$1"
      ;;
    --get)
      case "$1" in /*) : ;; *) echo "remote path must be absolute: $1" >&2; exit 2 ;; esac
      exec ssh "${SSH_OPTS[@]}" "$USER_@$HOST" "cat '$1'" > "$2"
      ;;
  esac
fi

[ $# -eq 1 ] || { echo "usage: $0 <node-script> | -c '<command>' | --put | --get" >&2; exit 2; }
[ -f "$1" ] || { echo "no such script: $1" >&2; exit 2; }

# Refuse a script with CRLF endings.  It is piped into `bash -s`, and bash on
# Linux reads the CR as part of the word -- `set -euo pipefail\r` is a command
# named `pipefail\r` that does not exist, so the script dies near the top with a
# message about an invalid option and nothing pointing at the line ending.
#
# The check counts *bytes*, not lines.  `grep -c "$(printf '\r')"` was the first
# version and it is a false negative here: measured 2026-10-10, GNU grep under
# MSYS reports 0 matching lines for a file that Python confirms holds 71 CRLF
# pairs, because it strips CR while reading.  A guard that cannot fire is worse
# than no guard, since it gets counted as coverage.  `tr -d` and `wc -c` work on
# bytes with no line semantics, which is the level this question lives at.
_raw="$(wc -c < "$1")"
_nocr="$(tr -d '\r' < "$1" | wc -c)"
if [ "$_raw" != "$_nocr" ]; then
  echo "REFUSING to send $1: it has CR characters ($((_raw - _nocr)) of them)." >&2
  echo "  Piped into bash -s, each CR stays glued to the end of its word and" >&2
  echo "  the script fails on an option name it cannot parse." >&2
  echo "  fix:  tr -d '\\r' < '$1' > '$1.lf' && mv '$1.lf' '$1'" >&2
  exit 2
fi

# Piped, not scp'd: the node never accumulates a copy of the installer scripts,
# so there is nothing left behind to go stale or to be mistaken for state.
exec ssh "${SSH_OPTS[@]}" "$USER_@$HOST" 'bash -s' < "$1"
