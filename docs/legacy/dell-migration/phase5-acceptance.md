# Phase 5 — acceptance readings (§3, §7, §8, §9, §10, §11)

The design is in `phase5-api-gateway.md`. This file is the other half: **what was
measured on the node, on which date, by which script**, and — as much as the
readings themselves — what those readings do not cover.

Everything below was taken on **2026-10-12** against the tree deployed that day.

| | |
|---|---|
| Node | `{{LAB_HOSTNAME}}` / `{{LAB_HOST}}`, Ubuntu, kernel 7.0.0-14 |
| Deployed commit | `49e4fdc71ddfd99efb6ff7a793600487d857df43` (branch `dev/api-gateway`) |
| Archive sha256 | `7bd362e5c96ecfb1cc44d1782e84a52cdd67a4ef6e15cc7c86f6aa40178bfa06` |
| Deploy verdict | `PASS — code at {{LAB_ROOT}}/llm-lab, serving revision 'api-gateway', health check clean` |
| Scripts | `deploy/templates/node/72|73|74|75-accept-gateway*.sh` |
| Result | **4 scripts, 161 checks, 0 FAIL** (83 + 16 + 36 + 26) |

The four scripts answer four different questions and are kept apart on purpose.
A single script that checks everything is a script whose failures are
indistinguishable from each other, and this project has already written down
what happens when a check reports green over nothing.

| Script | The question it answers |
|---|---|
| `72-accept-gateway.sh` | Does the deployed gateway behave as the brief requires, on the real model, including the limits and the failures? |
| `73-accept-gateway-sdk.sh` | Does a **real OpenAI client library** read it as an OpenAI API? |
| `74-accept-portal-gateway.sh` | Does the read-only console panel read what it says it reads, and leak nothing? |
| `75-accept-gateway-untouched.sh` | Did the change leave the rest of the machine where it was? |

---

## 1. The deliverable, in one place

This is the whole of what an internal system needs. Nothing else has to be
understood to call this model.

| | Value |
|---|---|
| **API Base URL** | `https://{{LAB_HOST}}:8443/v1` |
| **API Key** | one per system, issued by the operator on the node (§1.2) |
| **Model name** | `qwen3.8-flash-next-iq2_xs` — as `GET /v1/models` returns it |
| **Trust** | the node's own root CA: `{{LAB_ROOT}}/services/caddy/data/pki/authorities/local/root.crt` |

The Base URL is a *published value*, declared in `config/dell-site.example.toml` and read
from there by the console, so the console and the config cannot drift apart. It
is not a constant compiled into anything. Read at the deployed tree:

```
ok   the config declares a public Base URL for this service  -- https://{{LAB_HOST}}:8443/v1
ok   the card publishes exactly that one  -- card='https://{{LAB_HOST}}:8443/v1' config='https://{{LAB_HOST}}:8443/v1'
ok   and it is an https address with a path, not a bare host  -- a client appends /chat/completions to this
ok   the internal address is loopback and is published as internal only  -- http://localhost:8200 (via reverse-proxy)
```

The model name is read from the live list rather than from this document. The
day the engine serves a different pack, this line is stale and the reading is
not — which is why `73` asks the SDK for `models.list()` and uses what comes
back.

### 1.1 The client

Pasted verbatim from `examples/openai_client.py`; the whole file is reproduced in
the appendix. Six lines are the entire integration:

```python
import httpx2                 # what openai 3.x is built on; 1.x and 2.x used `httpx`
from openai import OpenAI

client = OpenAI(
    base_url="https://{{LAB_HOST}}:8443/v1",
    api_key="llmlab_...",                      # shown once, at issue time
    max_retries=0,                             # the gateway does not retry either
    http_client=httpx2.Client(
        verify="{{LAB_ROOT}}/services/caddy/data/pki/authorities/local/root.crt",
    ),
)
print(client.chat.completions.create(
    model="qwen3.8-flash-next-iq2_xs",
    messages=[{"role": "user", "content": "用一句话说明你是运行在哪里的模型。"}],
).choices[0].message.content)
```

`pip install openai` and this runs. Measured on the node in the client venv the
acceptance builds (`74`-adjacent, `73`):

