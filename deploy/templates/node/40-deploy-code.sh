#!/usr/bin/env bash
# Unpack a staged code archive into the service tree on the node.
#
# The archive is produced on the workstation with `git archive HEAD`, so it
# contains exactly the tracked set: no secrets file, no scratch directory, no
# venv, no `.git`.  That is the point of building it that way rather than
# tarring the working directory -- the working directory contains a live API
# key, and "remember to exclude it" is a rule that eventually gets forgotten.
#
# Expects:  /tmp/llm-lab-code.tar  already staged by `remote.sh --put`
#
# The swap is deliberately two-phase.  The new tree is unpacked beside the old
# one and checked for its entry point *before* anything is replaced, so a
# truncated or corrupt upload leaves the running service untouched instead of
# half-overwritten.  The previous tree is kept as `.prev` rather than deleted:
# a deployment that cannot be rolled back is a deployment you cannot afford to
# try.
set -euo pipefail

LIVE={{LAB_ROOT}}/llm-lab
NEW={{LAB_ROOT}}/llm-lab.new
PREV={{LAB_ROOT}}/llm-lab.prev
TAR=/tmp/llm-lab-code.tar

echo "=== staging archive ==="
[ -f "$TAR" ] || { echo "MISSING: $TAR (stage it with remote.sh --put)" >&2; exit 1; }
ls -l "$TAR"
tar -tf "$TAR" >/dev/null || { echo "archive is not readable as tar" >&2; exit 1; }
echo "entries: $(tar -tf "$TAR" | wc -l)"

echo
echo "=== refusing an archive that carries what it must not ==="
# A guard, not a formality: the archive builder is a command someone will
# eventually run by hand, and the cost of a wrong one is a credential on a
# shared filesystem.
if tar -tf "$TAR" | grep -Eq '(^|/)(secrets\.env|local\.toml|strata-key\.env|\.env)$|(^|/)id_[a-z0-9_]+$|\.pem$|\.key$'; then
  echo "REFUSING: the archive contains credential-shaped paths:" >&2
  tar -tf "$TAR" | grep -E '(^|/)(secrets\.env|local\.toml|strata-key\.env|\.env)$|(^|/)id_[a-z0-9_]+$|\.pem$|\.key$' >&2
  exit 1
fi
echo "ok: no credential-shaped paths"

echo
echo "=== unpacking beside the live tree ==="
rm -rf "$NEW"
mkdir -p "$NEW"
tar -xf "$TAR" --no-same-owner -C "$NEW"

for f in legacy/console/server.py config/dell-site.example.toml legacy/console/web/index.html; do
  [ -e "$NEW/$f" ] || { echo "REFUSING: the staged tree is incomplete, $f is absent" >&2; exit 1; }
done
echo "ok: entry point, site config and front end are all present"

echo
echo "=== swapping ==="
if [ -d "$LIVE" ]; then
  rm -rf "$PREV"
  mv "$LIVE" "$PREV"
  echo "previous tree kept at $PREV"
else
  echo "no previous tree; this is the first deployment"
fi
mv "$NEW" "$LIVE"

echo
echo "=== result ==="
ls -ld "$LIVE" "$PREV" 2>/dev/null || true
du -sh "$LIVE"
echo
echo "logical lines in the portal: $(wc -l < "$LIVE/legacy/console/server.py")"
echo "VERDICT: PASS -- code staged at $LIVE"
