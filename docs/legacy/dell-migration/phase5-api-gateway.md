# Phase 5 — the API gateway (§3, §4, §7–§9, §11–§13)

An internal system should be able to call the model on the Dell the way it calls
any OpenAI-compatible service: **fill in an API Base URL, an API Key and a model
name, and get an answer.** That is the whole deliverable. Everything below is
either how it was built or what it deliberately does not do.

This is the design and operations record. The acceptance *readings* — what was
measured on the node, on which date, with which script — are in
`phase5-acceptance.md`.

---

## 1. What was asked, and where it is answered

| Brief | Where it is answered |
|---|---|
| §3 phase-1 compatibility: `GET /v1/models`, `POST /v1/chat/completions`, `stream=false` and `true`, standard fields only | §4 below; `ALLOWLIST` in `legacy/gateway/gateway.py` |
| §4 a `/v1/*` route of its own, Bearer only, no Basic stacked, TLS chain trusted | §8; `deploy/templates/caddy/Caddyfile.tmpl` `handle /v1/*` |
| §5 per-system keys, digest only, plaintext shown once, never in the repo | §5 |
| §6 key management by CLI, and a **read-only** console area | §5, §9 |
| §7 one upstream slot, per-key limit, bounded queue, 429 + `Retry-After`, timeouts, cancel on disconnect, honest relay | §6 |
| §8 a standalone stdlib service on `localhost`, allowlisted routes, fixed upstream, Strata key swapped in, nothing leaked downstream | §6, §8 |
| §9 no prompts, no résumé text, no model answers, no PII; token counts real or marked as estimates | §7 |
| §11 acceptance by real client, copy-pasteable example, deliverables documented | §3, §4; `examples/openai_client.py`; `deploy/templates/node/72|73|74|75-*`; `phase5-acceptance.md` |
| §12 read-only console panel | §9 |
| §13 a separate branch and PR, not self-merged | the PR, not this file |

Explicitly **not** built, because the brief cancelled them: HR-specific routes, a
resume prompt inside the gateway, forced YES/NO, gateway-side HR rules, forced
`temperature`/`max_tokens`, gateway-invented confidence scores.

## 2. The two ways to reach this model

The gateway adds a per-system identity to a path that did not have one. It does
**not** close the path that already existed, and the brief requires that fact to
be stated rather than glossed:

> 「Strata 当前 `{{LAB_BIND_ADDR}}:8080` 仍然可由部分内网主机直接访问。必须如实记录这一事实，
> 不能因为 Gateway 上线就声称所有请求已被强制收口。」

| | Through the gateway | Direct to Strata |
|---|---|---|
| Address | `https://{{LAB_HOST}}:8443/v1` | `http://<host>:8080/v1` |
| Credential | per-system key, revocable | the shared Strata key |
| Attribution | per key: who called, when, outcome | none — all callers look alike |
| Limits | 1 upstream slot, 1 per label, queue 8 | whatever Strata does (`parallel 1`) |
| Reachable from | the LAN, HTTPS | any host that can open a TCP connection to 8080 |

The gateway is on a **second path**. A request that goes straight to 8080 is not
limited, attributed or revocable from here, and nothing in this deployment stops
it. `72-accept-gateway.sh` measures the 8080 path with a bare TCP connect and
prints the result; the console card repeats the sentence, so the claim is not
only in a document.

## 3. What a caller needs

Three values, and nothing else:

- **Base URL** — `https://{{LAB_HOST}}:8443/v1`. Declared in
  `config/dell-site.example.toml` under `[public_urls] api-gateway`; the console reads it
  from there rather than assembling it, so the console and the config cannot
  disagree. It is a *published value*, not a constant baked into a client.
- **API Key** — one per system, from the CLI (§5). A key is `llmlab_` followed by
  `secrets.token_urlsafe(32)` — 32 bytes of entropy, which is the part that
  matters, and a prefix that makes a key recognisable to whoever finds one in a
  log.