```
ok   the SDK imports in its own environment  -- 3.29.0
ok   the client reports success on every shape it tried
ok   it read the model list through the SDK's own model object  -- ['qwen3.8-flash-next-iq2_xs']
ok   a non-streamed completion parsed into choices[0].message.content
     -- {"chars": 20, "finish_reason": "stop", "served_model": "qwen3.8-flash-next-iq2_xs",
         "usage": {"completion_tokens": 106, "prompt_tokens": 61, ...}}
ok   a streamed completion parsed into delta chunks, incrementally
     -- {"chars": 20, "chunks": 105, "total_s": 0.863, "ttft_s": 0.056}
ok   and its first chunk arrived before the answer was complete  -- ttft 0.06s of 0.86s
```

`max_retries=0` is deliberate and is argued in the example's own header: the
engine answers one request at a time, so a silent retry lands *behind* whatever
it was waiting for, and a 429 here is an instruction with a `Retry-After`, not a
transient blip.

### 1.2 Issuing a key

There is no web button, and that is a requirement rather than a shortcut —
§6 of the brief: 「不要为了尽快交付而开放没有管理员鉴权的网页签发 API」. The portal has
no identity of its own (one shared Basic credential at the edge, every client
identity header deleted there), so a web "issue" control would be a control any
holder of the shared password could press. Keys are issued on the node:

```
python3 -m deploy.dell.gateway.admin key create hr-system --note OPS-1234
python3 -m deploy.dell.gateway.admin key list
python3 -m deploy.dell.gateway.admin key disable k_ab12cd34
python3 -m deploy.dell.gateway.admin key enable  k_ab12cd34
python3 -m deploy.dell.gateway.admin key revoke  k_ab12cd34
python3 -m deploy.dell.gateway.admin usage
```

The plaintext is printed once, at creation, to the operator's terminal, and to
nothing else. The store holds a digest; no command can bring a key back. A key
is `llmlab_` + `secrets.token_urlsafe(32)`.

What that buys, measured (`72`):

```
ok   disable is accepted by the CLI  -- k_5f6857ac is now disabled
ok   a disabled key is refused with 401 and named as disabled  -- 'this API key is disabled'
ok   enabling brings it back, without restarting the gateway  -- status 200
ok   a revoked key is refused with 401 and named as revoked  -- 'this API key has been revoked'
ok   a revoked key cannot be resurrected by `enable`
ok   a key of the right shape but unknown -> 401 'no such API key'
ok   a key from another system is refused, and the refusal says why  -- 'no such API key'
     (and it does not look like a key from this service)
```

The gateway notices a CLI write without a restart. That is the property that
makes revocation usable: the answer to "this key leaked" is one command and a
second, not a restart of the thing serving the model.

### 1.3 Error codes

Every refusal is the OpenAI error object — `{"error": {"message", "type",
"code"}}`. The shape is not decoration: a client that receives a bare string
where it expected `error.message` reports its own parse failure, and the person
reading that report looks in the client.

| Status | `code` | When | What the message says |
|---|---|---|---|
| 401 | `invalid_api_key` | no key, unknown key, wrong key space | `no such API key` / `this API key is disabled` / `this API key has been revoked` |
| 404 | `not_found` | a path that is not `/v1/*` at all | `no such route on this gateway` |
| 404 | `not_published` | a real Strata route this gateway does not publish | `<path> is not published by this gateway` |
| 411 / 413 | `length_required` / `payload_too_large` | no `Content-Length`, or over the limit | — |
| 429 | `rate_limited` | the queue is full, or a wait ran out | `the queue is full (8 waiting)` / `waited 120s for a slot and none came free` |
| 502 | `upstream_unreachable` | Strata refused the connection | — |
| *upstream's own* | *upstream's own* | Strata answered, successfully or not | relayed verbatim |

401 carries `WWW-Authenticate: Bearer realm="llm-lab"` and **not** a Basic
challenge — a Basic challenge raises a browser password dialog and breaks every
API client:

```
ok   the challenge is a Bearer one and the body is the OpenAI error shape
     -- WWW-Authenticate 'Bearer realm="llm-lab"', code 'invalid_api_key'
ok   the door is not Caddy's password prompt  -- a Basic challenge would break every API client
```

Read by the SDK rather than by a curl (`73`):

```
ok   a bad key raises the SDK's AuthenticationError, not a generic HTTP error
ok   and the SDK sees the gateway's own error code inside it  -- code 'invalid_api_key'
```

---

## 2. Compatibility (§3): a real client, both shapes

The brief is explicit: 「兼容性验收不能只看 HTTP 200。必须使用真实 OpenAI SDK 测试非流式与流式调用。」 So `72` uses raw HTTP and `73` installs `openai`
and lets it decide.

