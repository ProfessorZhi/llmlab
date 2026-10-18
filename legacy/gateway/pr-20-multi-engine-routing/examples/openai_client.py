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