- **Model name** — Strata's own id, exactly as `GET /v1/models` returns it. The
  benchmark provenance records this deployment serving
  `qwen3.8-flash-next-iq2_xs`; that record is dated, and the live model list is
  authoritative — the acceptance run reads it rather than trusting this line.
  Aliases exist (`--alias short=real`) but **none is configured by default**, so
  a request that asks for a name Strata does not serve is refused by Strata, with
  Strata's own message.

A running example lives in `examples/openai_client.py` and is copied verbatim
into `phase5-acceptance.md`. The short form:

```python
import httpx2                 # what openai 3.x is built on; 1.x and 2.x used `httpx`
from openai import OpenAI

client = OpenAI(
    base_url="https://{{LAB_HOST}}:8443/v1",
    api_key="llmlab_...",                      # shown once, at issue time
    max_retries=0,                             # see §6 -- the gateway does not retry either
    http_client=httpx2.Client(
        verify="{{LAB_ROOT}}/services/caddy/data/pki/authorities/local/root.crt",
    ),
)
print(client.chat.completions.create(
    model="qwen3.8-flash-next-iq2_xs",
    messages=[{"role": "user", "content": "用一句话说明你是运行在哪里的模型。"}],
).choices[0].message.content)
```

The CA file is the node's own root. It is not installed on the client machine by
this deployment, which is why the example names it explicitly instead of
disabling verification; the brief's TLS clause is about a trusted chain, and
`verify=False` is how that clause gets quietly dropped.

The import on the first line is the one version-sensitive line here, and it is
not a detail: `pip install openai` on 2026-10-12 installs **3.29.0**, which
drives `httpx2` and no longer depends on `httpx` at all — so the obvious
`import httpx` raises `ModuleNotFoundError` in the environment a caller gets.
`examples/openai_client.py` therefore imports whichever of the two the installed
SDK brought with it; both answer the only two things used here, `verify=` and
`timeout=`.

**And the answer needs room.** The model behind this deployment is a reasoning
model: it writes its thinking first and starts `content` only afterwards. A
caller that sets a small `max_tokens` gets `finish_reason: "length"` with an
**empty** `content` — measured against the upstream itself, not through the
gateway, where a 64-token budget is spent entirely on thinking. A budget of a
few hundred tokens is enough; the snippet above sets none, and takes the
service's own default.

## 4. Compatibility, measured three ways

The brief is explicit that a 200 is not acceptance:

> 「兼容性验收不能只看 HTTP 200。必须使用真实 OpenAI SDK 测试非流式与流式调用。」

So there are four scripts, and each one can fail while the others pass:

| | Script | What it proves |
|---|---|---|
| HTTP | `72-accept-gateway.sh` | The wire format, the refusals, the 8080 path, the limits (live serialization and a live 429 burst, plus the slot/queue cases against a mock upstream that holds the slot), and resource use. Raw sockets, no library. |
| SDK | `73-accept-gateway-sdk.sh` | The **real `openai` package**, non-streaming and streaming, in its own venv. Parses the report; refuses to pass if `openai` cannot be installed. |
| Console | `74-accept-portal-gateway.sh` | The card an operator reads: the address it publishes, that no digest or key reaches it, that it is not anonymous, and what it says when the gateway is stale or gone. |
| Boundary | `75-accept-gateway-untouched.sh` | That §10's "do not touch" list was honoured — by comparing each process's own kernel start time against the deploy moment, not by asserting it — and that no new LAN listener appeared. |

The readings from all four, on the deployed commit, are in
`phase5-acceptance.md`; this file is what they were written against.

Surface, exactly as the allowlist has it — two routes, and a known-but-
unpublished `/v1/*` route gets a specific 404 rather than a generic one:

| Request | Behaviour |
|---|---|
| `GET /v1/models` | Relayed. The model list is Strata's. |
| `POST /v1/chat/completions` | Relayed. `stream: false` returns JSON; `stream: true` returns SSE, passthrough, chunk by chunk. |
| Anything else under `/v1/` | `404 {"error":{"code":"not_published"}}` — a real Strata feature, through a door that was never opened. |
| Anything outside `/v1/` | `404 {"error":{"code":"not_found"}}`. `/healthz` answers without a key and says only that the process is listening. |

What is **not** rewritten: with no alias configured, the request body is relayed
byte for byte. `temperature`, `max_tokens`, `top_p`, the system prompt, tool
definitions — whatever the caller sends, the model gets. The gateway has no
opinion about any of them, which is what makes it a gateway rather than a second
product with its own defaults.

Errors keep the shape a client library parses — `{"error": {"message", "type",
"code"}}`, never a bare string, because a client that receives a string reports
its own parse failure and sends the reader looking in the client:

| Status | `code` | When | Headers |
|---|---|---|---|
| 401 | `invalid_api_key` | no key, unknown key, disabled, revoked | `WWW-Authenticate: Bearer realm="llm-lab"` |
| 404 | `not_published` / `not_found` | a known route not published here / no such route | — |
| 400 | `bad_length` | `Content-Length` is not a number, or negative | — |
| 411 | `length_required` | a POST with no `Content-Length`, or a chunked body | — |
| 413 | `payload_too_large` | body over 8 MiB, refused before it is read | — |
| 429 | `rate_limited` | the queue is full, or the wait timed out | `Retry-After: <seconds>` |
| 504 | `upstream_timeout` | the model did not answer within 300 s | — |
| *anything else* | *Strata's own* | upstream error | relayed verbatim, status and body |

An upstream failure is never converted into a success. There is no code path that
answers 200 with an apology in it.

## 5. Keys

Managed by a server-side CLI, by decision:

> 「不要为了尽快交付而开放没有管理员鉴权的网页签发 API」

The console has no administrator identity — every reader of it passes the *same*
basic-auth pair — so "who issued this key" would have no answer, and a key-issuing
form behind a shared password is a shared key with extra steps. The console
therefore **displays** keys and **issues** none. Both halves are asserted: `74`
requires the console route to be behind Caddy's basic auth, and the card itself
carries the CLI commands and the sentence "这里不签发 Key —— 是决定，不是没做".

```bash
cd {{LAB_ROOT}}/llm-lab
ADMIN="python3 -m deploy.dell.gateway.admin"

$ADMIN key create hr-system --note "TICKET-1234"   # prints the key ONCE
$ADMIN key list                                    # ids, labels, states, counters
$ADMIN key show k_1a2b3c4d                         # one key as JSON
$ADMIN key disable k_1a2b3c4d                      # stop it, reversibly
$ADMIN key enable  k_1a2b3c4d                      # undo a disable
$ADMIN key revoke  k_1a2b3c4d                      # end it, permanently
$ADMIN usage [--json] [--active-only]              # per-key totals + the process's own
```

What is stored, per key: `id`, `label`, `note`, `created_at`, `disabled`,
`revoked`, `last_used_at`, and four counters. What is stored *of the key itself*
is a digest. The plaintext exists in exactly one place — the terminal it was
printed on — and is unrecoverable from the store, which is why `create` says so
in its own output. The store lives at
`{{LAB_ROOT}}/llm-lab-data/api-gateway/keys.json`, **outside the checkout**,
mode 0600 in a 0700 directory, because a `git reset` must not be able to take
away the record of who holds a key.

**The per-key concurrency bucket is keyed by `label`, not by key id** — two keys
created with the same label share one slot (`gateway.py`: `user = record.get("label")
or ident`). This is deliberate: a system that rotates its key mid-flight should
not be able to hold two slots by accident. It also means the label is the unit of
attribution everywhere — counters, the usage log, the console — so **one label
per system, always**. The console card states this rather than leaving it to be
inferred from two numbers.

