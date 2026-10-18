#!/usr/bin/env bash
#
# Lane Release/Legacy #50 — preview rollback *drill*.
#
# This script is a plan printer. It reads the ordered steps from
# preview-rollback-plan.txt, states the precondition it can check read-only, and
# stops. It deliberately contains no privileged command: the lane that owns this
# file is not authorised to change system services, the reverse proxy, the
# authentication stack or anything running as another user, so the artefact it
# ships must not be able to do so by accident.
#
#   ./drill-preview-rollback.sh            # print the plan (default)
#   ./drill-preview-rollback.sh --check    # print the plan and verify preconditions
#   ./drill-preview-rollback.sh --execute  # refused, by design (see below)
#
# Exit codes
#   0  plan printed (and preconditions verified, with --check)
#   2  a read-only precondition failed
#   3  execution was requested
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLAN="$HERE/preview-rollback-plan.txt"

MODE="plan"
for arg in "$@"; do
  case "$arg" in
    --plan) MODE="plan" ;;
    --check) MODE="check" ;;
    --execute)
      cat >&2 <<'REFUSED'
Refusing to execute: a production rollback is not a drill step and needs separate
written approval, a maintenance window agreed with the other users on the host,
and a verified backup taken immediately beforehand. Nothing was changed.

Record the approval in the release issue, then follow the ordered steps by hand
from tests/release/preview-rollback-plan.txt, with docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md
open beside you.
REFUSED
      exit 3
      ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 64 ;;
  esac
done

if [ ! -f "$PLAN" ]; then
  echo "FAIL: plan file missing: $PLAN" >&2
  exit 2
fi

echo "Atelier V6 preview rollback drill"
echo "================================="

if git -C "$HERE/../.." rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "worktree HEAD : $(git -C "$HERE/../.." rev-parse HEAD)"
  echo "worktree ref  : $(git -C "$HERE/../.." rev-parse --abbrev-ref HEAD)"
else
  echo "worktree HEAD : (not a git worktree — preconditions below cannot be checked)"
  if [ "$MODE" = "check" ]; then exit 2; fi
fi

echo
echo "Plan file, printed verbatim (read from $PLAN):"
echo
nl -ba -w2 -s'  ' "$PLAN"

echo
echo "No change was made. This run only read files and printed text."

if [ "$MODE" = "check" ]; then
  echo
  echo "Read-only preconditions verified:"
  echo "  [ok] plan file readable"
  echo "  [ok] running inside a git worktree"
  if grep -q '^1\.' "$PLAN"; then
    echo "  [ok] plan is numbered"
  else
    echo "  [FAIL] plan is not numbered; a rollback must be ordered"
    exit 2
  fi
fi

exit 0
