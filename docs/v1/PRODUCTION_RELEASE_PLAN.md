# Production release plan (source-only preparation)

**Status: NOT READY. No deployment is authorised by this document.** It records
the gates a production release must clear, the exact host steps that would
follow an approval, and the rollback. Everything here is a plan; the repository
contains templates and a validator, not a running service.

## 1. Gates

A release manifest carries five booleans. `tools.release.contract` reads them
and returns `NOT_READY` unless all five are true; there is no default-true path.

| Gate | Meaning | Evidence required | Owner |
|---|---|---|---|
| `platform_auth_verified` | Platform accounts authenticate independently of Linux | the auth core merged, its negative tests green, a real login exercised once on the host without recording the secret | Agent A + operator |
| `inference_upstream_verified` | A real model upstream answers through the Gateway | Strata listener present on its documented port, an authenticated bounded call, latency/error recorded | upstream owner |
| `same_origin_verified` | UI and API answer from one origin with the release mount plan | `/index.html`, `/api/v2/ops`, `/auth/platform/*` served on the release port; Linux auth routes 404 | Agent C |
| `host_deployment_approved` | An explicit, dated deployment window and operator | written approval naming the commit and the port | operator |
| `rollback_drill_verified` | A restore of the previous release and the auth database was actually rehearsed | drill log with commands, duration and result | operator |

`READY` in the validator means "the gates are recorded". It is **not** a claim
that the host is serving, and it never substitutes for the deployment approval.

## 2. Hard prerequisites (currently unmet)

1. **Platform authentication is not wired into any server.** The core exists on
   a feature branch; the BFF integration is a separate change.
2. **The same-origin staging composition still mounts Linux auth**
   (`/auth/linux/*` via `build_linux_service`). It is fail-closed but it is not
   the platform sign-in path, and it is not a release entrypoint.
3. **No inference upstream.** The Gateway is configured against
   `127.0.0.1:8080` and nothing is listening there. Bringing that up is an
   upstream-owner action, not a portal change.
4. **No production TLS entry for llmlab.** The existing tailnet routes are
   `443 -> CloudCLI`, `8443 -> sidecar Caddy`, `8444 -> static preview`. None of
   them may be reused or modified for the release.
5. **No persistent, backed-up state directory** has been approved.
6. **No three-account negative isolation test** has ever run against a real host.

## 3. Deployment sequence (only after approval)

Each step is a separate, reviewable action. Nothing here is executed by the
repository.

1. **Freeze the commit.** Record the 40-hex SHA; the manifest must match it.
2. **Pick a port.** An unprivileged loopback port that is not in the reserved
   set (`443, 3230, 8080, 8200, 8443, 8444, 8765, 9090, 9101, 18443, 18769,
   38849`). Verify with `ss -tln` that it is unused; do not stop anything to
   free one.
3. **Create the state directory** `0700`, owned by the service account. The
   platform-auth database is created `0600` inside it and never leaves it.
4. **Initialise the first platform administrator interactively** at the console
   (`python3 -m backend.platform_auth.cli --state-dir <dir> init-admin`). There
   is no default account and no generated password.
5. **Install the user unit** from `deploy/llmlab/llmlab-release.service.tmpl`
   with `{{ENTRYPOINT}}` filled by the reviewed auth-integration entrypoint.
   Verify `systemctl --user cat` shows the intended port and loopback bind.
6. **Publish on the tailnet only**: a *new* `tailscale serve --https=<port>`
   route to `127.0.0.1:<port>`. Funnel stays off. The 443/8443/8444 routes are
   not touched.
7. **Canary**: one operator account, read-only scope, for one agreed window.
   Watch 401/403 rates, readiness (`/readyz` must be READY), latency, and audit
   rows. No write scopes during the canary.
8. **Three-account negative isolation**: three platform accounts, each with a
   distinct scope, verifying that A cannot read B's objects, that a disabled
   account's session dies immediately, and that a forged `X-Platform-User`
   header changes nothing. Results are recorded; no passwords in the record.
9. **Record the deployment**: commit, manifest, port, unit hash, TLS route,
   canary window, rollback trigger.

## 4. Backup and rollback

**Backup (before and after canary):**

```
systemctl --user stop llmlab-release
sqlite3 <state_dir>/platform_auth.sqlite3 ".backup '<backup_dir>/platform_auth-<sha>.sqlite3'"
chmod 600 <backup_dir>/platform_auth-*.sqlite3
systemctl --user start llmlab-release
```

`<backup_dir>` is `0700`; backups are `0600`; they are never committed.

**Rollback triggers**: readiness not READY for 5 minutes, any 5xx above 1% of
requests, any cross-account read observed, or the canary operator reporting a
blocked workflow.

**Rollback procedure:**

1. `systemctl --user stop llmlab-release`
2. restore the previous manifest and the previous unit entrypoint;
3. restore the database from the pre-canary backup if the schema or accounts
   changed (they should not have during a read-only canary);
4. `systemctl --user start llmlab-release`; confirm `/readyz`;
5. remove the tailnet route for the release port;
6. record the rollback with timestamps and the evidence that triggered it.

## 5. Explicitly out of scope

* no change to 443/8443/8444, Caddy, Gateway, Strata, PAM, SSH, systemd system
  units, GPU or model processes;
* no Funnel, no public listener, no `0.0.0.0`;
* no QA fixture or demo data in a release origin;
* no secrets in git, argv, environment files, logs or audit rows.
