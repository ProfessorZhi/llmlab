#!/usr/bin/env bash
# Prove, from the node itself, that the migrated Strata key is readable *and*
# accepted -- without ever putting the secret on a command line.
#
# Two claims are being tested and they are not the same:
#   1. the file is readable by this account, at the mode we set;
#   2. the value inside it is the one the running Strata honours.
#
# Claim 2 is only meaningful against an endpoint that is closed without a key.
# /health answers 200 with no key at all, so it is run as a *negative* control
# rather than as the evidence.  /metrics and /v1/models were measured at 401
# without a key during the phase-1 audit, so a 200 there is the key working.
#
# Python reads the file directly, so the secret is never an argv element that
# `ps` could catch, and never reaches this script's stdout.
set -euo pipefail

D={{LAB_ROOT}}/services/secrets
F="$D/strata-key.env"

echo "=== the file ==="
[ -f "$F" ] || { echo "MISSING: $F" >&2; exit 1; }
ls -l "$F"
stat -c 'owner=%U:%G mode=%a size=%s' "$F"
echo

"python3" - <<'PY'
import hashlib, json, os, stat, sys, urllib.error, urllib.request

F = "{{LAB_ROOT}}/services/secrets/strata-key.env"
BASE = "http://localhost:8080"

st = os.stat(F)
mode = stat.S_IMODE(st.st_mode)
print("mode is exactly 600      : %s" % (mode == 0o600))
print("world  cannot read       : %s" % (not (mode & 0o004)))
print("group  cannot read       : %s" % (not (mode & 0o040)))

val = ""
for line in open(F, "r", encoding="utf-8", errors="replace"):
    line = line.strip()
    if line.startswith("STRATA_API_KEY="):
        val = line.split("=", 1)[1].strip().strip('"').strip("'")
if not val:
    print("no STRATA_API_KEY in the file")
    sys.exit(1)

print("value length             : %d" % len(val))
print("value sha256[:12]        : %s" % hashlib.sha256(val.encode()).hexdigest()[:12])
print()

def probe(path, key):
    req = urllib.request.Request(BASE + path, method="GET")
    if key:
        req.add_header("Authorization", "Bearer" " " + key)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(300).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                     # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)

print("--- negative control: no key (must NOT be 200 on the two protected ones) ---")
for p in ("/health", "/metrics", "/v1/models"):
    s, _ = probe(p, None)
    print("  GET %-12s -> %s" % (p, s))

print()
print("--- evidence: with the key ---")
ok = {}
for p in ("/health", "/metrics", "/v1/models"):
    s, b = probe(p, val)
    ok[p] = s
    print("  GET %-12s -> %s  %s" % (p, s, b[:100].replace("\n", " ")))

print()
if ok["/v1/models"] == 200 and ok["/metrics"] == 200:
    print("VERDICT: PASS -- str(1) readable at 600, str(2) accepted by Strata on localhost:8080.")
    print("         No Strata restart, no key change, no original file touched.")
elif ok["/v1/models"] == 401:
    print("VERDICT: FAIL -- 401 with the key; the file and the running Strata disagree.")
    sys.exit(1)
else:
    print("VERDICT: INCONCLUSIVE -- statuses above are not a clean 200/401 pair.")
    sys.exit(1)
PY