Non-streaming, on the real model (`72`):

```
ok   a non-streaming chat completion answers 200 with the sentinel word  -- 0.9s, 910 bytes, content 'ZQXMARK'
ok   the model's reasoning survives the relay as its own field  -- reasoning_content 249 chars
ok   the answer is relayed, not rewritten: it is a chat completion object  -- chat.completion
ok   the upstream's own token counts are relayed  -- {"completion_tokens": 81, "prompt_tokens": 89,
     "prompt_tokens_details": {"cached_tokens": 0}, "total_tokens": 170}
```

Streaming (`72`):

```
ok   the stream answers 200 as text/event-stream  -- Content-Type 'text/event-stream'
ok   it carries SSE data lines and a terminator  -- 81 'data:' lines, [DONE] present: True, 19268 bytes
ok   it arrives incrementally, i.e. the gateway does not buffer the answer
     -- 82 reads, sizes [244, 14, 235, 238, 240, 238, 235, 241], gaps [0.039, 0.011, 0.018, 0.0, …]
ok   every frame is a well-formed chat.completion.chunk  -- 80 parseable frames
ok   the streamed deltas assemble into the answer the model gave  -- assembled 'ZQXMARK'
ok   the reasoning arrives as its own delta field, as the model sent it
```

The pasted invocation, unchanged, with no `--check` (`73`):

```
ok   the documented invocation works as pasted -- no --check, text on stdout
     -- rc=0, 455 bytes
ok   and it reports when the first piece of the answer arrived
     -- ['[first piece after 0.05s, whole answer after 0.36s]']
```

### 2.1 What the model does that looks like a gateway bug

The model behind this deployment is a **reasoning model**: it writes
`reasoning_content` first and only then begins `content`. A request whose budget
runs out during the reasoning comes back with `finish_reason: "length"` and an
**empty** `content`. Measured directly against the upstream on 2026-10-12:
`max_tokens=16` → empty, `max_tokens=64` → still empty, `max_tokens=256` → text.

This is the model's behaviour and not the gateway's, and the acceptance proves
it with a control rather than by assertion:

```
ok   CONTROL: the upstream itself answers a 16-token budget with empty content,
     so the gateway is not the one dropping text  -- upstream 200, finish_reason 'length', content ''
ok   and through the gateway that same answer is relayed as it stands,
     not filled in with text the model never produced  -- gateway 200, 'length', content ''
```

The consequence for a caller is in the notes of `72`, and it is worth repeating
here because it is the first thing a new integration hits: **a caller that sets
`max_tokens` too low gets no text at all.** The example's default budget is 512
rather than the 48 or 64 an example would normally use, and the note in `73`
records that this number was measured with a *different* prompt and therefore
does not carry over as a number — only as the instruction to leave room for the
thinking.

---

## 3. One at a time, and a bounded queue (§7)

The engine runs `parallel=1`. The gateway's job is to make that fact honest
rather than to hide it: one upstream slot, a bounded queue, and a `429` with a
`Retry-After` for whoever arrives at a full one.

Serialization, measured live (`72`):

```
ok   the baseline this comparison needs: one caller on its own, served  -- status 200 in 0.54s
ok   and they were served one after another: the wall time scales with the number of callers
     -- 1 caller 0.54s; 3 at once 1.43s (2.7x) -- running concurrently would be about 1.0x
ok   the gateway's own log agrees, and says how long they waited
     -- queue_ms of the last 3 non-streaming chat completions on this key: [0.0, 475.3, 949.6]
```

The `queue_ms` column is the part that matters: the second and third callers
waited 0.48 s and 0.95 s, which is 1× and 2× the model's own answer time. That
is a queue, observed from the gateway's own accounting rather than inferred from
wall-clock arithmetic.

The deployed instance's own 429, measured live by arriving twelve at once:

```
ok   with the queue full, some callers are refused with 429 while the rest are served
     -- and none is answered with anything else  -- 3 refused, 9 served, 0 other, all 12 answered in 1.6s
ok   the refusal names the queue rather than a wait that ran out  -- ('rate_limited', 'the queue is full (8 waiting)')
ok   and it carries a Retry-After  -- Retry-After ['5']
ok   the refusal comes back at once -- it is not a queue that holds everyone  -- slowest refusal 0.02s
```

