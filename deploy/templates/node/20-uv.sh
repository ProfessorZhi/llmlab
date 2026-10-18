#!/usr/bin/env bash
# Install `uv` into ~/.local/bin -- no sudo, no system change.
#
# Why the release tarball and not `curl -LsSf https://astral.sh/uv/install.sh | sh`:
# piping a downloaded script into a shell executes whatever the network handed
# over, with no chance to look at it and nothing to verify against.  The tarball
# path can be checked against the publisher's own .sha256 before anything runs.
#
# Idempotent: re-running upgrades in place and says so.
set -euo pipefail

BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/uv-dist"
mkdir -p "$BIN" "$SHARE"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

want="${UV_VERSION:-latest}"
if [ "$want" = "latest" ]; then
  echo "resolving latest uv release ..."
  # Unauthenticated GitHub API; 60 req/h per IP.  Fall back to a known-good tag
  # rather than failing the whole deploy on a rate limit.
  tag="$(curl -fsS -m 30 https://api.github.com/repos/astral-sh/uv/releases/latest 2>/dev/null \
         | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$tag" ] || { tag="0.9.7"; echo "  API unavailable/rate-limited -> falling back to $tag"; }
else
  tag="$want"
fi
echo "target uv tag: $tag"

asset="uv-x86_64-unknown-linux-gnu.tar.gz"
base="https://github.com/astral-sh/uv/releases/download/$tag"

echo "downloading $asset ..."
curl -fsSL -m 300 -o "$TMP/$asset"        "$base/$asset"
curl -fsSL -m 60  -o "$TMP/$asset.sha256" "$base/$asset.sha256"

echo "verifying checksum against the publisher's .sha256 ..."
( cd "$TMP" && sha256sum -c "$asset.sha256" ) || {
  echo "CHECKSUM MISMATCH -- refusing to install" >&2
  echo "expected: $(cat "$TMP/$asset.sha256")" >&2
  echo "actual:   $(sha256sum "$TMP/$asset")" >&2
  exit 1
}

tar -xzf "$TMP/$asset" -C "$TMP"
src="$(find "$TMP" -type f -name uv -perm -u+x | head -1)"
[ -n "$src" ] || { echo "no uv binary inside the tarball" >&2; exit 1; }

install -m 0755 "$src" "$BIN/uv"
srcx="$(find "$TMP" -type f -name uvx -perm -u+x | head -1)"
[ -n "$srcx" ] && install -m 0755 "$srcx" "$BIN/uvx" || true
echo "$tag" > "$SHARE/VERSION"

echo
echo "installed: $("$BIN/uv" --version)"
printf 'binary  : '; ls -l "$BIN/uv" | awk '{print $NF, $5" bytes"}'
printf 'PATH has ~/.local/bin: '
case ":$PATH:" in *":$BIN:"*) echo yes ;; *) echo "no -- scripts must use $BIN/uv explicitly (or export PATH)" ;; esac
