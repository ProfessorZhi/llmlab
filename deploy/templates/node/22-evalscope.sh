#!/usr/bin/env bash
# Build the isolated EvalScope environment.
#
# Separate venv, separate config, separate outputs.  It talks to Strata over
# the existing OpenAI-compatible HTTP API and loads no model of its own, so it
# needs no GPU and no torch -- which is exactly why it can live here at all.
#
# Nothing touches {{STRATA_ROOT}}/.venv or any system package.
set -euo pipefail

UV="$HOME/.local/bin/uv"
BASE={{LAB_ROOT}}/services/evalscope
VENV="$BASE/.venv"
VERSION="${EVALSCOPE_VERSION:-1.12.0}"
INDEX="${PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

[ -x "$UV" ] || { echo "uv missing -- run 20-uv.sh first" >&2; exit 2; }

mkdir -p "$BASE"/{config,logs,outputs}

echo "=== venv ==="
if [ -x "$VENV/bin/python" ]; then
  echo "reusing existing venv: $("$VENV/bin/python" --version 2>&1)"
else
  "$UV" venv --python 3.12 "$VENV" 2>&1
fi
"$VENV/bin/python" -c 'import sys; print("venv python:", sys.version.split()[0], sys.executable)'

echo
echo "=== install evalscope==$VERSION (index: $INDEX) ==="
export UV_INDEX_URL="$INDEX"
export UV_DEFAULT_INDEX="$INDEX"
if ! "$UV" pip install --python "$VENV/bin/python" "evalscope==$VERSION" 2>&1 | tail -25; then
  echo "uv pip install failed with the mirror; retrying against upstream pypi" >&2
  unset UV_INDEX_URL UV_DEFAULT_INDEX
  "$UV" pip install --python "$VENV/bin/python" "evalscope==$VERSION" 2>&1 | tail -25
fi

echo
echo "=== the service extra, named explicitly instead of 'evalscope[all]' ==="
# `evalscope service` is the Dashboard, and its dependencies are declared only
# under the `all` extra -- which also carries torch, torchvision, open_clip_torch
# and diffusers.  Installing `[all]` to get flask would therefore pull the entire
# model stack onto a host whose whole point is that it does not run models: five
# gigabytes, a CUDA-shaped dependency surface next to a running inference server,
# and nothing that this Dashboard ever calls.  Measured on the node: the service
# needs exactly these five, and the loop below is how that set was found rather
# than guessed -- each missing import was added, one at a time, starting from
# `python -c "import evalscope.service"`.
#
#   flask          the app itself
#   plotly         the perf archive's chart rendering
#   uvicorn        evalscope.perf.utils.local_server imports it at module load
#   fastapi        pulled by the perf server; brings starlette + opentelemetry
#   sse_starlette  streaming progress for a running benchmark
SERVICE_DEPS=(flask plotly uvicorn fastapi sse_starlette)
"$UV" pip install --python "$VENV/bin/python" "${SERVICE_DEPS[@]}" 2>&1 | tail -6

echo
echo "=== verify ==="
"$VENV/bin/python" - <<'PY'
import importlib.metadata as md
print("evalscope:", md.version("evalscope"))
for name in ("numpy", "openai", "transformers", "pydantic", "litellm"):
    try:
        print(f"  {name}: {md.version(name)}")
    except md.PackageNotFoundError:
        print(f"  {name}: (absent)")
try:
    import evalscope
    print("import ok:", evalscope.__version__)
except Exception as e:
    print("IMPORT FAILED:", type(e).__name__, e)
    raise SystemExit(1)

# The claim that matters is not "evalscope imports" but "the Dashboard can
# start", and those are different imports -- the first version of this script
# proved the former and the service still died on `ModuleNotFoundError: flask`.
try:
    import evalscope.service
    print("service import ok: create_app is", evalscope.service.create_app.__name__)
except Exception as e:
    print("SERVICE IMPORT FAILED:", type(e).__name__, e)
    raise SystemExit(1)

# And the negative, which is the reason the extras were named by hand: the
# model stack must not be here.  A vendored torch would sit next to a live
# inference server's CUDA, and its absence is a property worth asserting rather
# than assuming.
heavy = []
for name in ("torch", "torchvision", "open_clip_torch", "diffusers",
             "opencv-python", "lpips", "omegaconf", "iopath"):
    try:
        md.version(name)
        heavy.append(name)
    except md.PackageNotFoundError:
        pass
print("model-stack packages present:", heavy or "none (as intended)")
if heavy:
    print("REFUSING: the model stack was pulled into the eval venv:", heavy)
    raise SystemExit(1)
PY

echo
echo "=== the service entry point the node will run ==="
ls -l "$VENV/bin/evalscope" 2>&1 || echo "(no evalscope console script)"
"$VENV/bin/evalscope" --help 2>&1 | head -12

echo
echo "=== freeze the resolved set (this is the lock file) ==="
"$VENV/bin/python" -m pip freeze > "$BASE/requirements.lock" 2>/dev/null \
  || "$UV" pip freeze --python "$VENV/bin/python" > "$BASE/requirements.lock"
wc -l "$BASE/requirements.lock"
printf 'evalscope==%s\n' "$VERSION" > "$BASE/VERSION"

echo
printf 'venv size: '; du -sh "$VENV" 2>/dev/null | head -1
df -BG --output=avail "$BASE" | tail -1
