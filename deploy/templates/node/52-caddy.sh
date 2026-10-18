#!/usr/bin/env bash
# Install the reverse proxy -- the only thing on this host that listens on a
# routable address.
#
# INSTALL-ONLY.  This script fetches Caddy, renders the Caddyfile and validates
# it.  It does not start it.  Starting it is `labctl start caddy`, which is the
# same path every other service takes, and keeping that separation means this
# script can be re-run against a live host to update the binary without
# bouncing anybody's console.
#
# THE TWO CHECKS THAT MATTER and where they live:
#
#   * the artifact is verified twice -- against a value pinned in this file,
#     which is the gate and fails closed, and against the publisher's own
#     sha512sums, which catches a hand-edited pin.  Caddy publishes *sha512*,
#     not sha256; a sha256 pin would never have matched and the mismatch would
#     have read as a corrupted download.
#
#   * the rendered Caddyfile is validated by `caddy validate` before anyone
#     relies on it.  A Caddyfile is a program; reading it is not checking it.
#
# No secret is printed.  The generated passwords go to a 600 file and are
# echoed as a *path*; the hashes go into the Caddyfile, which is also 600.
# Nothing on this host should have a reason to put either on a terminal, where
# it would land in a scrollback buffer and in whatever logs that terminal.
set -euo pipefail

VERSION="${CADDY_VERSION:-2.11.7}"
# UNDerscore between platform and architecture.  This is spelled out and not
# derived from `uname` for a reason: it was written once as `linux-amd64` --
# which is how the *directory* inside the tarball is named -- and the release
# asset is `linux_amd64`.  One character.  Every run of this script failed with
# `curl: (22) ... 404` while hand-typed URLs in the same shell succeeded, so it
# read like an intermittent CDN and was chased as one for four rounds.  The
# preflight below now answers that question before the download instead of
# after it.
ARCH=linux_amd64
ASSET="caddy_${VERSION}_${ARCH}.tar.gz"
URL="https://github.com/caddyserver/caddy/releases/download/v${VERSION}/${ASSET}"
SUMS_URL="https://github.com/caddyserver/caddy/releases/download/v${VERSION}/caddy_${VERSION}_checksums.txt"

# From the publisher's caddy_2.11.7_checksums.txt.  Independently reproduced
# 2026-10-10 by downloading the tarball over a different network path and
# hashing it: same byte count, same digest.
PINNED_SHA512="a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7"

BASE={{LAB_ROOT}}/services/caddy
SECRETS={{LAB_ROOT}}/services/secrets
AUTH_ENV="$SECRETS/caddy-auth.env"
LIVE={{LAB_ROOT}}/llm-lab
TMPL="$LIVE/deploy/templates/caddy/Caddyfile.tmpl"
LISTEN_ADDR="${CADDY_LISTEN:-{{LAB_HOST}}:8443}"
# Derived, not a second literal.  The site address's host is a TLS/match name
# (see the template) and says nothing about which socket gets opened, so the
# `bind` directive needs the address spelled out separately -- which is exactly
# how two values that must agree start to disagree.  %:* strips the port and
# leaves whatever the host part was.
BIND_ADDR="${LISTEN_ADDR%:*}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "=== prerequisites ==="
[ -f "$TMPL" ] || { echo "MISSING: $TMPL -- deploy the code tree first" >&2; exit 1; }
echo "ok: Caddyfile template present at $TMPL"
echo "  listen address : $LISTEN_ADDR"
echo "  bind address   : $BIND_ADDR"
echo "  service tree   : $BASE"

