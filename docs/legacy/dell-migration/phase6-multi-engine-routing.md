# Phase 6 — multi-engine routing and the observability contract

Phase 5 gave the lab **one** upstream behind **one** per-user key. This phase
adds what the Atelier V5 brief asks for next: several registered engines, each
named by a model alias, each with an owner, and an ACL that decides who else may
call it — **without copying a weight or starting a second resident model.**

Two sentences are the whole design, and everything below is either how they are
implemented or what they deliberately do not claim:

> The caller picks a **name**. The name picks an **engine**. Whether *this*
> caller may use that engine is decided by a record on the **server**, matched
> against the key that was presented — never by anything the caller says.

This is the design and operations record. The offline acceptance is
`legacy/gateway/selftest_routing.py`; run it with

```
python -m deploy.dell.gateway.selftest_routing
```

## 1. What was asked, and where it is answered

| Brief | Where it is answered |
|---|---|
| Optional, admin-approved routing snapshot; default behaviour unchanged | §3, §4; `routing.Router`, `--routing-snapshot` |
| Multiple registered engines, routed by model alias / endpoint id | §3, §5; `routing.Endpoint`, `routing.Engine` |
| Requester identity from a **real, server-verified** `key_id → principal` binding | §6; `keystore.PRINCIPAL_RE`, `--owner`, `key bind` |
| ACL: `private`, explicit user / trusted uid, all-platform-users | §7 |
| Revoking a share stops new requests, no restart | §5 |
| Sharing ≠ copying weights ≠ starting a new model | §8 |
| Engine upstream limited to allowlisted localhost / Unix socket / trusted internal address; no SSRF; no browser-chosen URL | §9 |
| Per-engine upstream secret, never the caller's, never Strata's master key leaked | §9 |
| Streaming SSE, body limits, queue, 429 / cancellation, metadata logs preserved | §10, §11 |
| Routing logs carry server-side `request_id`/`engine_id`/`endpoint_id`/`owner_uid`/`caller_principal` | §10 |
| Health and failures told apart **per engine**, with real source/time/quality | §10 |
| Node/global GPU may be reported; **no invented per-user GPU share** | §12 |
| Consumption metered by Gateway key | §10 |
| Registry → Gateway snapshot schema, test and an example synthetic config | §3; `routing-snapshot.example.json`; §13 |
| No Portal integration, no production change in this phase | §14 |

## 2. Where this lives, and what it does not touch

```
legacy/gateway/routing.py             the schema, the trust check, the ACL  (new)
legacy/gateway/routing-snapshot.example.json   the worked example           (new)
legacy/gateway/selftest_routing.py    the offline acceptance               (new)
legacy/gateway/gateway.py             + routing hook, + correlation id, + log/status fields
legacy/gateway/keystore.py            + the `owner` binding and its validator
legacy/gateway/admin.py               + `--owner` on create, + `key bind`
legacy/gateway/telemetry.py           + per-key token totals
```

It does **not** touch `/srv`, `/opt`, PAM, sudoers, SSH, keys, the Caddy
configuration, the Portal repository, or the running deployment. No process was
restarted and no request was made to the live Strata while this was written.

## 3. The snapshot: one file, produced elsewhere, read here

The portal's registry writes it; the gateway reads it and decides with it. The
schema string is `llmlab.endpoint-routing/1`. The full worked example is
`legacy/gateway/routing-snapshot.example.json`, and the offline suite
asserts that **that file loads** — so the documented shape cannot drift away
from the shape the code accepts.

