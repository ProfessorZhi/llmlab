#!/usr/bin/env bash
#
# Release gate verdict — the single place that decides whether a run may be
# treated as release-ready.
#
# Why this is a script and not inline YAML shell: the previous revision decided
# inline, and a browser job that never ran reported its result as `skipped`,
# which matched none of the branches — so the job printed "All runnable gates
# succeeded" and exited 0. The result was a green check named "Gate verdict"
# with zero browser E2E behind it. Extracting the decision makes it executable
# and directly testable (tests/release/test_release_gate_verdict.py).
#
# Two modes, deliberately different:
#
#   --mode cpu       CPU-only unit checks. Reports the CPU gates and states that
#                    browser evidence was NOT evaluated. Browser state cannot
#                    change the exit code. Informative.
#
#   --mode release   Release acceptance. FAIL-CLOSED. Requires:
#                      * every CPU gate succeeded, AND
#                      * browser-available == "true", AND
#                      * browser-result == "success".
#                    A missing browser, a browser job that was skipped, and a
#                    detector that errored all FAIL this gate. "No browser on
#                    this runner" is not evidence of browser acceptance.
#
# Usage:
#   release-gate-verdict.sh --mode release \
#     --design success --python success --node success \
#     --browser-available true --browser-result success
#
# Exit codes: 0 = gate passes, 1 = gate fails, 64 = usage error.
#
# A markdown summary is written to stdout and, when GITHUB_STEP_SUMMARY is set,
# appended there as well.
set -euo pipefail

MODE=""
DESIGN=""
PYTHON=""
NODE=""
BROWSER_AVAILABLE=""
BROWSER_RESULT=""

die() { echo "usage error: $*" >&2; exit 64; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2-}"; shift 2 ;;
    --design) DESIGN="${2-}"; shift 2 ;;
    --python) PYTHON="${2-}"; shift 2 ;;
    --node) NODE="${2-}"; shift 2 ;;
    --browser-available) BROWSER_AVAILABLE="${2-}"; shift 2 ;;
    --browser-result) BROWSER_RESULT="${2-}"; shift 2 ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$MODE" in
  cpu|release) ;;
  "") die "--mode is required (cpu|release)" ;;
  *) die "unknown --mode '$MODE' (expected cpu|release)" ;;
esac

# Required gates. A gate that never ran reports "skipped", which is not success
# and must never be treated as one.
FAILURES=""
note_failure() {
  FAILURES="${FAILURES}${FAILURES:+, }$1"
  echo "::error::$1" >&2
}

for pair in "V5 frozen design + V6 derivation:$DESIGN" "Python regression:$PYTHON" "Node regression:$NODE"; do
  label="${pair%%:*}"
  result="${pair#*:}"
  if [ "$result" != "success" ]; then
    note_failure "$label did not succeed (result: '${result:-<empty>}')"
  fi
done

BROWSER_LINE=""
RUNS_ONLY=""
if [ "$MODE" = "release" ]; then
  if [ "$BROWSER_AVAILABLE" != "true" ]; then
    note_failure "no browser was detected on the runner (browser-available: '${BROWSER_AVAILABLE:-<empty>}'); release acceptance requires browser evidence"
    BROWSER_LINE="FAIL — no browser detected; browser E2E cannot have run"
    RUNS_ONLY="
**This gate requires browser E2E. A skipped browser job is not a pass, and a
detector that never ran is not a pass either.** Re-run on a runner with
Chrome/Chromium, or run \`npm run test:browser\` plus
\`node --test tests/auth-ui/browser.auth-ui.test.mjs\` on a host that has one,
before treating Atelier V6 as release-ready."
  elif [ "$BROWSER_RESULT" != "success" ]; then
    note_failure "browser acceptance did not succeed (result: '${BROWSER_RESULT:-<empty>}')"
    BROWSER_LINE="FAIL — browser E2E did not succeed ($BROWSER_RESULT)"
    RUNS_ONLY="
**This gate requires browser E2E to succeed. It did not.**"
  else
    BROWSER_LINE="PASS — browser E2E ran and succeeded"
  fi
else
  if [ "$BROWSER_AVAILABLE" != "true" ]; then
    BROWSER_LINE="SKIPPED — no browser on this runner (not a pass)"
  elif [ "$BROWSER_RESULT" != "success" ]; then
    BROWSER_LINE="NOT SUCCESSFUL — browser result was '$BROWSER_RESULT' (not a pass)"
  else
    BROWSER_LINE="PASS — browser E2E ran and succeeded"
  fi
fi

if [ -n "$FAILURES" ]; then
  VERDICT="FAIL"
  HEADLINE="**Verdict: FAIL.**"
else
  VERDICT="PASS"
  HEADLINE="**Verdict: PASS.**"
fi

SUMMARY="## Atelier V6 release gates — $( [ "$MODE" = "release" ] && echo "release acceptance" || echo "CPU-only unit checks" )

| gate | result |
| --- | --- |
| V5 frozen design + V6 derivation | ${DESIGN:-<empty>} |
| Python regression | ${PYTHON:-<empty>} |
| Node regression | ${NODE:-<empty>} |
| Browser acceptance | ${BROWSER_LINE} |

${HEADLINE}
${RUNS_ONLY}
CPU-only, read-only, no environment secrets were referenced."

printf '%s\n' "$SUMMARY"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '%s\n' "$SUMMARY" >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$VERDICT" = "PASS" ]; then
  echo "gate passed (mode=$MODE)"
  exit 0
fi
echo "gate failed (mode=$MODE): $FAILURES" >&2
exit 1
