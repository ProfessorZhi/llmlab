# llmlab production release templates

**These files are not a runtime entry point.** Nothing here is installed,
enabled, started, or read by any service. They describe the shape of a release
that an operator may deploy **after** the gates in
[`docs/v1/PRODUCTION_RELEASE_PLAN.md`](../../docs/v1/PRODUCTION_RELEASE_PLAN.md)
are recorded and a deployment window is approved.

| File | Purpose |
|---|---|
| `release.example.json` | A manifest the validator accepts. Copy it, replace `git_sha`, `static_root`, `state_dir`, the port and the CSRF origin, then run `python3 -m tools.release.cli validate <file>`. |
| `llmlab-release.service.tmpl` | A **user** unit template with hardening. `{{ENTRYPOINT}}` is a placeholder on purpose: the production entrypoint that mounts `/auth/platform/*` ships with the auth integration change. |

## What the manifest pins

* exact package version and exact 40-hex commit — a release is reproducible from
  one commit, not "latest";
* loopback bind, an unprivileged port that no existing Dell service uses;
* `Secure` + `HttpOnly` cookies, `SameSite=Lax|Strict`, an exact CSRF origin;
* `no-store` static responses;
* no QA fixture and no production/home path in `data_sources`.

## What must not be substituted

The same-origin staging composition (`backend/bff/staging.py`) mounts
`/auth/linux/login` and `/auth/linux/status` through `build_linux_service`. That
is a **PAM-shaped** identity path and is unavailable in this build. It must not
become the platform sign-in route. The release mount plan therefore refuses
`/auth/linux/*` and `/auth/admin/*` outright (404, not 503) — the release does
not have them.

## Fill-in rules

* never commit a filled-in copy of these templates;
* `state_dir` must be created `0700` and owned by the service account; the
  platform-auth database lives there at `0600`;
* if `state_dir` is under the service account's home, `ProtectHome` in the unit
  must be relaxed deliberately and documented in the deployment record;
* the CSRF origin must be the exact tailnet HTTPS origin that will serve the
  portal, not a wildcard.