```json
{
  "schema": "llmlab.endpoint-routing/1",
  "revision": 7,
  "generated_at": "2026-10-15T09:30:00Z",
  "source": "llm-lab-portal registry",
  "engines": [
    {"engine_id": "strata-primary",
     "upstream": {"transport": "http", "host": "localhost", "port": 8080},
     "secret_ref": {"file": "/srv/.../strata-key.env", "var": "STRATA_API_KEY"}},
    {"engine_id": "engine-alice",
     "upstream": {"transport": "unix", "path": "/run/llmlab/{{LAB_USER}}.sock"},
     "secret_ref": {"file": "/srv/.../engines.env", "var": "ENGINE_ALICE_KEY"}}
  ],
  "endpoints": [
    {"endpoint_id": "ep-alice-qwen", "engine_id": "engine-alice",
     "model_alias": "alice-qwen", "model_id": "Qwen/Qwen2.5-7B-Instruct",
     "owner_uid": "{{LAB_USER}}", "acl": {"kind": "private"}}
  ]
}
```

`model_alias` is what a caller sends; `model_id` is what that engine calls the
same thing. The rewrite happens here, so a caller never learns the engine's
internal name and the engine never receives a name it does not know.

**Turning it on** is one flag on the launch line:

```
python3 -m deploy.dell.gateway.gateway --routing-snapshot /etc/llmlab/routing.json \
        [--allow-upstream-address {{UPSTREAM_IP}}] ...
```

That launch line lives in `deploy/templates/node/53-gateway.sh`, which this phase
deliberately does **not** modify: changing what the running deployment executes
is an operator's action, taken with this document open, not a side effect of a
code change. Until someone adds the flag, the deployment behaves exactly as it
did in phase 5.

The snapshot is **read-only and trusted**, which is one of the two trust models
the brief allows. The other — a detached administrator signature — is *not*
implemented, and this file says so rather than implying otherwise. What replaces
it:

* the file must be a regular file, owned by root or by the gateway's own uid,
  and not writable by group or other;
* the sha256 of what was actually loaded, its revision and its `generated_at`
  are published in `status.json` under `routing`, so an operator can tell
  whether what is in force is what they approved;
* a snapshot that fails any of that is **not partially applied** — it is not
  applied at all, and `routing.error` says why.

The hash is published as **`routing.snapshot_sha256`**, not as `digest`. That is
not a style choice: `72-accept-gateway.sh` asserts that the console's view
contains no `"digest"` key *at all*, because a list of the digests that keys are
checked against would turn that view into a lookup table. The snapshot's hash is
a different thing, but it lands in the same document — so the field was renamed
rather than the existing check loosened, and `selftest_routing.py` now pins the
invariant on this side too.

## 4. The default route is unchanged, and that is testable

The single upstream, the `--alias` map, the queue, the body limit, the SSE relay
and the legacy log fields are all exactly what they were. With no
`--routing-snapshot`, `status.json` carries `"routing": null` — one word that
says *no routing is configured*, which is not the same as *a snapshot failed to
load*, and the two read differently on purpose.

Two consequences worth stating plainly:

* A model name the snapshot does not publish is **not** an endpoint. That
  request continues to the legacy upstream exactly as before. A snapshot adds
  routes; it does not take the old one away.
* A snapshot that cannot be trusted leaves a gateway that works on the single
  upstream. That is the safe failure: the alternative — refusing every request
  because a registry export was malformed — turns a configuration mistake into
  an outage.

**What the legacy path gains, exactly.** No route changes, no body changes, no
status changes, and no existing response header changes. Precisely three
additions, all of them things the brief requires and none of them readable as a
behaviour change:

* `usage.jsonl` lines gain `request_id`, `caller_principal`, `owner_uid`,
  `engine_id` and `endpoint_id` — `null` for the last three on this route,
  because nothing was routed. A log whose lines have different shapes is a log
  every reader has to special-case, so the new columns are on every line.
* `status.json` gains `routing` (`null` without a snapshot) and
  `resource_status` (§12), and `totals` gains `tokens_by_key`.
* `X-Request-Id` / `X-Correlation-Id` are stripped from the incoming request,
  replaced by an id this process mints, and returned as `X-Request-Id`. The
  caller's value is ignored and never reaches the upstream. This is the one
  change to a response, it is required for the id to mean anything, and it
  applies to every route including this one.

