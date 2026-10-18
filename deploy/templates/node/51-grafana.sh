#!/usr/bin/env bash
# Install Grafana OSS from a pinned, checksummed release tarball.
#
# Same shape as the Prometheus step: no sudo, no package manager, no system
# unit, everything under the service tree.  The tarball is 186 MB and it is the
# largest thing this deployment pulls -- worth knowing, and worth the disk check
# at the end rather than discovering it as a half-written extraction.
#
# No `grafana-cli plugin install`, no plugin at all.  The bundled panels cover
# every chart this deployment needs, and a plugin is a second download with its
# own signature story and its own upgrade cycle.
set -euo pipefail

VERSION="${GRAFANA_VERSION:-12.3.1}"
ARCH=linux-amd64
BASE={{LAB_ROOT}}/services/grafana
SECRETS={{LAB_ROOT}}/services/secrets
SRC={{LAB_ROOT}}/llm-lab/deploy/grafana
ASSET="grafana-${VERSION}.${ARCH}.tar.gz"
URL="https://dl.grafana.com/oss/release/${ASSET}"

# From the publisher's .sha256 for 12.3.1.  Pinned for the same reason as the
# Prometheus hash: a checksum fetched from where the artifact came from proves
# the transfer, not the artifact.
PINNED_SHA256="75d03b5ac2dc7b009625869355225d73e5b29866efbd999479bdd24429a695db"

# Staged inside the service tree rather than /tmp: /tmp is a tmpfs on many
# hosts and 186 MB is enough to fill a small one, which fails in a way that
# looks like a network problem.
STAGE="$BASE/.stage"
mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

echo "=== fetching grafana $VERSION (OSS) ==="
echo "  $URL"
curl -fsSL -m 900 -o "$STAGE/$ASSET" "$URL"
ls -l "$STAGE/$ASSET" | awk '{print "  downloaded:", $5, "bytes"}'

echo
echo "=== verifying ==="
actual="$(sha256sum "$STAGE/$ASSET" | awk '{print $1}')"
echo "  expected (pinned): ${PINNED_SHA256:0:16}...${PINNED_SHA256: -8}"
echo "  actual          : ${actual:0:16}...${actual: -8}"
if [ "$actual" != "$PINNED_SHA256" ]; then
  echo "CHECKSUM MISMATCH -- refusing to install." >&2
  exit 1
fi
echo "  pinned hash: MATCH"
if published="$(curl -fsSL -m 60 "${URL}.sha256" 2>/dev/null)"; then
  published="$(printf '%s' "$published" | tr -d '[:space:]')"
  if [ -n "$published" ] && [ "$published" != "$PINNED_SHA256" ]; then
    echo "PUBLISHED CHECKSUM DISAGREES with the pinned one -- refusing." >&2
    echo "  published: $published" >&2
    exit 1
  fi
  echo "  published hash: ${published:-<empty>} $([ -n "$published" ] && echo MATCH || echo '-- empty, pin stands')"
else
  echo "  published hash: unavailable; the pinned value still gated the artifact"
fi

echo
echo "=== unpacking ==="
mkdir -p "$STAGE/x"
tar -xzf "$STAGE/$ASSET" -C "$STAGE/x"
root="$(dirname "$(dirname "$(find "$STAGE/x" -type f -path '*/bin/grafana-server' | head -1)")")"
[ -n "$root" ] && [ -x "$root/bin/grafana-server" ] \
  || { echo "no bin/grafana-server inside the tarball" >&2; find "$STAGE/x" -maxdepth 2 >&2; exit 1; }
echo "  tarball root: $(basename "$root")"

mkdir -p "$BASE" "$BASE/data" "$BASE/logs" "$BASE/dashboards" "$BASE/conf"

# Program files are replaced; host state is not.  An existing data/ holds
# Grafana's own SQLite database (dashboards made in the browser, the admin's
# saved settings), and re-running an installer must not be the thing that
# deletes it.
echo "  replacing program files (bin, public, plugins-bundled)"
rm -rf "$BASE/bin" "$BASE/public" "$BASE/plugins-bundled"
cp -r "$root/bin" "$root/public" "$root/plugins-bundled" "$BASE/"
cp -f "$root/LICENSE" "$root/NOTICE.md" "$BASE/" 2>/dev/null || true
# defaults.ini is the documented baseline Grafana reads first.  Overwrite it
# with the shipped one: it is program documentation, not operator state, and a
# stale copy next to a new binary is how a setting appears to exist and not
# take effect.
cp -f "$root/conf/defaults.ini" "$BASE/conf/defaults.ini"
[ -f "$BASE/conf/sample.ini" ] || cp -f "$root/conf/sample.ini" "$BASE/conf/sample.ini" 2>/dev/null || true
printf '%s\n' "$VERSION" > "$BASE/VERSION"

