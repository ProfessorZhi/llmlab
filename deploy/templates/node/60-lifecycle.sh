#!/usr/bin/env bash
# Give the lab a boot hook and a crash hook, using what this account has.
#
# WHAT THIS IS:  two crontab lines.  One at `@reboot`, one every five minutes
# that re-runs the same idempotent autostart.  Together they mean a service
# comes back after a reboot, and a service that dies is noticed within five
# minutes.
#
# WHAT THIS IS NOT, stated plainly because the gap is easy to paper over: this
# is not systemd.  There is no restart-with-backoff, no socket activation, no
# cgroup accounting, no dependency ordering, and no supervision between ticks.
# A crash costs up to five minutes of downtime, and a service that fails on
# every start is retried every five minutes forever rather than being parked.
# The reason for the compromise is a measured constraint, not a preference: on
# this host `{{LAB_USER}}` has no sudo and no lingering, so `systemctl --user` units
# are killed at logout and system units cannot be installed at all.
#
# The hook calls `labctl autostart`, which SKIPS anything the operator stopped
# or anything disabled in config.  A boot hook that silently reverses a stop is
# worse than no boot hook: it makes the off switch a lie.
set -euo pipefail

LIVE={{LAB_ROOT}}/llm-lab
STATE={{LAB_ROOT}}/services
LABCTL="$LIVE/deploy/templates/labctl.py"
CONF="$LIVE/config/dell-site.example.toml"
PY=/usr/bin/python3
LOG="$STATE/logs/autostart.log"

echo "=== prerequisites ==="
for f in "$LABCTL" "$CONF"; do
  [ -f "$f" ] || { echo "MISSING: $f -- deploy the code tree first" >&2; exit 1; }
done
[ -x "$PY" ] || { echo "MISSING: $PY" >&2; exit 1; }
mkdir -p "$STATE/logs"
echo "ok: labctl, site config and interpreter all present"

# A marker that survives edits to the command line: the *purpose* is the thing
# that stays constant, and grepping for it is how this stays idempotent without
# matching (and then deleting) somebody else's unrelated line.
MARK="# llm-lab-autostart"
CMD="LAB_CONFIG=$CONF $PY $LABCTL autostart"
LINE_BOOT="@reboot $CMD >> $LOG 2>&1 $MARK"
LINE_TICK="*/5 * * * * $CMD >> $LOG 2>&1 $MARK"

echo
echo "=== current crontab ==="
BEFORE="$(crontab -l 2>/dev/null || true)"
if [ -z "$BEFORE" ]; then
  echo "(empty)"
else
  printf '%s\n' "$BEFORE"
fi

echo
echo "=== what will be installed ==="
printf '%s\n' "$LINE_BOOT" "$LINE_TICK"

echo
echo "=== applying (preserving every unrelated line) ==="
# Unrelated entries are kept verbatim.  The only lines removed are ones this
# script installed previously, identified by the marker -- rewriting a crontab
# wholesale is how one becomes the last writer and silently drops work.
KEPT="$(printf '%s\n' "$BEFORE" | grep -v -- "$MARK" | grep -v '^[[:space:]]*$' || true)"
NEW="$(printf '%s\n%s\n%s\n' "$KEPT" "$LINE_BOOT" "$LINE_TICK" | sed '/^$/d')"
printf '%s\n' "$NEW" | crontab -

echo
echo "=== verify: read the crontab back and count the marked lines ==="
AFTER="$(crontab -l 2>/dev/null || true)"
COUNT="$(printf '%s\n' "$AFTER" | grep -c -- "$MARK" || true)"
UNRELATED_BEFORE="$(printf '%s\n' "$BEFORE" | grep -v -- "$MARK" | grep -c . || true)"
UNRELATED_AFTER="$(printf '%s\n' "$AFTER" | grep -v -- "$MARK" | grep -c . || true)"
printf '%s\n' "$AFTER"
echo
echo "marked lines installed : $COUNT   (expect 2)"
echo "unrelated lines before : $UNRELATED_BEFORE"
echo "unrelated lines after  : $UNRELATED_AFTER   (must be equal -- nothing else was touched)"

if [ "$COUNT" -ne 2 ]; then
  echo "VERDICT: FAIL -- the two hook lines are not both present" >&2
  exit 1
fi
if [ "$UNRELATED_BEFORE" -ne "$UNRELATED_AFTER" ]; then
  echo "VERDICT: FAIL -- the crontab edit changed lines it did not own" >&2
  exit 1
fi

echo
echo "NOTE: this is NOT systemd-grade supervision.  Recovery latency is up to"
echo "      5 minutes; there is no backoff and no dependency ordering."
echo "VERDICT: PASS -- boot hook and crash tick installed, no other line touched."