It is written down here because a change that is not written down is a change
that is found by surprise.

## 5. Sharing, un-sharing, revoking — live, without a restart

The router re-reads the snapshot when the file's `stat` moves, on the request
that follows. That is one `stat` per request, the same price the key store
already pays, and it is what makes the following true **with no restart and no
signal**:

| Change in the snapshot | Effect on the next request |
|---|---|
| `acl` gains a user | that user can call it, and sees it in `/v1/models` |
| `acl` loses a user | that user is refused again |
| `state: "suspended"` | refused for everyone, owner included |
| `state: "revoked"` | refused for everyone; the record keeps saying so |

Sharing is a row in a file that the owner (or an administrator) edits. Nothing
is copied, nothing is started, and the engine keeps running as it was.

## 6. Identity: from the key record, and from nowhere else

A request may carry three candidate answers to "who is this?" and only one of
them is an answer:

| Candidate | Why it is not identity |
|---|---|
| the `model` field | it is a *name to look up*, not a claim of ownership |
| `X-User`, `X-Forwarded-User`, `X-Remote-User`, … | the caller's own assertion, with more punctuation. Destroyed on the way in |
| the key's `label` | free text an operator typed for humans to read |

So a key may carry **`owner`**: a platform user name (`{{LAB_USER}}`) or a literal
`uid:1002`, written by hand on the server when the key is issued:

```
python3 -m deploy.dell.gateway.admin key create {{LAB_USER}}-laptop --owner {{LAB_USER}}
python3 -m deploy.dell.gateway.admin key bind k_1a2b3c4d uid:1002
python3 -m deploy.dell.gateway.admin key bind k_1a2b3c4d --clear
```

The gateway reads `owner` out of the record it already authenticated by digest.
It is compared **exactly** against the endpoint's `owner_uid` and its
`acl.principals`: a key bound to `bob` does not match an endpoint shared with
`uid:2002`, even when both name the same account. Resolving the two into each
other would need the platform's user database, which would make this a second
place where identity is decided — and the offline suite pins that behaviour with
a check that would fail if someone "helpfully" added the lookup.

**A key without an owner is not broken and is not refused.** It keeps working on
the single upstream, exactly as it did before this field existed. It simply
cannot reach a registered endpoint, because the only way to say yes to it would
be to believe the caller. There is no default owner: a field that defaults to
something is a field that is right by accident.

## 7. The three ACLs, the refusals, and what the caller is told

| `acl.kind` | Who may call it |
|---|---|
| `private` (also the default if `acl` is absent) | the owner, and nobody else |
| `users` | the owner, plus each name in `acl.principals` |
| `all` | every key that carries *any* owner binding — the "all platform users" case |

**A refusal the caller can act on**: a request refused **before** the
concurrency gate, so it does not spend a queue slot and does not make anyone
wait to be told no. The suite checks both halves of that — a routed request
still `429`s behind a busy slot, while a refused one is answered at once.

**A refusal that tells the caller nothing.** "This model exists and is not for
you" is a different answer from "there is no such name", and a caller who can
tell those two apart can find every private alias in the lab by guessing at it,
one refusal at a time — an ACL whose subject list is enumerable is not much of
an ACL. So an endpoint the caller may not use answers **exactly what a name that
does not exist answers**: `404 model_not_found`. The precise reason is recorded
for the operator instead:

| `reason` in `usage.jsonl` and in the router's counters | What happened | What the caller sees |
|---|---|---|
| `not_shared_with_you` | bound, and not on this endpoint's list | `404 model_not_found` |
| `endpoint_revoked` | the share was withdrawn | `404 model_not_found` |
| `endpoint_suspended` | the endpoint is suspended | `404 model_not_found` |
| `principal_unbound` | the key is valid and carries no owner binding | `403`, with the binding named |
| `engine_unusable` | entitled, but the engine has no usable credential | `503`, naming the engine's problem |