## 6. Limits, and what a caller sees when they are hit

The model runs with `parallel 1` on one GPU, so the gateway's job is to make
queueing *visible* instead of making the model slow and blaming it.

| Knob | Value | Why |
|---|---|---|
| Global concurrent requests | **1** | One GPU, `parallel 1`. A larger number here does not make the model faster; it makes the first caller slower than their own timeout, and the failure gets reported against the model. |
| Concurrent requests per label | **1** | A system that runs two calls at once is one that is queuing; the queue is a better place for the second one than the model. |
| Queue depth | **8** | Beyond that, refuse fast. A caller who waits 120 s to be told "no" has burned a client timeout for nothing. |
| Queue wait | **120 s** | A ceiling, set in `[services.api-gateway]`, and **not yet derived from a benchmark** — the measured per-request times in the acceptance record are what a different value would be argued from. |
| `Retry-After` | **5 s** | What a refused caller is told to wait. |
| Max request body | **8 MiB** | A chat request is kilobytes; this is generous and still bounded. |
| Upstream timeout | **300 s** | Connect and read. A generation longer than five minutes is a prompt nobody meant to send. |
| Upstream probe | every **60 s**, 10 s budget | Feeds `upstream.ok` on the console. Never in the request path. |

Each request's timing is split into four numbers rather than one: **queue**
(admission to slot), **TTFT** (first byte of the model's answer), **upstream**
(the whole upstream exchange) and **total**. A single "latency" number hides
exactly the difference an operator needs — whether the model is slow or the queue
is deep.

Two behaviours worth stating because they are easy to get wrong in the other
direction:

- **A client that disconnects mid-stream cancels the upstream request.** The
  slot is released in a `finally`; the upstream socket is closed. A gateway that
  only notices at the end holds the one slot for the length of a generation
  nobody is reading. Such requests are counted as `cancelled`, and `cancelled` is
  **excluded** from the success rate — a rate that drops every time somebody
  closes a tab is a number people stop reading.
- **Nothing in flight is retried.** Not by the gateway, and the example passes
  `max_retries=0` so the SDK does not either. With one slot, an automatic retry
  is a silent doubling of the queue at the moment it is fullest. A caller that
  wants to retry sees a 429 with a `Retry-After` and can decide.

## 7. Privacy (§9)

> 「默认不记录 Prompt、简历正文、模型完整回答或个人身份信息。」

The usage log is one JSON line per request at
`{{LAB_ROOT}}/llm-lab-data/api-gateway/usage.jsonl`, and its field list *is*
the privacy decision:

```
ts  key_id  label  method  path  model  status  outcome  error  stream
queue_ms  ttft_ms  upstream_ms  total_ms  bytes_out  usage
```

No prompt. No message content. No completion text. `bytes_out` is a count, not a
buffer. The file is rotated at 8 MiB with one previous generation kept, because
an append-only log on an unattended box is a disk that fills up on a schedule
nobody wrote down, and that failure arrives looking like "the model stopped
answering".

Token counts obey the second half of the clause:

> 「Token 用量必须来自真实响应或明确标记为估算，不得伪造精确值。」

`usage` is **the upstream's own usage object, verbatim, or `null`**. It is never
estimated, and an absent one is recorded as absent rather than as zeros that
later read like "this call cost nothing". When the upstream reports usage in the
final SSE chunk, it is captured there too.

The refusal to log bodies is also why the console reads a *file* instead of
calling the gateway: an HTTP read would make the console a holder of a key, and
would fail exactly when the gateway is busy — the moment an operator most wants
the reading.

## 8. Deployment, and how to undo it

Layout: the service is a stdlib Python program in the checkout
(`legacy/gateway/`), its state is outside the checkout
(`{{LAB_ROOT}}/llm-lab-data/api-gateway/`), and its lifecycle is `labctl`,
the same as every other service here.

```bash
# the tree, out of a commit -- never out of the working directory, which holds a live key
git archive --add-file=<staging>/.deployed-commit <sha> > /tmp/llm-lab-code.tar
bash deploy/templates/remote.sh --put /tmp/llm-lab-code.tar /tmp/llm-lab-code.tar

bash deploy/templates/node/53-gateway.sh          # install-only: data dir, modes, port free, suite
labctl start api-gateway                     # the normal path for any service
bash deploy/templates/node/52-caddy.sh            # validate and install the front-door template
labctl restart caddy                         # apply it -- see below
bash deploy/templates/node/40-deploy-code.sh      # code: unpack, health check, rollback on failure
```

`53` is install-only on purpose, like `52`: it can be re-run on a live host to
re-check permissions and the suite without bouncing anybody's API call.

**The front door is applied by restarting Caddy, not by reloading it.** `52`
renders the template, checks it and installs it — it starts nothing, and it
cannot: this Caddy runs with `admin off`, so there is no admin API to reload
through. The `/v1/*` handle therefore comes into existence at the moment Caddy
restarts, which is also the moment every request in flight through it ends. That
is why it is done in a window and not under load, and why the line above is
`labctl restart caddy` rather than `caddy reload`.

**`40` restarts every service that runs out of the checkout, not just the
portal.** Residency is decided from the kernel's own `cwd` per managed service in
the config's services table, and "running" by matching that service's launch
argv against `/proc/<pid>/cmdline`. A service that is not running is deliberately
left alone: `labctl restart` clears the stopped-on-purpose flag, so looping over
the config's list would resurrect everything an operator had stopped, on every
deploy. The same restart runs on the rollback path, because a rollback that
restores the files and leaves the gateway executing the failed build has not
finished rolling back. The deploy ends by printing, per service, the pid and the
cwd the kernel reports — which is how the two services that were serving a
deleted directory were found.

To undo: `labctl stop api-gateway`, then revert the Caddy template and restart
Caddy — the `/v1/*` handle disappears and `/v1/*` falls through to the portal
catch-all (which answers 401 and then 404). Deleting the data directory deletes
the key store, so it is the last thing to touch. Strata is untouched throughout,
which is the point: the gateway is a client of Strata, not a modification of it.

## 9. The console panel

`设置与集成 → API Gateway` shows the entry address (copyable), the model list,
key state and last call per system, totals, success rate, the 429 count, and the
queue as an active/waiting pair. It is **read-only**: no issue button, no revoke
button, no limit editor.

It reads the gateway's status document, which the gateway rewrites every 5 s —
so the reading can be **stale**, and it says so: the document's age travels with
the numbers, anything older than 30 s (six ticks) is marked, and a document that
does not identify itself as this gateway's (`service == "llmlab-gateway"`) is
refused rather than rendered as zeros. The path is taken from the service's own
launch line (`--status-file`), so a moved file cannot leave the card showing the
last document from the old location as if it were current.

Only a fixed list of key fields is published to the browser; a field added to the
store later cannot drift to the page. The route is behind Caddy's basic auth —
it names the systems that hold keys, and that list is not for anonymous readers.

## 10. What this record does not claim

- **The 8080 path is still open.** Stated in §2, measured by `72`, repeated on
  the card. Nothing here forces every request through the gateway.
- **The console issues no keys**, by decision (§5), not because it was skipped.
- **The per-key bucket is per label**, not per key (§5). Two keys, one label, one
  slot — deliberate, and stated on the card rather than left as folklore.
- **Throughput equals the model's.** The global limit is 1 because the engine is
  `parallel 1`; the gateway does not add concurrency, it queues it in the open.
- **Token counts are absent when the upstream omits them.** No estimate is
  invented to fill the column (§7).
- **The card is a file read.** Its numbers are as old as the age it prints; a
  stopped gateway leaves its last document behind, and the panel's job is to stop
  showing it as live rather than to pretend it never existed.
