#!/usr/bin/env bash
# Copy the benchmark and report archives out of the checkout and into the data
# root, so the evidence stops depending on a git tree.
#
# Why this is not paranoia: `reports/` and `data/benchmarks/` are tracked, which
# is right -- they are small and they are the evidence.  But tracked also means
# a `git reset --hard`, a bad merge or a `checkout` during a rollback takes them
# with it, and a benchmark run is not reproducible from its commit (the model,
# the GPU state and the day are gone).  The data root is listed first in the
# portal's roots precisely so archived material is served from here.
#
# Idempotent and non-destructive: an existing file in the data root is never
# overwritten.  A copy step that can clobber is a copy step that can destroy the
# thing it was meant to protect.
set -euo pipefail

SRC={{LAB_ROOT}}/llm-lab
DST={{LAB_ROOT}}/llm-lab-data

echo "=== staging archives out of the checkout ==="
mkdir -p "$DST/reports" "$DST/benchmarks"

copied=0
skipped=0
for pair in "reports:$SRC/reports:$DST/reports" "benchmarks:$SRC/data/benchmarks:$DST/benchmarks"; do
  kind="${pair%%:*}"; rest="${pair#*:}"; from="${rest%%:*}"; to="${rest#*:}"
  [ -d "$from" ] || { echo "  $kind: no source dir ($from)"; continue; }
  echo "  $kind: $from -> $to"
  while IFS= read -r -d '' f; do
    rel="${f#"$from"/}"
    if [ -e "$to/$rel" ]; then
      skipped=$((skipped + 1))
      continue
    fi
    mkdir -p "$to/$(dirname "$rel")"
    cp -p "$f" "$to/$rel"
    copied=$((copied + 1))
  done < <(find "$from" -type f -print0)
done

echo
echo "=== result ==="
echo "copied  : $copied file(s)"
echo "left as-is (already present): $skipped"
echo
echo "data root contents:"
find "$DST" -maxdepth 2 -mindepth 1 | sort | head -30
echo
echo "sizes:"
du -sh "$DST/reports" "$DST/benchmarks" 2>/dev/null || true

echo
# The claim being made is that the evidence is now readable from a location git
# cannot rewrite, so it is checked rather than asserted.
miss=0
for f in "$SRC/reports"/*.md; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  [ -f "$DST/reports/$base" ] || { echo "MISSING in the data root: reports/$base" >&2; miss=1; }
done
for d in "$SRC/data/benchmarks"/*/; do
  [ -d "$d" ] || continue
  base="$(basename "$d")"
  [ -d "$DST/benchmarks/$base" ] || { echo "MISSING in the data root: benchmarks/$base" >&2; miss=1; }
done
if [ "$miss" -ne 0 ]; then
  echo "VERDICT: FAIL -- the archive is not fully staged" >&2
  exit 1
fi
echo "VERDICT: PASS -- every archived report and run is present in the data root,"
echo "         which a git reset cannot reach."