echo
echo "=== obtaining the artifact ==="
# PREFLIGHT FIRST: ask the release which assets it has, and require this one to
# be among them.
#
# This exists because of the one-character bug above.  A wrong asset name
# produces a 404, and a 404 from a release CDN is indistinguishable from a
# network fault -- it took four rounds of retrying and ad-hoc curl comparisons
# to notice that the failing URL was not the URL anyone had typed by hand.  The
# manifest is a 7 KB file that answers the question directly, so the failure now
# reads "the release does not list this asset" instead of "the download broke",
# and it costs one small request to get that.
#
# `-f` is deliberately absent here: a missing manifest is a fact to report, not
# an error to die on, because the pinned digest still gates whatever arrives.
MANIFEST=""
if curl -fsSL -m 60 -o "$TMP/sums" "$SUMS_URL"; then
  MANIFEST="$TMP/sums"
  if awk -v a="$ASSET" '$2 == a {found=1} END {exit !found}' "$MANIFEST"; then
    echo "  preflight: the release lists $ASSET"
  else
    echo "REFUSING: this release does not publish an asset named $ASSET" >&2
    echo "  it publishes:" >&2
    awk '{print "    " $2}' "$MANIFEST" | grep -E 'linux' >&2
    echo "  fix the ASSET/ARCH naming above rather than the download." >&2
    exit 1
  fi
else
  echo "  preflight: manifest unavailable (offline); the pinned digest still gates the file"
fi

# Two routes to the bytes, local first.  A staged tarball moves the fetch to a
# machine whose path to the artifact is already proven and rides the pinned SSH
# channel instead of a third party's edge.  Either way the digest is the gate: a
# staged file is verified exactly as a downloaded one, so "it came from my own
# disk" buys no trust.
STAGED="${CADDY_TARBALL:-/tmp/$ASSET}"
SRC=""

if [ -f "$STAGED" ]; then
  echo "  staged tarball found: $STAGED"
  have="$(sha512sum "$STAGED" | awk '{print $1}')"
  if [ "$have" = "$PINNED_SHA512" ]; then
    echo "  staged digest matches the pin -- using it, no download"
    cp -f "$STAGED" "$TMP/$ASSET"
    SRC="staged"
  else
    echo "  staged digest does NOT match the pin -- ignoring the staged file"
    echo "    staged: ${have:0:20}..."
    echo "    pinned: ${PINNED_SHA512:0:20}..."
  fi
fi

if [ -z "$SRC" ]; then
  echo "  downloading: $URL"
  attempt=1; delay=5
  while :; do
    if curl -fsSL -m 600 -o "$TMP/$ASSET" "$URL"; then
      echo "  downloaded on attempt $attempt"
      SRC="downloaded"
      break
    else
      rc=$?
    fi
    if [ "$attempt" -ge 4 ]; then
      echo "DOWNLOAD FAILED after $attempt attempts (curl rc=$rc)." >&2
      echo "  The manifest above listed the asset, so this is a transfer fault" >&2
      echo "  rather than a naming one.  Stage the verified tarball instead:" >&2
      echo "    (on the workstation)  deploy/templates/remote.sh --put <local>/$ASSET /tmp/$ASSET" >&2
      echo "    (then re-run)         deploy/templates/remote.sh deploy/templates/node/52-caddy.sh" >&2
      exit 1
    fi
    echo "  attempt $attempt failed (curl rc=$rc); retrying in ${delay}s"
    sleep "$delay"
    attempt=$((attempt + 1)); delay=$((delay * 3))
  done
fi

echo
echo "=== verifying (sha512: it is what this publisher ships) ==="
actual="$(sha512sum "$TMP/$ASSET" | awk '{print $1}')"
echo "  expected (pinned): ${PINNED_SHA512:0:20}...${PINNED_SHA512: -10}"
echo "  actual            : ${actual:0:20}...${actual: -10}"
if [ "$actual" != "$PINNED_SHA512" ]; then
  echo "CHECKSUM MISMATCH -- refusing to install." >&2
  echo "  Either a corrupted download or a version whose digest was not updated" >&2
  echo "  in this script.  Note the algorithm: caddy publishes sha512, and a" >&2
  echo "  sha256 value pasted here would never match." >&2
  exit 1