A note in `72` says what that burst does **not** prove, and it is worth carrying
here: a refusal rate is not a property of this service. It is a function of how
many callers arrive at once, and the model's half-second answer time is what
keeps the number small. Twelve callers arriving together is a deliberate
overload, not a traffic estimate.

### 3.1 The cases a live deployment cannot produce

Three of the brief's clauses need a request that is *still being answered*
seconds later: a caller that waits and then times out, a caller that hangs up
mid-answer, and proof that the slot comes back afterwards. **The real
deployment cannot produce those.** The model answers a whole request in about
half a second, so there is no way for a caller to hold a slot past a wait
timeout — and an earlier version of `72` asserted exactly that, against the live
instance, and failed, because a caller that sleeps on its own side holds a
*socket*, not a slot.

So those three are measured against a **mock upstream that holds the slot on
purpose** — a second instance of the same program, on a loopback port, driving a
paced mock:

```
ok   CONTROL: the live view shows it holding that slot  -- active>=1 after 4.0s (the status tick is 5s)
ok   a caller arriving at a full one-deep queue is refused with 429 at once  -- 'the queue is full (1 waiting)'
ok   a caller that was already waiting is refused when its wait runs out
     -- status 429 after 5.0s: 'waited 5s for a slot and none came free'
ok   both refusals are recorded, with the wait that preceded them
     -- [('limit_queue', 0.0), ('limit_timeout', 5000.5)]
ok   the slot is released when the caller hangs up mid-answer  -- active==0 after 3.0s
ok   and the gateway let go of the upstream rather than reading it to the end
     -- the mock's writes started failing 1 time(s): the gateway closed the upstream connection
ok   the abandonment is recorded as cancelled, not as a failure of the key or of the service
ok   the next caller is served normally after that disconnect -- the slot really did come back
```

**This is a reading about the gateway, not about the deployed instance**, and
the script says so in its own header. What is claimed for the deployed instance
is the half that does not need a slow answer: that it serializes, that its
`queue_ms` says so, and that its own queue-full path returns a real `429`.

---

## 4. Privacy and the log (§9)

The first use case for this gateway is HR, so the brief's rule is that prompts,
résumé bodies, complete model answers and personal identifiers are **not**
recorded by default: 「默认不记录 Prompt、简历正文、模型完整回答或个人身份信息。」

`usage.jsonl`, one JSON object per line, holds exactly this and nothing else:

```
['bytes_out', 'error', 'key_id', 'label', 'method', 'model', 'outcome', 'path',
 'queue_ms', 'status', 'stream', 'total_ms', 'ts', 'ttft_ms', 'upstream_ms', 'usage']
```

```
ok   usage.jsonl is one JSON object per line  -- 102 lines, 102 parsed
ok   each record carries the fields the console and an operator read  -- missing: []
ok   no record carries a request or a response body field
ok   a streamed request is recorded as streamed and with a first-byte time  -- [('/v1/chat/completions', 2.8)]
ok   the prompt sentinel is in no file the deployment writes
ok   the caller's key is not in Caddy's access log  -- Authorization is written as the redaction placeholder
..   what was searched  -- 75 files: the gateway's own log, the other services' logs,
     Caddy's access log, the usage log and the status document
```

The search is for a sentinel planted in the prompt, over **75 files**, including
the other services' logs and Caddy's access log — because the interesting
failure is never "our log holds it", it is "somebody's log holds it". `73`
repeats the search with a different prompt and a different client:

```
ok   the prompt and the answer are in no gateway log line  -- 6 log file(s), 11435 bytes
```

The console panel is checked the same way, and this is the check that a
read-only console most easily fails:

```
ok   no string shaped like a key appears anywhere in the response  -- checked for 'llmlab_'
ok   not one stored digest appears in the response  -- 15 digest(s) in the store, 0 rendered
ok   no key row carries a digest field  -- 15 key row(s) rendered
```

### 4.1 Token counts

§9: 「Token 用量必须来自真实响应或明确标记为估算，不得伪造精确值。」 The gateway
does not compute counts. It relays the upstream's own `usage` object when there
is one and reports `null` when there is not:

```
..   token usage  -- {"completion_tokens": 81, "prompt_tokens": 89, ...}
     (the upstream's own object, relayed verbatim)
..   token usage the SDK saw  -- the upstream sent none on this response, so the client saw
     null -- reported as absent, not filled in
```

No number in this system is an estimate passed off as a measurement, because no
number in this system is computed by the gateway at all.