echo
echo "=== provisioning: datasource + dashboard provider, from the checkout ==="
[ -d "$SRC" ] || { echo "missing $SRC -- run 40-deploy-code.sh first" >&2; exit 2; }
rm -rf "$BASE/conf/provisioning"
mkdir -p "$BASE/conf/provisioning/datasources" "$BASE/conf/provisioning/dashboards"
cp -f "$SRC/provisioning/datasources/prometheus.yml" "$BASE/conf/provisioning/datasources/"
cp -f "$SRC/provisioning/dashboards/llm-lab.yml"     "$BASE/conf/provisioning/dashboards/"
cp -f "$SRC/dashboards/llm-lab-overview.json"        "$BASE/dashboards/"
ls -l "$BASE/conf/provisioning/datasources" "$BASE/conf/provisioning/dashboards" "$BASE/dashboards" \
  | sed 's/^/  /'

echo
echo "=== cross-check: every metric the dashboard queries is one the adapter emits ==="
# An empty panel and a typo'd metric name look identical on screen, and the
# likely reading of an empty panel is "nothing has happened yet" -- which is
# the wrong conclusion and the one that wastes an afternoon.  So the names are
# compared against the adapter's own tables instead of being eyeballed.
/usr/bin/python3 - "$SRC/dashboards/llm-lab-overview.json" \
                   {{LAB_ROOT}}/llm-lab/deploy/templates/adapter/adapter.py <<'PY'
import importlib.util, json, re, sys

dash_path, adapter_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("llmlab_adapter", adapter_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

emitted = {mod.PREFIX + "_" + s for s, _ in mod.GAUGES.values()}
emitted |= {mod.PREFIX + "_" + s for s, _ in mod.COUNTERS.values()}
emitted |= {mod.PREFIX + "_" + k for k in
            ("up", "scrape_ok", "scrape_duration_seconds", "totals_since_timestamp_seconds", "info")}

dash = json.load(open(dash_path, encoding="utf-8"))
used: set[str] = set()
for panel in dash.get("panels", []):
    for t in panel.get("targets", []):
        used |= set(re.findall(r"llmlab_strata_[a-z0-9_]+", t.get("expr", "")))

missing = sorted(m for m in used if m not in emitted)
print("  adapter emits %d names; the dashboard queries %d of them" % (len(emitted), len(used)))
print("  unreferenced adapter metrics: %d" % len(sorted(emitted - used)))
if missing:
    print("  QUERIED BUT NEVER EMITTED: %s" % missing)
    print("  -> these panels would render empty and read as 'no activity yet'")
    raise SystemExit(1)
print("  every queried metric exists: OK")
PY

echo
echo "=== custom.ini (600 -- it holds a generated admin password) ==="
if [ -f "$BASE/conf/custom.ini" ]; then
  echo "  exists -- left as-is; delete it to regenerate the password"
else
  PW="$(/usr/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  SK="$(/usr/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  umask 077
  cat > "$BASE/conf/custom.ini" <<INI
# Grafana on the {{LAB_NODE}}.  Generated by deploy/templates/node/51-grafana.sh.
#
# Mode 600 because it carries two generated secrets.  It is deliberately NOT in
# the repository and NOT in the checkout: it is this host's state, and a file
# that exists only here cannot be leaked by a git push.
#
# root_url and serve_from_sub_path are deliberately ABSENT.  They name the
# address the *browser* uses, which is a property of the site, so they come
# from [services.grafana.env] in config/dell-site.example.toml -- GF_* environment
# variables outrank this file, so writing them here too would be a second
# source that always loses and never says so.  Started by hand, with no env,
# Grafana falls back to http://localhost:3000/ and serves the root path, which
# is the right answer for a manual run on this host.

[paths]
data         = $BASE/data
logs         = $BASE/logs
plugins      = $BASE/data/plugins
provisioning = $BASE/conf/provisioning

[database]
path = $BASE/data/grafana.db

[server]
protocol   = http
# Loopback, like every other service here.  The reverse proxy is the only
# listener on a routable address.
http_addr  = localhost
http_port  = 3000

[security]
admin_user     = admin
admin_password = $PW
secret_key     = $SK
# false because the proxy terminates TLS; Grafana itself speaks http on
# loopback, and a Secure cookie set over plain http is simply never sent back.
cookie_secure        = false
disable_gravatar     = true
# No dashboard may be framed by another site.  Embedding is how a monitoring
# page becomes a phishing surface.
allow_embedding      = false

[users]
allow_sign_up         = false
allow_org_create      = false
auto_assign_org       = true
auto_assign_org_role  = Viewer

[auth.anonymous]
enabled = false

[analytics]
reporting_enabled = false
check_for_updates = false
feedback_links_enabled = false

[log]
# console, not file: the portal captures stdout/stderr into its own log
# directory, and a service that writes to a second place is a service whose
# logs you forget to look at.
mode  = console
level = info

[snapshots]
external_enabled = false
INI
  chmod 600 "$BASE/conf/custom.ini"
  echo "  written: $BASE/conf/custom.ini (mode $(stat -c %a "$BASE/conf/custom.ini"))"
  echo "  the admin password is in it; it is not printed here and not in git"
  echo "  retrieve with:  ssh ... 'grep ^admin_password $BASE/conf/custom.ini'"
fi

echo
echo "=== installed ==="
"$BASE/bin/grafana-server" -v 2>&1 | head -3 || true
ls -l "$BASE/bin/grafana-server" "$BASE/conf/custom.ini"
du -sh "$BASE"
df -BG --output=avail "$BASE" | tail -1
