#!/usr/bin/env bash
# Create the isolated service tree.  Idempotent: safe to re-run.
#
# Everything lives under {{LAB_ROOT}}/services because that is the one
# subtree on this box the `{{LAB_USER}}` account can write to without sudo.  /srv and /
# are root-owned, so no new top-level directory is possible.
#
# Nothing here touches {{STRATA_ROOT}}, {{STRATA_DATA_ROOT}}, or the Strata process.
set -euo pipefail

BASE={{LAB_ROOT}}/services

# 2770 + setgid: the group keeps write access for teammates, and the setgid bit
# means files created later inherit {{LAB_GROUP}} rather than a creator's private
# group.  That matters because the key file is written by {{LAB_ADMIN_USER}} and read by {{LAB_USER}}.
mkdir -p "$BASE"/{secrets,evalscope,prometheus,grafana,metrics-adapter,run,logs}
chmod 2770 "$BASE" "$BASE"/{secrets,run,logs}
chmod 2775 "$BASE"/{evalscope,prometheus,grafana,metrics-adapter}

# secrets/ is the only place a credential may live.  Keep it non-world-readable
# even though the parent is already group-only.
chmod 2770 "$BASE/secrets"

cat <<EOF
tree ready at $BASE
$(ls -ld "$BASE" "$BASE"/*)
EOF

echo
echo "--- writability as this account ---"
for d in "$BASE" "$BASE"/*; do
  [ -d "$d" ] || continue
  if : > "$d/.wtest_$$" 2>/dev/null; then
    printf '%-46s writable\n' "$d"; rm -f "$d/.wtest_$$"
  else
    printf '%-46s READ-ONLY\n' "$d"
  fi
done

echo
echo "--- the credential contract this tree expects ---"
cat <<'EOF'
  path : {{LAB_ROOT}}/services/secrets/strata-key.env
  owner: {{LAB_ADMIN_USER}}:{{LAB_GROUP}}      mode: 640
  body : STRATA_API_KEY=<one of the keys the running Strata already accepts>
  note : creating a *new* key requires restarting Strata, which is forbidden,
         so this must be one of the keys already in api-keys.env.
EOF

echo
echo "--- existing disk headroom ---"
df -BG --output=avail "$BASE" | tail -1