### 4.2 Latency

The live view decomposes latency instead of reporting one number, so that "it was
slow" can be answered with *which part* was slow:

```
ok   it carries process totals and a latency decomposition
     -- {"avg_queue": 0.0, "avg_total": 242.7, "avg_ttft": 164.5, "avg_upstream": 78.2, "samples": 6}
```

---

## 5. The front door and the unchanged machine (§4, §10)

### 5.1 The new route

`/v1/*` is its own Caddy route with **only** `Authorization: Bearer`. No Basic
auth is stacked in front of it, which is checked from both ends — an anonymous
caller gets the gateway's error, and a *valid portal credential* is still not a
gateway key:

```
ok   and through the front door the client gets the gateway's own error, not a generic 4xx
     -- status 401, {"error": {"message": "no such API key", "type": "invalid_request_error",
        "code": "invalid_api_key"}}
ok   a portal credential is not a gateway key  -- status 401 -- the /v1 path does not accept Basic,
     so no credential is stacked in front of the gateway's own
```

And the rest of the front door is where it was (`72`, `75`):

```
ok   the portal still answers  -- status 200
ok   Grafana is still behind /grafana/  -- status 302
ok   Strata's console is still behind /strata/  -- status 200
ok   and the new route did not publish anything else  -- 404 '{"error": "no route for /prometheus/api/v1/query"}'
ok   the front door still refuses an anonymous reader of the portal  -- status 401
ok   and serves the portal to a viewer who has the credential  -- status 200
```

### 5.2 The machine it was added to

`75` answers the §10 boundary directly. The instrument is not a port diff — a
port can be re-bound by a new process and look identical — but each process's
**own start time** read from `/proc/PID/stat` against `/proc/stat`'s `btime`,
compared with the deploy moment (the mtime of `.deployed-commit`, written as the
code lands). A process whose start time predates the deploy was not restarted by
it, and no process can be made to look older than it is.

```
..   the code at {{LAB_ROOT}}/llm-lab landed 2026-10-12 12:49:50
..   revision: 49e4fdc71ddfd99efb6ff7a793600487d857df43
ok   Strata's model server was not restarted by the deploy  -- pid 32750 started 2026-10-10 02:17:50
ok   Strata's engine was not restarted by the deploy  -- pid 32756 started 2026-10-10 02:17:50
ok   Grafana was not restarted by the deploy  -- pid 220400 started 2026-10-10 20:10:42
ok   Prometheus was not restarted by the deploy  -- pid 209780 started 2026-10-10 19:27:40
ok   EvalScope was not restarted by the deploy  -- pid 212682 started 2026-10-10 19:40:19
ok   the gateway is running the freshly deployed code  -- pid 328375 started 2026-10-12 12:50:52
ok   the portal is running the freshly deployed code  -- pid 328364 started 2026-10-12 12:50:52
ok   the metrics adapter is running the freshly deployed code  -- pid 328382 started 2026-10-12 12:50:53
```

The engine is asserted separately from its launcher because "Strata was not
restarted" is a claim about the process holding the loaded weights; a restarted
launcher in front of a surviving engine would mean something quite different.

No new LAN listener:

```
..   listening on a non-loopback address right now:
       {{LAB_BIND_ADDR}}:22
       {{LAB_BIND_ADDR}}:8080
       {{LAB_HOST}}:8443
       [::]:22
ok   the gateway's port is not one of them  -- 8200 is bound to loopback only
ok   the gateway accepts on loopback  -- localhost:8200
ok   and refuses the same port on the LAN address  -- {{LAB_HOST}}:8200 did not accept
```

This satisfies §10's 「不开放新的 LAN 监听端口」, and it is measured by knocking,
not by reading a config: a listener that refuses a LAN connection is not
reachable from the LAN whatever `ss` prints.

### 5.3 What is still open, stated rather than glossed

§10 requires this to be recorded and forbids the claim that the gateway closes
it: 「Strata 当前 `{{LAB_BIND_ADDR}}:8080` 仍然可由部分内网主机直接访问。必须如实记录这一事实，
不能因为 Gateway 上线就声称所有请求已被强制收口。」

`{{LAB_BIND_ADDR}}:8080` is still bound and still answers, and the launch line it answers
with is read from the kernel's copy of its argv:

```
ok   Strata still serves on every interface, as it did before -- the gateway does not close this
     -- {{STRATA_ROOT}}/.venv/bin/python -u {{STRATA_ROOT}}/serve/server.py --engine strata
        --config {{STRATA_ROOT}}/strata-iq2_xs.json --host {{LAB_BIND_ADDR}} --port 8080
```

The exposure is real and it is stated plainly: **anything that can route to
`{{LAB_HOST}}:8080` reaches the model API without passing through the
gateway.** The gateway adds attributed, revocable, per-system keys on a second
path. It does not remove the first one.

One correction to how that exposure is usually described, found by this
acceptance. An earlier draft of `75` asserted that Strata "answers without a
key". It does not, and the assertion failed:

```
ok   Strata refuses a caller with no key, in its own error shape
     -- status 401, {"error": {"type": "authentication_error", "message": "missing or wrong API key"}}
ok   and answers the internal Strata key  -- status 200, 220 bytes
ok   a *gateway* key is not an upstream credential  -- status 401 -- the two key spaces are separate
```

So the honest wording is **a reachable port plus a credential that lives on this
host** — narrower than an open model API, and not narrower than the brief says.
The shared Strata key is the thing to rotate if this path is ever abused, and
nothing on this path is attributed to a system.

---

## 6. What it costs

Measured on the node, not asserted:

```
ok   the gateway's resident memory is small -- under 150 MB  -- 28040 kB RSS (27.4 MB, 0.09% of the box's 31 GB)
..   its own idle CPU over 6.0s  -- 0.00% of one core across 14 threads
..   scope of that measurement  -- taken while this run was the only caller.  It says nothing about
     behaviour at the queue depth the brief describes, which is what the mock upstream in the selftest exercises.
```

27.4 MB resident, on a box where the model itself needs 30 GB. The idle-CPU note
is scoped deliberately: an idle measurement is not a load measurement, and the
queue-depth case lives in the mock section for that reason.

---

## 7. What these readings do not cover

This section is the point of the file as much as §2 is. A number that travels
without its scope is worse than no number.

1. **A full end-to-end load test at the queue depth §7 describes** has not been
   run against the live instance, and cannot be until the engine is
   `parallel>1`. The queue behaviour is measured against the mock.
2. **The three live upstream failures** — a 401 (needs a gateway started with a
   wrong Strata key), a 500 (nothing here can make the engine fail on demand)
   and a timeout (needs a generation held past 300 s) — are exercised against the
   mock in `legacy/gateway/selftest.py`. **None is claimed from the live
   run.** The one upstream failure that *was* produced live is a 400 from a
   malformed body, and it is relayed byte-for-byte:
   `gateway 'Unterminated string starting at: line 1 column 11 (char 10)'` vs
   `upstream` the same string.
3. **`75` compares start times and listeners.** It does not diff configuration,
   does not read Strata's loaded weights, and cannot list the hosts elsewhere on
   the LAN that reach `:8080`.
4. **The 429 rate is not a service property.** It is a function of arrival rate;
   the measurement is a deliberate twelve-at-once burst.
5. **The SDK acceptance pins a version.** `openai` 3.29.0, which is built on
   `httpx2` rather than `httpx`; `import httpx` raises `ModuleNotFoundError` in
   that environment, which is why the example imports either. A future major
   version is not covered by this run.
6. **`74`'s live half is the console's read path.** The cases a live gateway
   will not produce — a stale document, a document belonging to another service,
   a launch line with no `--status-file` — are measured against fabricated
   inputs, and the script cleans them up.

---

## 8. Reproducing this

The readings above are reproducible from the deployed commit:

```
# build the archive, ship it, and deploy it
git archive --format=tar --add-file=<staging>/.deployed-commit -o /tmp/llm-lab-code.tar <sha>
bash deploy/templates/remote.sh --put /tmp/llm-lab-code.tar /tmp/llm-lab-code.tar
bash deploy/templates/remote.sh deploy/templates/node/40-deploy-code.sh          # EXPECT=api-gateway

# the four acceptance runs
bash deploy/templates/remote.sh deploy/templates/node/72-accept-gateway.sh
bash deploy/templates/remote.sh deploy/templates/node/73-accept-gateway-sdk.sh
bash deploy/templates/remote.sh deploy/templates/node/74-accept-portal-gateway.sh
bash deploy/templates/remote.sh deploy/templates/node/75-accept-gateway-untouched.sh
```