fi
echo "  pinned digest: MATCH"

if [ -n "$MANIFEST" ]; then
  published="$(awk -v a="$ASSET" '$2 == a {print $1}' "$MANIFEST" | head -1)"
  if [ -n "$published" ] && [ "$published" != "$PINNED_SHA512" ]; then
    echo "PUBLISHED DIGEST DISAGREES with the pinned one -- refusing." >&2
    echo "  published: $published" >&2
    echo "  pinned   : $PINNED_SHA512" >&2
    exit 1
  fi
  echo "  published digest: $([ -n "$published" ] && echo MATCH || echo '-- not listed; the pin stands')"
else
  echo "  published digest: unavailable (offline); the pinned value still gated it"
fi

echo
echo "=== installing into $BASE ==="
tar -xzf "$TMP/$ASSET" -C "$TMP"
src="$TMP/caddy"
[ -f "$src" ] || { echo "unexpected tarball layout" >&2; ls -la "$TMP" >&2; exit 1; }
mkdir -p "$BASE" "$BASE/data" "$BASE/logs" "$BASE/config"
install -m 0755 "$src" "$BASE/caddy"
printf '%s\n' "$VERSION" > "$BASE/VERSION"
# 700 on the data directory, because this is where the local CA's *private key*
# lives.  A directory that holds a signing key gets the mode that says so.
chmod 700 "$BASE/data"
chmod 700 "$BASE/logs"
"$BASE/caddy" version

echo
echo "=== credentials ==="
# Idempotent on purpose: re-running this script must NOT rotate the password.
# Rotating silently would leave the operator holding a password that no longer
# works, with nothing on screen to say why -- and the Caddyfile in front of them
# would look correct.
mkdir -p "$SECRETS"
chmod 700 "$SECRETS"
gen() { head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24; }
if [ -f "$AUTH_ENV" ]; then
  echo "  $AUTH_ENV exists -- reusing the existing credentials"
  ADMIN_PW="$(sed -n 's/^CADDY_ADMIN_PASSWORD=//p' "$AUTH_ENV" | head -1)"
  VIEWER_PW="$(sed -n 's/^CADDY_VIEWER_PASSWORD=//p' "$AUTH_ENV" | head -1)"
  [ -n "$ADMIN_PW" ] && [ -n "$VIEWER_PW" ] || {
    echo "REFUSING: $AUTH_ENV exists but does not hold both passwords." >&2
    echo "  Delete it to generate a fresh pair, or repair it.  Generating over" >&2
    echo "  the top would leave a half-written file and no way to tell which" >&2
    echo "  password is in force." >&2
    exit 1
  }
else
  echo "  generating two credentials: 'admin' (all routes) and 'viewer' (read-only)"
  ADMIN_PW="$(gen)"; VIEWER_PW="$(gen)"
  umask 077
  cat > "$AUTH_ENV" <<EOF
# LLM Lab reverse-proxy credentials.
#
# 'admin' may reach every route, including the service start/stop API.
# 'viewer' may reach the console and the dashboards and NOTHING that changes
# state -- that split is enforced by the proxy (see deploy/templates/caddy), not by
# the portal, so it holds even for a route the portal has not been told about.
#
# Read these with:   sed -n 's/^CADDY_ADMIN_PASSWORD=//p' $AUTH_ENV
# Change one with:   edit here, then re-run 52-caddy.sh and restart caddy.
CADDY_ADMIN_USER=admin
CADDY_ADMIN_PASSWORD=$ADMIN_PW
CADDY_VIEWER_USER=viewer
CADDY_VIEWER_PASSWORD=$VIEWER_PW
EOF
  chmod 600 "$AUTH_ENV"
fi
echo "  credentials are in $AUTH_ENV (mode $(stat -c %a "$AUTH_ENV"))"
echo "  this script does not print them; the acceptance script uses them in-process"

