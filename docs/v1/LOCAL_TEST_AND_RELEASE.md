# V1 integration tests and safe release runbook (GitHub CI quota exhausted)

## 1. Separate service health from the GitHub CI billing state

**Policy:** GitHub Actions may be red, unavailable or not executed because minutes/credits are exhausted. This is not a reason to stop all development or to fabricate a green check. Mark the GitHub automation result **CI_UNAVAILABLE_QUOTA** when explicitly verified billing-related and attach *independent Linux local test evidence*. A real test/assertion failure remains **TEST_FAILED** and blocks integration. Do not disable GitHub branch protection or change required checks without separate repository-maintainer approval.

The dedicated [Linux Quality Runner #82](https://github.com/ProfessorZhi/llm-lab-portal/issues/82) owns reproducible test receipts, local command/exit capture and redacted artifact digests. Preferred test runners are already-authorized, reachable Linux servers. Dell may run short CPU tests if measured load permits; do not stress live A6000 or launch inference. Windows is excluded by default.

## 2. Reproducible baseline from actual repository

Exact commands should be pinned to the PR commit and checked against the scripts at that SHA. As of the contract branch, root `package.json` defines:

```bash
node --version                              # expect >=20 per package engines
python3 --version                           # project requires >=3.11
npm test                                    # Node browser-native contract/unit tests
npm run test:browser                        # headless browser test runner, if browser installed
npm run verify:extract                      # generated web/prototype consistency
python3 -m unittest tests.test_v6_capability_catalog -v
python3 -m unittest discover -s tests -p 'test_*.py'
```

The specific Python discovery command may need adaptation to the current test naming/package import path — if so report exact command, output and reason; do not claim it passed without execution. In addition, run the repository's documented V5 hash, V6 derive/extract and API route/DTO checks. V5 prototype is immutable; changes belong in original V6 source before generation.

Local test logs must include device/OS/tool versions, branch and full SHA, command, exit code, start/end, PASS/FAIL/SKIP counts and SHA-256 digest of the redacted log. Never treat an assertion skipped for environmental reasons as a passing feature.

## 3. Gate checklist

| Gate | Minimum required proof | Block if |
| --- | --- | --- |
| G0 baseline | frozen GitHub baseline SHA; all Lane branches isolated and no overwrite of active worktree | baseline ambiguity/permissions |
| G1 contract | route maps/versioned DTOs and auth state agreement, exact source tags | missing/mismatched schema |
| G2 build | Node unit suite, Python unit/security suite, generation parity, lint/static where configured | real command/test failure |
| G3 browser | nine views, 320/390/768/1024/1440, both themes, keyboard, login/source states, refresh/privacy | unhandled error, data leak or fabricated default |
| G4 cross-domain | A→share B→invoke→meter→revoke→next-call denied (CPU harness **for code**, live test **for host**) | Owner/ACL/Router mismatch |
| G5 producer | Dell genuine GPU/time/units/source, known Engine loaded readiness, trusted Gateway metadata and report owners | source unknown or stale shown LIVE |
| G6 security | 401/403/404/CSRF, spoofed UID, cross-session cache clearing, token revocation, safe error response | cross-tenant disclosure or bypass |
| G7 release | isolated protected canary, 8443/legacy noninterference, exact SHA, backup and rollback drill | rollback/ingress/owner mismatch |
| G8 approval | operator-signed exact scope for production PAM/Gateway/ingress changes, release GO record | missing explicit authorization |

A worker may complete its source-only G1/G2 even while G5–G8 are pending. The **V1 Production** state cannot bypass G4 live, G5, G6, G7 or G8.

## 4. Integration protocol

Integrator pins an integration branch at reviewed baseline and merges independent completed slices incrementally. On conflict, inspect both owners, update contract and run dependent tests; don't force-push other workers. At each green local snapshot record exact SHA and evidence. Never overwrite current :8443 or old Portal just to display a finished UI.

If no Actions runners can be used, the Quality Agent should execute equivalent commands in an approved Linux worktree and publish redacted results to PR/Issue. GitHub protected merges that require nonexistent check results remain pending unless an authorized maintainer explicitly changes the policy.

## 5. Host read-only canary procedure (not executable authority)

1. Obtain permission to read current Dell ports, service ownership and protected ingress; capture baseline SHA/version/health and backup/rollback options.
2. On approved isolated build path run local frontend/BFF and trusted fixed read-only HostSummary; keep Gateway writes, PAM system install and evaluation job submission disabled.
3. Validate real source, sample_at/observed_at/age and null handling in protected browser, refresh/expired sessions, mobile and nine views.
4. Monitor noninterference to Strata, Gateway, Prometheus, legacy portal and other Linux users' work.
5. Prepare exact operator change order (affected path/service/config, commands and rollback); do not cut over without separate approval.
6. Upon approval, perform an atomic minimal release, verify URL/session/metrics/API, test rollback and record deployed SHA, operator and time. Failed gates → revert only the new portal artifact, never force-reset GPU/model processes.

Do not store credentials, raw personal request contents, full private logs or model file paths in screenshots/PR.
