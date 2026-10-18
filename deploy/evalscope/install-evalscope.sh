#!/usr/bin/env bash
# LLM Lab - install EvalScope into the project's own virtual environment.
#
# Linux/macOS counterpart of install-evalscope.ps1. Everything lands in
# <checkout>/evaluation/.venv. The global Python interpreter is never modified:
# no --user installs, no site-packages writes, no elevation.
#
# The package index defaults to the Tsinghua mirror; override with --index-url.
#
# Usage:
#   deploy/evalscope/install-evalscope.sh
#   deploy/evalscope/install-evalscope.sh --index-url https://pypi.org/simple
set -euo pipefail

INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
PYTHON_BIN="${PYTHON_BIN:-}"
EXTRA="service"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --index-url) INDEX_URL="$2"; shift 2 ;;
        --python)    PYTHON_BIN="$2"; shift 2 ;;
        --extra)     EXTRA="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "unknown argument: $1" >&2
            exit 64 ;;
    esac
done

log() { printf '[%s] %s\n' "$1" "$2"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EVAL_DIR="$ROOT/evaluation"
VENV_DIR="$EVAL_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

echo
echo "  LLM Lab - EvalScope installer"
echo

# ---------------------------------------------------------------- base ------
if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    fi
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    log ERROR "no usable Python interpreter found. Install Python 3.10 or newer."
    exit 2
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
    log ERROR "EvalScope requires Python 3.10 or newer."
    exit 2
fi
VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
log INFO "base interpreter: $PYTHON_BIN (Python $VER)"
log INFO "note: mirror indexes can lag pypi.org; cross-check the version if it looks old."

# ---------------------------------------------------------------- venv ------
mkdir -p "$EVAL_DIR"
if [ ! -x "$VENV_PY" ]; then
    log INFO "creating virtual environment at $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    if [ ! -x "$VENV_PY" ]; then
        log ERROR "venv creation failed."
        exit 3
    fi
    log OK "venv created"
else
    log INFO "reusing existing venv at $VENV_DIR"
fi

# ------------------------------------------------------------- install ------
log INFO "upgrading pip from $INDEX_URL"
"$VENV_PY" -m pip install --upgrade pip --index-url "$INDEX_URL" \
    || log WARN "pip upgrade failed"

SPEC="evalscope[$EXTRA]"
log INFO "installing $SPEC (this pulls a large dependency set and can take a while)"
if ! "$VENV_PY" -m pip install "$SPEC" --index-url "$INDEX_URL"; then
    log ERROR "installation of $SPEC failed."
    log ERROR "Common causes: mirror missing a wheel, no network, or a Python version floor."
    exit 4
fi

# -------------------------------------------------------------- validate ----
if ! EVA_VERSION="$("$VENV_PY" -c 'import evalscope; print(evalscope.__version__)' 2>&1)"; then
    log ERROR "evalscope imported but failed: $EVA_VERSION"
    exit 5
fi
log OK "evalscope $EVA_VERSION installed"

if [ -x "$VENV_DIR/bin/evalscope" ]; then
    log OK "cli: $VENV_DIR/bin/evalscope"
else
    log WARN "evalscope console script was not created; the module is importable though."
fi

echo
log OK "done. The global Python was not modified."
echo
