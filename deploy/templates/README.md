# Deployment templates (legacy `llm-lab`)

These files are the sanitized deployment templates and operations scripts from the
retired private `llm-lab` repository. They are **reference material**, not a
runtime entry point. Nothing here is executed by the new portal automatically.

## Sanitization

Every deployment-specific value is a placeholder:

| Placeholder | Meaning |
|---|---|
| `{{LAB_HOST}}` | address or DNS name of the inference node |
| `{{LAB_HOSTNAME}}` | node hostname |
| `{{LAB_NODE}}` / `{{LAB_NODE_ID}}` | human label / short identifier of the node |
| `{{LAB_USER}}` | unprivileged account that owns the checkout |
| `{{LAB_ADMIN_USER}}` | account that owns the service secrets |
| `{{LAB_GROUP}}` | shared group name |
| `{{LAB_ROOT}}` | deployment root (services, logs, state) |
| `{{LAB_CHECKOUT}}` | path of the git checkout |
| `{{LAB_HOME}}` | account home directory |
| `{{LAB_SSH_KEY_PATH}}` / `{{LAB_KNOWN_HOSTS_PATH}}` / `{{LAB_SSH_FINGERPRINT_PATH}}` | SSH identity files, **not** included in this repository |
| `{{STRATA_ROOT}}` / `{{STRATA_DATA_ROOT}}` | inference engine installation / model data roots |
| `{{LAB_HTTPS_PORT}}` | public TLS port of the reverse proxy |

Loopback addresses are written as `localhost`.

## Layout

| Path | Origin |
|---|---|
| `caddy/Caddyfile.tmpl` | `deploy/dell/caddy/Caddyfile.tmpl` |
| `node/*.sh` | `deploy/dell/node/*.sh` |
| `adapter/adapter.py` | `deploy/dell/adapter/adapter.py` |
| `labctl.py` | `deploy/dell/labctl.py` |
| `remote.sh` | `deploy/dell/remote.sh` |
| `fetch-caddy-root.sh` | `deploy/dell/fetch-caddy-root.sh` |

The scripts still describe the legacy `deploy/dell/...` workflow in some comments.
Path references were updated to the new `deploy/templates/...` layout, but the
templates are intentionally not wired into `labctl` or any service unit.

## Not included

* `config/ssh/*` — host fingerprints and public keys are host identity material.
* Any real secret, token, password, private key, address or user name.
* The legacy request-body logging experiment (PR #17).
