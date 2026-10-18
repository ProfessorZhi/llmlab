#!/usr/bin/env bash
# Fetch an independent CPython 3.12 via uv -- no sudo, no system change.
#
# Why not the system python3.14: the Windows reference install runs
# evalscope 1.12.0 on 3.12.10.  Pinning the same interpreter on the node means
# the dependency resolution that is known to work is the one we reproduce,
# instead of discovering a 3.14 wheel gap in the middle of an evaluation run.
#
# The interpreter lands under ~/.local/share/uv/python and is never on the
# system PATH.  Nothing here touches /usr/bin/python3* or {{STRATA_ROOT}}/.venv.
set -euo pipefail

UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || { echo "uv missing -- run 20-uv.sh first" >&2; exit 2; }

WANT="${PYTHON_VERSION:-3.12}"
echo "installing CPython $WANT ..."
"$UV" python install "$WANT" 2>&1

echo
echo "--- interpreters uv now manages ---"
"$UV" python list 2>&1

echo
echo "--- resolve the concrete path ---"
P="$("$UV" python find "$WANT" 2>/dev/null || true)"
if [ -z "$P" ] || [ ! -x "$P" ]; then
  echo "could not resolve an interpreter for $WANT" >&2
  exit 1
fi
echo "resolved: $P"
"$P" --version
"$P" -c 'import sys, ssl, sqlite3, lzma, ctypes, hashlib
print("stdlib ok:", "ssl", ssl.OPENSSL_VERSION.split()[1], "| sqlite3", sqlite3.sqlite_version)
print("executable:", sys.executable)'
printf 'ensurepip available in this build: '
"$P" -m ensurepip --version >/dev/null 2>&1 && echo yes || echo "no (uv venvs do not need it)"

printf 'install root: '
printf '%s\n' "$P" | sed "s|$HOME/.local/share/uv/python/||; s|/bin/python.*||"
du -sh "$HOME/.local/share/uv/python" 2>/dev/null | head -1
