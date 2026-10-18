#!/usr/bin/env bash
# Install Prometheus from a pinned, checksummed release tarball.
#
# No sudo, no system package, no service unit: it lands in the service tree and
# the portal starts it like any other managed service.  That is not a shortcut
# around init -- it is the decision recorded in docs/dell-migration, and its
# cost (no crash recovery, no start-on-boot without the cron shim) is stated
# rather than hidden.
#
# The checksum is verified twice, against two independent sources:
#
#   1. the value pinned below, which is the gate.  It fails closed: if the
#      download is wrong this stops, whether or not the network can reach
#      GitHub to ask.
#   2. the publisher's own sha256sums.txt, fetched live.  This cannot stop a
#      compromised release, but it does catch the realistic failure -- a
#      version bump where the hash below was edited by hand and got a digit
#      wrong -- which otherwise shows up as "checksum mismatch" and a wasted
#      round trip.
set -euo pipefail

VERSION="${PROMETHEUS_VERSION:-3.7.3}"
ARCH=linux-amd64
BASE={{LAB_ROOT}}/services/prometheus
ASSET="prometheus-${VERSION}.${ARCH}.tar.gz"
URL="https://github.com/prometheus/prometheus/releases/download/v${VERSION}/${ASSET}"

# Obtained from the publisher's sha256sums.txt for v3.7.3.  A checksum that is
# fetched at install time from the same place as the artifact proves only that
# the transfer was intact; pinning it here is what makes it a decision.
PINNED_SHA256="fc9e5da126817438cf2820d9a7206c75e3122802ba8d20add3a6219a59ca6913"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "=== fetching prometheus $VERSION ==="
echo "  $URL"
curl -fsSL -m 600 -o "$TMP/$ASSET" "$URL"

echo
echo "=== verifying ==="
actual="$(sha256sum "$TMP/$ASSET" | awk '{print $1}')"
echo "  expected (pinned): ${PINNED_SHA256:0:16}...${PINNED_SHA256: -8}"
echo "  actual          : ${actual:0:16}...${actual: -8}"
if [ "$actual" != "$PINNED_SHA256" ]; then
  echo "CHECKSUM MISMATCH -- refusing to install." >&2
  echo "  This is either a corrupted download or a version whose hash was not" >&2
  echo "  updated in this script. Verify by hand before changing the pin." >&2
  exit 1
fi
echo "  pinned hash: MATCH"

# Second opinion, from the publisher.  Absence of this file is not fatal -- the
# pin above already gated the artifact -- but a *disagreement* is.
if sums="$(curl -fsSL -m 60 "https://github.com/prometheus/prometheus/releases/download/v${VERSION}/sha256sums.txt" 2>/dev/null)"; then
  published="$(printf '%s\n' "$sums" | awk -v a="$ASSET" '$2 == a {print $1}' | head -1)"
  if [ -n "$published" ] && [ "$published" != "$PINNED_SHA256" ]; then
    echo "PUBLISHED CHECKSUM DISAGREES with the pinned one -- refusing." >&2
    echo "  published: $published" >&2
    echo "  pinned   : $PINNED_SHA256" >&2
    exit 1
  fi
  echo "  published hash: ${published:-<not listed>} $([ -n "$published" ] && echo MATCH || echo '-- not listed, pin stands')"
else
  echo "  published hash: unavailable (offline); the pinned value still gated the artifact"
fi

echo
echo "=== unpacking into $BASE ==="
mkdir -p "$BASE"
tar -xzf "$TMP/$ASSET" -C "$TMP"
src="$TMP/prometheus-${VERSION}.${ARCH}"
[ -d "$src" ] || { echo "unexpected tarball layout" >&2; ls -la "$TMP" >&2; exit 1; }

# Binaries and shared assets only.  No data, no config -- those are this host's
# state and must survive a re-run of this script, which is what makes the
# upgrade path a copy rather than a rebuild.
install -m 0755 "$src/prometheus" "$BASE/prometheus"
install -m 0755 "$src/promtool"   "$BASE/promtool"
cp -f "$src/LICENSE" "$src/NOTICE" "$BASE/" 2>/dev/null || true

# The 3.x release tarball is five files: two binaries, two licence texts and a
# sample config.  `consoles/` and `console_libraries/` shipped with 2.x and are
# gone -- the web console templates were removed in 3.0.  The first version of
# this script copied them unconditionally, so the install died on a `cp` for a
# directory that no longer exists, after the checksums had already matched and
# with an error that reads like a corrupt download.  Copied if present, so the
# same script still works against a 2.x tarball, but not required.
for extra in consoles console_libraries; do
  if [ -d "$src/$extra" ]; then
    rm -rf "$BASE/$extra"
    cp -r "$src/$extra" "$BASE/"
    echo "  copied $extra/"
  fi
done
printf '%s\n' "$VERSION" > "$BASE/VERSION"

# The scrape config is written here only if absent.  Prometheus re-reads it on
# SIGHUP, so an operator edit takes effect without a restart -- and overwriting
# it on every deploy would silently undo that edit.
mkdir -p "$BASE/data"
if [ -f "$BASE/prometheus.yml" ]; then
  echo "  prometheus.yml exists -- left as-is (an operator may have edited it)"
else
  cat > "$BASE/prometheus.yml" <<'YAML'
# Prometheus on the {{LAB_NODE}}.  One job, one target: the adapter, which is the
# only thing that knows how to read Strata.  Prometheus does not talk to Strata
# directly because Strata speaks JSON and Prometheus speaks its own line
# protocol -- that translation is the adapter's whole job, and putting it in a
# scrape config would mean putting a JSON parser in Prometheus.
global:
  # 30 s.  Strata's own hardware window is a 60-sample ring, so sampling faster
  # than that re-reads the same numbers; sampling much slower loses the shape of
  # a benchmark's GPU curve, which is the thing people come here to look at.
  scrape_interval: 30s
  scrape_timeout: 10s
  evaluation_interval: 30s

scrape_configs:
  - job_name: strata
    static_configs:
      - targets: ["localhost:9101"]
        labels:
          instance: {{LAB_NODE_ID}}
          source: strata-metrics-adapter
          host: {{LAB_HOSTNAME}}

  # Prometheus on itself.  Cheap, and it is how you tell "the disk filled up"
  # from "the exporter broke" when a series stops arriving.
  - job_name: prometheus
    scrape_interval: 60s
    static_configs:
      - targets: ["localhost:9090"]
        labels:
          instance: {{LAB_NODE_ID}}
YAML
  echo "  prometheus.yml written"
fi

echo
echo "=== validate the config with the shipped promtool, not by eye ==="
"$BASE/promtool" check config "$BASE/prometheus.yml"

echo
echo "=== installed ==="
"$BASE/prometheus" --version | head -3
ls -l "$BASE/prometheus" "$BASE/promtool" "$BASE/prometheus.yml"
du -sh "$BASE"
df -BG --output=avail "$BASE" | tail -1