echo
echo "=== hashing ==="
# Through stdin, never argv: `caddy hash-password --plaintext X` puts X in the
# process table, where any user on this host can read it while it runs.  The
# newline matters -- `printf '%s'` without one sends the password and then EOF,
# and caddy abandons the read on the EOF and prints `{"msg":"EOF"}` on stderr
# and an empty string on stdout.  The first version did exactly that, and the
# only reason it was caught is that Caddy then refused to validate a config with
# empty `basic_auth` entries; a quieter validator would have installed a proxy
# that authenticates everyone.
hash_of() {
  h="$(printf '%s\n' "$1" | "$BASE/caddy" hash-password)"
  case "$h" in
    "") echo "REFUSING: caddy returned an empty hash" >&2; exit 1 ;;
    *'|'*|*'&'*|*'\'*) echo "REFUSING: the hash contains a character the renderer treats specially" >&2; exit 1 ;;
  esac
  printf '%s' "$h"
}
ADMIN_HASH="$(hash_of "$ADMIN_PW")"
VIEWER_HASH="$(hash_of "$VIEWER_PW")"
echo "  admin  hash: ${ADMIN_HASH:0:7}...(${#ADMIN_HASH} chars, bcrypt)"
echo "  viewer hash: ${VIEWER_HASH:0:7}...(${#VIEWER_HASH} chars, bcrypt)"

echo
echo "=== rendering the Caddyfile ==="
# sed with `|` as the delimiter.  bcrypt output is [./A-Za-z0-9$] only, and the
# guard above has already rejected the three characters sed would reinterpret.
umask 077
sed -e "s|{{LISTEN_ADDR}}|$LISTEN_ADDR|g" \
    -e "s|{{BIND_ADDR}}|$BIND_ADDR|g" \
    -e "s|{{ADMIN_HASH}}|$ADMIN_HASH|g" \
    -e "s|{{VIEWER_HASH}}|$VIEWER_HASH|g" \
    "$TMPL" > "$BASE/Caddyfile"
chmod 600 "$BASE/Caddyfile"

leftover="$(grep -c '{{' "$BASE/Caddyfile" || true)"
if [ "$leftover" != "0" ]; then
  echo "REFUSING: $leftover placeholder(s) survived rendering:" >&2
  grep -n '{{' "$BASE/Caddyfile" >&2
  exit 1
fi
echo "  no placeholder remains"

echo
echo "=== formatting, then validating with caddy itself, not by eye ==="
# `fmt --overwrite` rather than `fmt --diff`: the rendered file is assembled from
# a template by substitution, so its whitespace is whatever the template happens
# to have.  Letting Caddy canonicalise it means the file on disk is exactly what
# Caddy would write, which makes a future diff against a hand-edit meaningful
# instead of noisy.  Comments survive; the template's prose is intact.
"$BASE/caddy" fmt --overwrite "$BASE/Caddyfile"
if "$BASE/caddy" fmt --diff "$BASE/Caddyfile" >/dev/null 2>&1; then
  echo "  canonical form: yes"
else
  echo "  WARNING: still not canonical after fmt --overwrite" >&2
fi

"$BASE/caddy" validate --config "$BASE/Caddyfile" --adapter caddyfile
echo "  caddy validate: OK"

echo
echo "=== installed ==="
ls -l "$BASE/caddy" "$BASE/Caddyfile"
echo "  data/ and logs/ are mode $(stat -c %a "$BASE/data") / $(stat -c %a "$BASE/logs")"
if [ -f "$BASE/data/pki/authorities/local/root.crt" ]; then
  echo "  local CA root: $BASE/data/pki/authorities/local/root.crt"
else
  echo "  local CA root: not created yet -- Caddy makes it on first start"
fi
echo
echo "VERDICT: PASS -- caddy $VERSION installed and its config validates."
echo "NEXT:  labctl start caddy     (this script deliberately does not start it)"