The last two are deliberately *not* folded into the `404`, and the reason is the
same reason they are safe: `principal_unbound` is a statement about the
**key**, identical for every name that caller tries, and its whole value is that
an operator can read it and go and fix the binding — which is what the brief
asks for when a legacy key has no reliable binding. `engine_unusable` is
reachable only by a caller who *is* entitled (the entitlement check comes
first), so it tells them the one thing they need and nobody else anything. §14
records the residual oracle this leaves.

The other enumeration boundary is `/v1/models`, which lists only what *this*
caller may call. Both are tested, and the second one is tested with a body on
the request — see §9.

## 8. Sharing is routing; it is not copying

This is the requirement the whole phase exists for, so it is worth being literal
about it. "Share my model with carol" here means **one line in a snapshot**.
It does not mean:

* copying weights to a second directory (two copies to keep in sync, on a card
  that is already full);
* loading a second resident model (the A6000 holds one, and Strata runs
  `parallel 1`);
* giving anyone else the engine's credential — the gateway holds it and attaches
  it on the way out, exactly as it has always done with Strata's key.

## 9. The upstream is not the caller's to choose

A routing snapshot is written by a registry and read by a process that can open
sockets. That makes it an SSRF surface, and it is closed by construction rather
than by remembering to check:

* **Two transports exist**: `http` on an IP literal, and `unix` on an absolute
  socket path. Anything else — `file://`, `ftp://`, a typo — fails to parse.
  There is no code path below the validator that could fetch it.
* **No DNS, ever.** An engine address must be an IP **literal**. A hostname
  would be resolved at connect time, which is precisely the shape DNS rebinding
  needs: the name checks out when the snapshot is validated and points somewhere
  else when the socket is opened. A name is refused with that reason in it.
* **Loopback by default.** A non-loopback address needs
  `--allow-upstream-address {{UPSTREAM_IP}}` on the command line, i.e. an operator
  decision made server-side. **The snapshot cannot widen this** — it can only
  choose among addresses the operator already allowed. A registry that could add
  an address could exfiltrate the lab's traffic.
* **A redirect off an engine is an error**, not something to follow and not
  something to relay: an engine answering **any** `3xx` produces `502
  upstream_redirect_refused`, and its `Location` never reaches the caller. The
  whole class rather than the five codes that carry a `Location` in practice,
  because a list that has to be extended every time somebody remembers a code is
  a list that will be. The legacy upstream is untouched here — that path answers
  exactly what it answered before.
* **Only one route is ever routed.** A request reaches an engine only as
  `POST /v1/chat/completions`; `GET /v1/models` is always answered by this
  gateway, from the legacy upstream's list plus the aliases *this* caller may
  use. Proxying the list would hand the caller that engine's own model ids —
  the internal names behind other people's endpoints — which is enumeration
  through the back door. This is a rule and not a consequence of `GET` being
  bodyless: a `GET` may carry a `Content-Length` and a body, so a caller could
  and did reach an engine's list by naming a model in one. The offline suite
  sends exactly that request.
* **Per-engine secrets.** Each engine has its own `secret_ref`, so revoking one
  engine's access is one file, and "share my model" never means "hand over my
  engine's credential". Secrets are read once at load, never written to the
  status document (which reports `secret_present`), never logged and never sent
  to a caller. An engine whose credential cannot be read is reported
  **unusable** rather than probed and reported "down" — because the difference
  between "the engine is broken" and "this gateway has no key for it" is the
  whole of what an operator needs at that moment.
* **The browser never names a URL.** A caller chooses a *model alias*. The alias
  → engine mapping comes from the approved snapshot; there is no request field,
  header or query parameter anywhere in this gateway that carries an upstream
  address or port.

## 10. What a routing decision records

`usage.jsonl` keeps its shape — one JSON line per request, no prompt, no
completion — and gains five fields. Every one of them is written by the server,
after the fact, from values the caller cannot influence:

| Field | Meaning |
|---|---|
| `request_id` | this process's id for this request, also returned as `X-Request-Id` |
| `caller_principal` | the binding on the authenticated key, or `null` |
| `owner_uid` | the endpoint's registered owner, or `null` |
| `engine_id` | the engine that served it, or `null` **on the legacy route** |
| `endpoint_id` | the endpoint it resolved to, or `null` on the legacy route |

`null` there is a reading, not a gap: it says *nothing was routed, this went to
the single upstream*. The 429 and refusal lines carry the same columns as a
forwarded one, because a log whose lines have different shapes is a log every
reader has to special-case.

`request_id` is minted once per **request**, in `handle_one_request` rather than
in the dispatcher: an id minted in the dispatcher would be missing from the
responses `BaseHTTPRequestHandler` writes itself (a bad request line, an
unsupported method), and on a kept-alive connection those would then carry the
*previous* request's id — an id pointing at somebody else's line being worse
than no id at all.

**Health is per engine.** The background ticker probes each engine's
`/v1/models` with *that engine's own credential* — cheap, no GPU, no model load
— and records `healthy`, `status`, `checked_at` and `error` per engine.
"Averaged health" cannot be acted on by the person whose engine is down. An
engine answering something that is not HTTP at all is recorded against *that
engine*, and the probe loop is wrapped so that no engine can end the thread
doing the probing: that thread is also the only thing that persists the per-key
counters and rewrites `status.json`, so a probe that could kill it would look
like a console frozen at the moment things went wrong.

**Consumption is per key.** Requests and outcomes are already the key store's
per-key counters. Token totals now accumulate per key from the `usage` blocks
the upstream actually sent, with a `reports` count beside them so that an absent
figure cannot be read as a measured zero. Nothing is estimated: a missing or
malformed `usage` block contributes nothing rather than a guess.

## 11. Limits

Unchanged, and deliberately **not** per engine. One RTX A6000 with Strata at
`parallel 1` is the capacity of the box; a second engine is another way to reach
the same hardware, not a second hardware. So a request to engine B still queues
behind a request running on engine A, and the offline suite asserts exactly
that, because "we added an engine and got two slots" is the mistake that would
look like a feature until two people used it at once.

## 12. What is not measured, and why it is written down

`status.json` gains a `resource_status` block that says, structurally, that the
gateway measures **nothing about the GPU**:

```json
"resource_status": {
  "measured_by": "llmlab-gateway",
  "gpu": {"measured": false, "source": null, "reason": "the gateway holds no NVML binding; ..."},
  "per_user_gpu_share": {"measured": false, "reason": "the card is shared and the engine API does not report per-request VRAM, ..."}
}
```

The brief allows node-level GPU facts and forbids inventing a **per-user share
of a shared model**. Both halves resolve to the same answer here: this process
has no NVML binding, the engine API does not report what a request cost in VRAM,
and one card serves everyone. So the honest content of the block is a statement
of what is absent — written as a structured `measured: false` rather than as a
zero, because `0 MiB` on a console is read as a measurement.

## 13. The offline acceptance

`python -m deploy.dell.gateway.selftest_routing` — 107 checks, all offline: two
mock engines, two mock owners, the legacy upstream mock from the phase-5 suite,
and nothing but `localhost` on ports the test bound itself. Its shape is the
argument: **every positive check has a negative control beside it.**

* two engines, two owners, all three ACL kinds, and the same request refused for
  the wrong caller;
* each engine's key never seen by another engine, by the legacy upstream, or by
  a caller — and the caller's key never seen by any engine;
* a private model absent from a stranger's `/v1/models` and present in its
  owner's — including with a `GET` carrying a model name in its body, which is
  how an engine's own model ids could otherwise be enumerated;
* a refusal for a model that is not yours answered exactly as an unknown name
  is, with the real reason asserted in `usage.jsonl`;
* a share granted and withdrawn, an endpoint suspended and revoked, each taking
  effect on the very next request;
