# Vision request samples and the contract behind them

> **Legacy reference.** Imported from the private `llm-lab` repository (legacy
> PR #12). It is not wired into `llm-lab-portal` and is not a supported adapter.
> Model endpoint names and addresses were replaced with example values.

Everything here runs on a CPU. Nothing here talks to the model service, opens
the GPU, or needs a picture from outside the repository.

| file | what it is |
|---|---|
| `vision_client.py` | **the consumer contract** — validate, size, build, send, parse |
| `consumer_e2e.py` | caller → gateway → a stand-in vision-enabled service, on CPU |
| `fixtures.py` | pictures on demand: 1/2/4 of them, distinct, sized to a byte count |
| `cpu-only-realism.md` | can a real answer be produced without the GPU? measured answer: no |
| `make_synthetic_image.py` | draws the on-disk fixtures (PNG / EXIF-rotated JPEG / WebP) |
| `chat_yesno.openai.json` | a picture + question in the OpenAI shape |
| `chat_yesno.anthropic.json` | the same, in the Anthropic Messages shape |
| `request_contract.py` | runs both samples and the whole image path through **Strata's own code** |

```bash
python3 examples/vision/make_synthetic_image.py     # draws the pictures
python3 examples/vision/request_contract.py         # 40 checks, no GPU
python3 examples/vision/consumer_e2e.py             # 65 checks, no GPU
```

## The consumer side

`vision_client.py` is what a caller (the portal, a screener, an evaluation) has
to get right, with every refusal raised **before a socket is opened** and every
error carrying a `code`, a `field` and a `remedy` written to be shown to a
person:

* **`image_url.url` must be a string.** A JSON number there reaches Strata's
  request handler, which calls `.startswith` on it, and becomes an
  `AttributeError` — a 500 on a path where every other input error is a 400.
  That is an upstream bug, recorded rather than fixed; the caller's half of it
  is to not send one, and `validate_image_url` is that half.
* **The body budget is computed, not estimated**, including base64's 4/3
  inflation, by building the request and measuring it — so an 8 MiB limit is
  correctly reported as *about a 6 MiB picture*. The remedy names the largest
  picture that would have fitted, solved by bisection against the real builder.
* **An interrupted upload is a limit problem.** The gateway answers on the
  `Content-Length` and hangs up while the body is still going out, so an
  oversized picture arrives as a dead socket. `TransportRefused` marks that
  `upload_interrupted=True` and says so, because otherwise it is indistinguishable
  from "the service is down".
* **A verdict is a verdict.** `parse_verdict` accepts YES and NO and nothing
  else; `"I cannot see images"` yields `None`, never `NO`. A screener that mapped
  a refusal to a rejection would fail a candidate and never see why.
* **Both answer shapes**, bare YES/NO and a business JSON document, with
  `Reply.verdict` and `Reply.document` kept distinct so the two cannot be
  confused.
* **Streaming, with a fallback** that is reported (`Reply.fell_back`) rather than
  silent.

## The end-to-end run, and what it does not settle

`consumer_e2e.py` drives the real consumer and the real gateway against a
stand-in that advertises `"images": true` and reads the image parts. It checks
1/2/4 pictures arrive with the right count, order, mime and bytes — compared
three ways, fixture against wire against upstream — that a WebP is converted
before it is sent, that an http URL is passed through with the client opening no
connection to it, that the 8 MiB budget holds at the boundary, and that no
picture or document text reaches a log or comes back in a response.

The stand-in **answers from the request it received, not from the picture**. It
cannot see. Every check there is about transport, and the run ends by printing:

```
MODEL VISUAL COMPREHENSION: NOT PASSED
```

That line stays until an instance with the encoder loaded answers two different
pictures differently. `cpu-only-realism.md` has the measurements behind why that
cannot happen on this box yet.

## What was wrong with the samples this replaces

Two things, and both meant the sample could not be run by anyone:

1. **The model id was not a model this box serves.** The samples asked for
   `qwen3.8-flash-next`. The running Strata reports `qwen3.8-flash-next-iq2_xs`
   from `GET /health`, `GET /v1/models` returns the same through the gateway, and
   `docs/dell-migration/phase5-acceptance.md` records it being read back through
   the OpenAI SDK. A request naming a model that does not exist fails before any
   question about pictures is reached — so a sample with the wrong id is not a
   vision sample at all.
2. **The picture was not in the repository.** Both samples pointed at
   `evidence/resume_sample.png`, which does not exist and never did. The only way
   to make the sample run was to supply a real résumé, and a fixture that has to
   be somebody's CV is a fixture nobody can commit.

The pictures are now drawn from constants — a red square, a blue circle, an
EXIF-rotated JPEG, a WebP — so they are identical on every machine and carry no
personal data.

`request_contract.py` fails if either problem comes back: it reads the samples,
checks the model id, fills the placeholder with the drawn fixture, and parses the
result with `serve.frontend.openai_to_messages` / `anthropic_to_messages`.

## What `request_contract.py` actually proves

It imports `serve/server.py` and `serve/frontend.py` **from the installed
Strata** and calls the same functions a live request calls. It is not a
re-implementation, so a pass is a statement about the code that runs, not about
a copy of it. It checks:

* an `image_url` part becomes one image source, in document order, with the
  text part and the system prompt intact;
* `Vision.load` on a `data:` URL, a bare path and `file://` returns exactly the
  bytes that went in;
* every unusable source — UNC, `//host/share`, a bad scheme, a missing file — is
  refused as a **`ValueError`**, which is what becomes a 400;
* `Vision.download` enforces the 32 MiB cap, on the announced `Content-Length`
  *and* on the body, against a mock HTTP server on localhost (the only socket it
  opens);
* `Vision.normalize` passes PNG/JPEG/BMP/GIF through byte-for-byte and converts
  WebP to PNG, and refuses bytes that are not an image.

Two guards make the "no GPU" claim testable rather than asserted:
`Vision._start` — the only way the encoder process is created — is replaced with
a tripwire that fails the run if anything reaches it, and the run checks that no
GPU library was imported. It also imports with `sys.dont_write_bytecode` set, so
no `.pyc` is left in the installed tree.

## What it does **not** prove, and must not be reported as proving

**It says nothing about whether a model can read a picture.**

The path has four steps: part → source string (1), source → bytes (2), bytes →
something the decoder accepts (3), bytes → embeddings → an answer (4). This file
covers 1–3. Step 4 is the encoder and the model, and it needs a GPU.

On this host step 4 is **not merely untested, it is unimplemented**: Strata is
running without `--vision` and `GET /health` reports `"images": false`. The
encoder binary and the `mmproj` weights exist but are only in an isolated
workspace, and the NVML driver/library mismatch means the host may not be
restarted to deploy them.

`legacy/gateway/vision_protocol_selftest.py` carries the control for this:
it sends two *different* pictures and gets the same answer for both. That is what
an upstream which cannot see looks like from the outside, and it is why a 200
with the word YES in it is not evidence of anything about the picture.

## The gateway in front of the model

A picture request passes through `legacy/gateway/gateway.py`, which is a
byte relay that knows about keys, limits and aliases and nothing about images.
`legacy/gateway/vision_protocol_selftest.py` asserts that, including that a
nonsense image part is *forwarded* rather than rejected — the gateway has no way
to know what the model can read, so refusing would be it inventing a judgement
it cannot make. Three limits found there are worth knowing before wiring a
client up:

* **`--max-body-mib` defaults to 8.** Since base64 inflates by 4/3, that is
  about a **6 MiB** picture, not 8. The gateway is launched with no override.
* **`/v1/messages` is not published** by the gateway (allowlist: `GET
  /v1/models`, `POST /v1/chat/completions`). An Anthropic image block is only
  reachable by talking to Strata directly.
* **The running gateway has no aliases** (`aliases: {}`), so every caller must
  already name `qwen3.8-flash-next-iq2_xs`. Above 1 MiB the gateway stops
  parsing the body for the `model` field at all, so alias translation and large
  pictures do not compose — a latent trap if an alias is ever configured.

Run it from the repository root:

```bash
python3 legacy/gateway/vision_protocol_selftest.py    # 48 checks, mock upstream
```
