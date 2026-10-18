#!/usr/bin/env bash
# Fetch the reverse proxy's local CA root to this workstation, so the browser
# can trust the console instead of being told to ignore the warning.
#
# RUN ON THE WORKSTATION, not the node.  It is the counterpart to
# deploy/templates/caddy/Caddyfile.tmpl: that file explains why `tls internal` is the
# only workable certificate scheme for a private address, and this is the step
# that turns it from "untrusted" into "trusted" without disabling anything.
#
# WHICH FILE, AND WHY IT IS SAFE TO MOVE.  This is the *root certificate*, which
# is public by construction -- it is the thing a browser is supposed to have.
# The key that signs with it stays on the node in `data/pki/authorities/local/`
# and is not fetched, not printed, and not read by this script.  A root
# certificate without its key proves nothing to anyone who holds it; installing
# it says "I trust certificates this CA signed", which is true here and is the
# only reason the browser check can pass honestly.
#
# The certificate is NOT committed to this repository.  It is generated per
# deployment: a rebuilt node mints a new CA, and a stale copy committed here
# would silently fail to validate the new one -- the kind of failure that gets
# resolved by turning verification off, which is exactly what must not happen.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_ROOT={{LAB_ROOT}}/services/caddy/data/pki/authorities/local/root.crt

# Where the operator will look for it.  Outside the checkout by default: this is
# workstation state, not project source, and `git status` should stay clean.
OUT="${1:-$HOME/llm-lab-caddy-root.crt}"

echo "=== fetching the CA root from the node ==="
"$HERE/remote.sh" --get "$REMOTE_ROOT" "$OUT"
echo "  saved: $OUT"
echo "  sha256: $(sha256sum "$OUT" | awk '{print $1}')"

echo
echo "=== the certificate says what it should ==="
if command -v openssl >/dev/null 2>&1; then
  openssl x509 -in "$OUT" -noout -subject -issuer -dates \
    | sed 's/^/  /'
  # A root that is not self-signed is the wrong file.  It happens when someone
  # follows a different Caddy version's directory layout -- the intermediate
  # lives one level over and would install without complaint, then fail to
  # validate anything.
  subj="$(openssl x509 -in "$OUT" -noout -subject | sed 's/^subject=//')"
  issr="$(openssl x509 -in "$OUT" -noout -issuer  | sed 's/^issuer=//')"
  if [ "$subj" = "$issr" ]; then
    echo "  self-signed: yes (a root, not an intermediate)"
  else
    echo "  WARNING: subject != issuer -- this is not a self-signed root." >&2
    echo "    subject: $subj" >&2
    echo "    issuer : $issr" >&2
    echo "  Installing this would not establish trust." >&2
    exit 1
  fi
else
  echo "  openssl not present; skipping the self-signed check"
fi

echo
echo "=== does the chain actually validate with it? ==="
# openssl, deliberately, and NOT curl.
#
# On Windows the curl that ships with Git Bash is built against schannel.  Point
# it at a root with --cacert and it reports "added 1 certificate(s) from CA
# file" and then fails the handshake anyway -- schannel does not take a
# filesystem trust anchor the way OpenSSL does.  Measured here: curl --cacert
# said the handshake never completed, while `openssl s_client -CAfile` on the
# same file and the same endpoint said `Verify return code: 0 (ok)` and showed
# the full chain leaf <- ECC Intermediate <- 2026 ECC Root.  One of the two was
# wrong and it was not the server.
#
# So the verification runs on the tool that can do it, and the curl result is
# reported beside it rather than used as the verdict.  Getting this the other
# way round would have sent the operator hunting a server-side certificate
# problem that does not exist.
if command -v openssl >/dev/null 2>&1; then
  out="$(openssl s_client -connect {{LAB_HOST}}:8443 -servername {{LAB_HOST}} \
         -CAfile "$OUT" </dev/null 2>&1 || true)"
  code="$(printf '%s\n' "$out" | sed -n 's/^ *Verify return code: //p' | tail -1)"
  printf '%s\n' "$out" | sed -n 's/^ *\([0-9]*\) s:/  chain[\1] subject: /p' | sed 's/^  chain/  /'
  if printf '%s' "$code" | grep -q '^0 '; then
    echo "  openssl verify: 0 (ok) -- the root completes the chain this server sends"
  else
    echo "  openssl verify: ${code:-<none>} -- FAILED" >&2
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    ccode="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$OUT" \
            --max-time 15 https://{{LAB_HOST}}:8443/api/healthz || true)"
    if [ "$ccode" = "401" ]; then
      echo "  curl (schannel): 401 -- agrees"
    else
      echo "  curl (schannel): ${ccode:-no answer} -- disagrees with openssl;"
      echo "    on Windows this build does not honour --cacert, so it is the"
      echo "    tool that is wrong here. Known and expected; nothing to fix."
    fi
  fi
else
  echo "  openssl not present; install the root and test in the browser"
fi

cat <<EOF

=== install it ===
  Windows (this workstation), current user only -- no administrator needed:
      certutil -addstore -user Root "$(cygpath -w "$OUT" 2>/dev/null || echo "$OUT")"
      (then restart the browser: it caches the trust store per process)

  Then open:
      https://{{LAB_HOST}}:8443/
  and expect a login prompt. The credentials are on the node at
      {{LAB_ROOT}}/services/secrets/caddy-auth.env   (mode 600)
  and are read there with:
      sed -n 's/^CADDY_ADMIN_PASSWORD=//p' <that file>

  Do not add an exception, and do not disable certificate checking: the whole
  reason this file exists is so that neither is necessary.
EOF