* spoofed `X-User`/`X-Forwarded-User`/`X-Remote-User`/body-claimed identity
  changing nothing, with a control showing the real owner still works with the
  same headers attached;
* a key bound by the admin CLI at run time being honoured on the next request,
  and a bare `key bind` with no owner refused rather than read as "clear";
* an engine answering 302 producing `502` with no `Location` relayed, and the
  same engine answering normally once the redirect is removed;
* a streamed answer relayed as SSE with its terminator, a slow engine producing
  `504` at the budget, an engine's own 4xx passed through in its own words;
* an engine answering something that is not HTTP recorded against that engine
  while the ticker — which also persists the counters — keeps running;
* the same request id appearing on two requests over one kept-alive connection
  being *different*, so the id is per request and not per connection;
* `main()` itself starting up with a snapshot configured and saying so, which is
  a different path from "a `Router` can be constructed" and the one where a
  rename can take the whole gateway down;
* group-writable snapshots, wrong schema strings, non-allowlisted addresses,
  hostnames, `file://` upstreams, inline secrets and unknown protocols each
  refused — with the legacy route still answering afterwards;
* a redirected/shared/revoked model neither routable nor enumerable.

The example snapshot is loaded by the suite too, so the shipped documentation
cannot describe a file the code rejects.

## 14. What this record does not claim

* **No signature verification.** The trust model is "read-only and trusted", not
  "signed". If the registry's writer is compromised, `snapshot_sha256` in `status.json`
  shows *what* changed, not *who* approved it.
* **No name ↔ uid resolution.** `bob` and `uid:2002` are separate principals and
  must both be listed if both are meant. Documented in §6; deliberate.
* **A bounded existence oracle remains, and it is the deliberate half of §7.** A
  key with *no* owner binding gets `403` for a registered name and the upstream's
  own answer for an unknown one, so a holder of such a key can tell "some
  endpoint is called that" from "no endpoint is". Unbound keys are an operator
  misconfiguration, not user credentials, the answer contains no owner and no
  model id, and removing the oracle would mean answering a misconfigured
  internal system with a silent `404` and leaving nobody able to see why. The
  bounded part is worth stating plainly: this is an oracle about *names*, not
  about who owns them or what they serve. `GET /v1/models` — the surface the
  brief actually names — is closed for every kind of key.
* **An endpoint on an engine whose credential is missing is not listed** in
  `/v1/models` but is still reachable as a `503`. Callers who are not entitled
  never see either answer.
* **The unix-socket transport is parsed, validated and used by the example, but
  no request has been forwarded over one on this box yet** — the engines here
  listen on TCP. It is the first thing to exercise when a socket one appears.
* **Everything here has been exercised against mocks, offline.** No live engine
  other than the single legacy upstream exists yet; nothing in this phase has
  been run against a second real engine, and this file does not pretend
  otherwise.
* **`/v1/models` answers from the legacy upstream and is extended** with the
  caller's visible aliases. It is not yet a pure registry view: a model that
  only exists on a registered engine is listed, but the list is still anchored
  on whatever the legacy upstream reports.
* **No per-engine or per-endpoint rate limiting.** The gate is the box's, in
  §11. Per-endpoint quotas would need a decision about what a "request against
  an endpoint" costs, which nobody has made.
* **No Portal, Registry or UI work.** This phase is the Gateway half of the
  data contract and nothing else; the other three lines of work (V5 UI, Portal
  Registry + `llmlab` CLI, PAM broker) are separate, and this file is the
  contract they can rely on.
* **The direct path to `:8080` is still open**, exactly as recorded in
  `phase5-api-gateway.md` §2. Nothing here closes it, and nothing here claims
  to.
* **A residual cross-process write race remains** in the key store, documented
  in `keystore.py` and observable as a rare failure of the phase-5 "re-enabling
  brings the same key back" check (1 run in ~5 observed while writing this
  phase, on the unmodified base commit as well). It is not introduced here and
  is not fixed here.