`73` builds its own venv and installs the SDK; the first run is slower than the
rest. Each script revokes every key it issued before it exits, and `72` and `73`
both assert at the end that the keys are dead.

---

## Appendix — `examples/openai_client.py`, verbatim

```python
#!/usr/bin/env python3
"""Call the LLM Lab model service the way any OpenAI client would.

    pip install openai
    python3 openai_client.py                       # a short demo, streamed
    python3 openai_client.py --check               # verify the wiring (JSON)

Three things are needed and nothing else: the base URL, a key, and the model
name.  The first two are below; the third is whatever `--model` is, or the one
model the service advertises if it is left out.

WHY THE --ca FLAG EXISTS, AND WHY THERE IS NO FLAG TO TURN VERIFICATION OFF.
The address is signed by this deployment's own certificate authority, so a
client that trusts only the public roots refuses it -- correctly, because it has
no reason to believe that host is who it says it is.  The fix is to tell the
client about the root (`root.crt`, from the operator, or downloaded from the
node with `deploy/templates/fetch-caddy-root.sh`), which keeps verification *on*.
A client that checks nothing is a client that would not notice the day it was
talking to something else, and this service carries the prompt text of whoever
calls it -- so it is the wrong place to make that trade.

WHY max_retries IS ZERO.  The SDK retries connection errors and 429s by default.
Against this service that would be a mistake twice over: the engine runs one
request at a time, so a retry lands *behind* whatever it was waiting for and
makes the caller's own latency worse; and a 429 here is a deliberate instruction
to come back later, not a transient blip.  A caller that wants to wait should
read `Retry-After` and decide for itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_BASE = "https://{{LAB_HOST}}:8443/v1"
DEFAULT_CA = "{{LAB_ROOT}}/services/caddy/data/pki/authorities/local/root.crt"
DEFAULT_PROMPT = "用一句话说明你是运行在哪里的模型。"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass


def read_key(args) -> str:
    """The key, from --key, then OPENAI_API_KEY, then one line of stdin.

    Stdin is the last resort on purpose: it keeps the key out of the process
    argv (`ps` shows those to every user on the box) and out of the shell
    history, which is where a key pasted into a command line ends up.
    """
    if args.key:
        return args.key.strip()
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip()
    if sys.stdin.isatty():
        sys.stderr.write("paste the API key and press enter: ")
    line = sys.stdin.readline()
    return line.strip()


def _hx():
    """The HTTP client library the installed SDK is built on.

    openai 3.x drives `httpx2` (pydantic's successor to httpx) where 1.x and 2.x
    drove `httpx`.  The two are identical for the only two things used here,
    `verify=` and `timeout=`, so this imports whichever one the SDK brought
    with it rather than naming one and failing to start against the other.
    Measured on the node 2026-10-12: `pip install openai` gives 3.29.0, and in
    that environment a plain `import httpx` raises ModuleNotFoundError --
    httpx is no longer a dependency of the SDK, it was replaced, not added to.
    """
    try:
        import httpx2
        return httpx2
    except ImportError:                                # openai 1.x / 2.x
        import httpx
        return httpx


def build_client(args, key):
    from openai import OpenAI

    hx = _hx()
    timeout = hx.Timeout(args.timeout, connect=10.0)
    return OpenAI(
        base_url=args.base_url,
        api_key=key,
        max_retries=0,
        timeout=timeout,
        http_client=hx.Client(verify=args.ca, timeout=timeout),
    )


def _err(exc) -> dict:
    """What the SDK made of a refusal.  Reported, never swallowed."""
    body = getattr(exc, "body", None)
    code = None
    if isinstance(body, dict):
        code = (body.get("error") or {}).get("code") if isinstance(
            body.get("error"), dict) else body.get("code")
    return {"type": type(exc).__name__, "status": getattr(exc, "status_code", None),
            "code": code, "message": str(exc)[:300]}


def check(client, args) -> dict:
    """Run each shape once and report what happened.  Nothing here is inferred:
    a field is only non-null if the response carried it."""
    out = {"ok": False, "base_url": args.base_url, "model": None,
           "models": None, "chat": None, "stream": None, "errors": {}}
    try:
        listing = client.models.list()
        out["models"] = [m.id for m in listing.data]
    except Exception as exc:                           # noqa: BLE001
        out["errors"]["models"] = _err(exc)
        return out

    model = args.model or (out["models"][0] if out["models"] else None)
    out["model"] = model
    if not model:
        out["errors"]["model"] = {"type": "ValueError",
                                  "message": "the service advertised no model"}
        return out

    try:
        r = client.chat.completions.create(
            model=model, max_tokens=args.max_tokens,
            messages=[{"role": "user", "content": args.prompt}])
        text = (r.choices[0].message.content or "") if r.choices else ""
        out["chat"] = {
            "chars": len(text), "finish_reason": r.choices[0].finish_reason
            if r.choices else None,
            "served_model": r.model,
            # The upstream's own counts, or null.  Not computed, not estimated.
            "usage": r.usage.model_dump() if r.usage else None,
        }
    except Exception as exc:                           # noqa: BLE001
        out["errors"]["chat"] = _err(exc)

    try:
        t0 = time.monotonic()
        chunks, ttft, text = 0, None, []
        for chunk in client.chat.completions.create(
                model=model, max_tokens=args.max_tokens, stream=True,
                messages=[{"role": "user", "content": args.prompt}]):
            if ttft is None:
                ttft = round(time.monotonic() - t0, 3)
            chunks += 1
            if chunk.choices and chunk.choices[0].delta.content:
                text.append(chunk.choices[0].delta.content)
        out["stream"] = {"chunks": chunks, "chars": len("".join(text)),
                         "ttft_s": ttft,
                         "total_s": round(time.monotonic() - t0, 3)}
    except Exception as exc:                           # noqa: BLE001
        out["errors"]["stream"] = _err(exc)

    # A key that is not ours must be refused, and refused in a way this client
    # understands.  This is the check that says the error shape on the wire is
    # the OpenAI one and not merely "something 4xx".
    try:
        build_client(args, "llmlab_definitelynotakeydefinitelynotakey0000"
                           "0000").models.list()
        out["errors"]["bad_key"] = {"type": "None: accepted", "status": 200,
                                    "message": "a bogus key was accepted"}
    except Exception as exc:                           # noqa: BLE001
        out["errors"]["bad_key"] = _err(exc)

    out["ok"] = (bool(out["models"]) and out["chat"] is not None
                 and out["stream"] is not None
                 and out["errors"]["bad_key"]["type"] == "AuthenticationError")
    return out


def demo(client, args) -> int:
    model = args.model
    if not model:
        model = client.models.list().data[0].id
    print("model : %s" % model)
    print()
    print("--- not streamed ---")
    r = client.chat.completions.create(
        model=model, max_tokens=args.max_tokens,
        messages=[{"role": "user", "content": args.prompt}])
    print(r.choices[0].message.content)
    if r.usage:
        print("[usage] %s" % json.dumps(r.usage.model_dump(), sort_keys=True))
    print()
    print("--- streamed (each piece as it arrives) ---")
    t0 = time.monotonic()
    first = None
    for chunk in client.chat.completions.create(
            model=model, max_tokens=args.max_tokens, stream=True,
            messages=[{"role": "user", "content": args.prompt}]):
        if first is None:
            first = time.monotonic() - t0
        if chunk.choices and chunk.choices[0].delta.content:
            sys.stdout.write(chunk.choices[0].delta.content)
            sys.stdout.flush()
    print()
    print()
    print("[first piece after %.2fs, whole answer after %.2fs]"
          % (first or 0.0, time.monotonic() - t0))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="an OpenAI client for the LLM Lab "
                                             "model service")
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL",
                                                         DEFAULT_BASE))
    ap.add_argument("--model", default=None,
                    help="default: the first model the service advertises")
    ap.add_argument("--key", default=None,
                    help="default: $OPENAI_API_KEY, then one line of stdin")
    ap.add_argument("--ca", default=DEFAULT_CA,
                    help="the PEM the service's certificate chains to "
                         "(default: %(default)s)")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--check", action="store_true",
                    help="run every shape once and print a JSON report")
    args = ap.parse_args(argv)

    key = read_key(args)
    if not key:
        print("no API key: pass --key, set OPENAI_API_KEY, or pipe one in",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.ca):
        print("the CA at %s does not exist.\n"
              "Ask the operator for root.crt (or run "
              "deploy/templates/fetch-caddy-root.sh).  Do not work around this by "
              "turning verification off." % args.ca, file=sys.stderr)
        return 2

    client = build_client(args, key)
    if args.check:
        report = check(client, args)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    try:
        return demo(client, args)
    except Exception as exc:                           # noqa: BLE001
        print("failed: %s" % json.dumps(_err(exc), ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```
