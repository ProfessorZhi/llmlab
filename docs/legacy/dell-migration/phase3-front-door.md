# Phase 3 — the front door (§1, §2)

What §1 and §2 of the brief asked for, what was built, and what was measured
rather than assumed. Every claim below is backed by a check that runs on the
node (`deploy/templates/node/54-accept-caddy.sh`, 42 checks) or by a reading quoted
with the command that produced it.

Status: **complete**, with two findings that need a decision (§4 below).

---

## 1. What is in front of the portal

| | |
|---|---|
| Portal | `localhost:8765` — unchanged, and that is the boundary, not a leftover |
| Front door | `https://{{LAB_HOST}}:8443` — Caddy 2.11.7, the only process here on a routable address |
| Published | `/` (portal), `/strata/` (Strata console), `/grafana/` (Grafana) |
| Not published | EvalScope — see §4.1 |
| TLS | `tls internal`; the root CA is fetched and installed on the workstation, verification is never disabled |
| Credentials | two sets: `admin` (every route) and `viewer` (read-only). Switching services is admin-only. |
| Working address | `{{LAB_HOST}}:8443` only, via `bind`; TCP only, no HTTP/3, no `:80` |
| Audit | JSON access log, rolled at 10 MiB × 5, credential headers redacted by Caddy |

## 2. The three properties, and how each is checked

**Nothing is anonymous.** A request with no credentials to an API path gets
`401` with `Www-Authenticate: Basic realm="restricted"` and a body that is *not*
the portal's, which matters because a 401 the proxy sends and a 401 the portal
sends are different claims. Every published route sits under a `basic_auth`.

**Switching services is admin-only.** `POST /api/service/stop` with *viewer*
credentials is refused by the proxy (401) and never reaches a handler. With
*admin* credentials it reaches the portal, which is observed as the portal's own
cross-origin 403 — only the portal can emit that. The refusal is then confirmed
as state, not as a status code: `grafana`'s row is read before and after and must
be identical, because a status code cannot show that nothing half-happened.

**The proxy is not switchable from the console.** Caddy is `managed` (the cron
hook must be able to revive the front door) and `portal_managed = false`. Even
admin credentials, with the correct `X-Requested-With`, get 403 from the portal,
specifically so the operator cannot watch the page die and lose the button that
turns it back on. `labctl` retains direct access.

**Client-supplied identity headers are destroyed.** Seven header names are
compiled into request-header deletions, verified against `caddy adapt` — the
machine's reading of the config, not the text of it. *Limitation, stated rather
than glossed:* no route echoes headers, so end-to-end the strip is invisible
today and only the compiled deletion can be shown.

## 3. What running it for real changed

Nine defects were found by executing things rather than reading them. Two were
in the deployment, and seven were in the instruments written to check it — which
is the more useful number, because every one of those seven failed *closed* here
and would have read as clean the other way round.

**In the deployment:**

1. `service_state` dialled `localhost` to decide whether a service is up —
   correct for everything that binds loopback, which was all of them until the
   proxy. It reported `down` on a proxy that was serving traffic. Services may
   now name a `probe_host`.
2. The Caddyfile's site address was read as constraining the socket. It does
   not: `https://{{LAB_HOST}}:8443` produced `*:8443`, answered on all three
   local addresses, while a comment in the file asserted the opposite. `bind` is
   the directive that constrains it.
3. `entry_url` reasoned "the portal binds loopback, therefore the reader is on
   this host". With a proxy in front, every component without a public URL was
   handed a loopback address — EvalScope would have been presented to a remote
   browser as `http://localhost:9000`.

**In the checks** (each recorded because the failure mode is the lesson):
`WWW-Authenticate` looked up case-sensitively in a dict of headers; a TLS check
that called a 401 a chain failure; a regex for `"op":"delete"` when a header
rewrite compiles to `{"handler":"headers","request":{"delete":[...]}}`; a log
check asserting `Authorization` was absent from a log that redacts it; a
redirect assertion that let urllib follow the redirect first; and a payload key
guessed as `services` instead of `items`.

Also corrected: `auto_https` and `protocols` are global/server options, not site
directives — both were written at site level and both were rejected by
`caddy validate`, which is the entire argument for rendering and validating on
the node instead of shipping a file that looks right.

## 4. Findings needing a decision

### 4.1 EvalScope cannot be published on this port

Measured: its `index.html` requests `/assets/index-*.js` and `/favicon.svg` with
a leading slash, baked in at build time, with no configuration hook exposed.
Under a path prefix those requests leave the prefix and land in the portal's
catch-all. The options are a response-body rewrite in the proxy (fragile, and
§2's spirit is against it) or a second listening port — which is outside this
work's authorisation. It is left unpublished *on purpose*, and the console
reports it as "not exposed" rather than offering a link that 404s.

Strata was originally believed to have the same problem. It does not — measured:
its page and every `fetch()` in `app.js` are relative, with no WebSocket and no
use of `location.origin`. It is published at `/strata/`.

### 4.2 The portal was wrong about Strata's asset paths, in writing

The belief in 4.1 about Strata was recorded in the migration notes as a reason
not to publish it. It was never measured and it was false, and had it stood,
§8's 模型与聊天 entry would have been left unimplemented for no reason.

### 4.3 Strata's own console is on every interface, unauthenticated

Pre-existing, and §2 explicitly places Strata's listener configuration out of
scope. From the workstation, `http://{{LAB_HOST}}:8080/` answers `200` with no
credentials while `/v1/models` and `/metrics` correctly return 401. This is a
property of the machine, not of this deployment, and it is unchanged by this
work — reported, not touched.

## 5. How to run it

```bash
deploy/templates/remote.sh deploy/templates/node/52-caddy.sh        # install / re-render
deploy/templates/remote.sh -c '/usr/bin/python3 deploy/templates/labctl.py restart caddy'
deploy/templates/remote.sh deploy/templates/node/54-accept-caddy.sh # 42 checks
deploy/templates/fetch-caddy-root.sh                           # workstation: trust anchor
```

The proxy is deliberately *not* started by its install script: starting it is
`labctl start caddy`, the same path every other service takes, which is what
lets the binary be updated without bouncing anybody's console.

## 6. Transitions, and what they are not

Supervision is `@reboot` cron plus a five-minute tick, marked
`# llm-lab-autostart`. **This is not systemd**: there is no crash supervision
beyond the five-minute interval, no socket activation, and no restart policy. A
proxy that dies at second 30 of a five-minute window is down for the rest of it.
Systemd-level management needs privileges this account does not have, and the
brief permits this as a transition provided it is labelled as one — so it is
labelled here and in the service's own documentation.

A service stopped by the operator is recorded as stopped and is *not* revived;
the autostart run reports the skip by name rather than reversing it. Verified by
reading the ticks cron actually wrote, not by reading the crontab.
