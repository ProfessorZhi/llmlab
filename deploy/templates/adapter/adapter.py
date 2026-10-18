#!/usr/bin/env python3
"""Translate Strata's JSON metrics into the Prometheus text format.

Strata exposes a rich JSON document at `GET /metrics` and no Prometheus
endpoint.  This sits between them and does exactly one thing: rename fields and
retype them.  It does not compute rates, averages, efficiencies or any other
derived number -- Prometheus already does that, does it correctly across
restarts and scrape gaps, and doing it in two places is how the two places come
to disagree.  A translator that starts interpreting is no longer a translator.

Two failure levels are kept apart on purpose:

  * this process not answering   -> Prometheus records `up 0` for the target
  * Strata not answering         -> this still answers 200, with
                                    `llmlab_strata_up 0` and the other series absent

Collapsing them would make "the inference server is down" and "the exporter is
down" the same alert, and they have different remedies.

Stdlib only, deliberately: it needs no venv, so there is no environment to
rebuild and nothing that can drift from the interpreter that runs it.  It reads
the API key from a file, never from argv, so the value cannot appear in `ps`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
PREFIX = "llmlab_strata"

# field -> (prometheus name suffix, type, help, scale)
#
# `scale` is a unit conversion, not a judgement about the value: Strata reports
# memory in bytes and Prometheus' convention is that a base unit goes in the
# name, so a byte value keeps its number and gains `_bytes`.  Ratios are the
# exception worth converting -- a percentage is not a Prometheus unit, and
# leaving one in a metric named `_pct` guarantees somebody multiplies it by 100
# twice.
GAUGES = {
    "hardware.gpu_util":          ("gpu_utilization_ratio", 0.01),
    "hardware.gpu_mem_used":      ("gpu_memory_used_bytes", 1),
    "hardware.gpu_mem_total":     ("gpu_memory_total_bytes", 1),
    "hardware.gpu_temp":          ("gpu_temperature_celsius", 1),
    "hardware.gpu_power":         ("gpu_power_watts", 1),
    "hardware.gpu_power_limit":   ("gpu_power_limit_watts", 1),
    "hardware.gpu_pcie_gen":      ("gpu_pcie_generation", 1),
    "hardware.gpu_pcie_gen_max":  ("gpu_pcie_generation_max", 1),
    "hardware.gpu_pcie_width":    ("gpu_pcie_width_lanes", 1),
    "hardware.gpu_pcie_rx_mb":    ("gpu_pcie_rx_bytes", 1048576),
    "hardware.gpu_pcie_tx_mb":    ("gpu_pcie_tx_bytes", 1048576),
    "hardware.cpu":               ("cpu_utilization_ratio", 0.01),
    "hardware.ram_used":          ("ram_used_bytes", 1),
    "hardware.ram_total":         ("ram_total_bytes", 1),
    "hardware.disk_read_mb":      ("disk_read_bytes", 1048576),
    "hardware.disk_write_mb":     ("disk_write_bytes", 1048576),
    "hardware.tok_s":             ("tokens_per_second", 1),
    "hardware.tok_s_mean":        ("tokens_per_second_mean", 1),
    "hardware.prefill_tok_s_mean": ("prefill_tokens_per_second_mean", 1),
    "conversation_cache.prompt_tokens": ("cache_prompt_tokens", 1),
    "conversation_cache.last_reused":   ("cache_last_reused_tokens", 1),
    "conversation_cache.last_prompt":   ("cache_last_prompt_tokens", 1),
    "requests_kept":              ("requests_kept", 1),
}

# Counters: monotonic since the process started.  Traffic accounting lives here,
# not in the gauges above, because these are the numbers that must survive a
# scrape gap -- a gauge sampled every 30 seconds loses whatever happened in
# between, and a counter does not.
COUNTERS = {
    "totals.requests":         ("requests_total", 1),
    "totals.prompt_tokens":    ("prompt_tokens_total", 1),
    "totals.output_tokens":    ("output_tokens_total", 1),
    "totals.reused":           ("cached_prompt_tokens_total", 1),
    "totals.drafts_offered":   ("speculative_drafts_offered_total", 1),
    "totals.drafts_accepted":  ("speculative_drafts_accepted_total", 1),
    "totals.prompt_ms":        ("prompt_seconds_total", 0.001),
    "totals.decode_ms":        ("decode_seconds_total", 0.001),
}

HELP = {
    "gpu_utilization_ratio": "GPU utilisation, 0..1.",
    "gpu_memory_used_bytes": "GPU memory in use, from Strata's own report.",
    "gpu_memory_total_bytes": "GPU memory total.",
    "gpu_temperature_celsius": "GPU temperature.",
    "gpu_power_watts": "GPU power draw.",
    "gpu_power_limit_watts": "GPU power limit.",
    "gpu_pcie_generation": "Current PCIe link generation.",
    "gpu_pcie_generation_max": "Maximum PCIe generation the link supports.",
    "gpu_pcie_width_lanes": "PCIe link width in lanes.",
    "gpu_pcie_rx_bytes": "PCIe receive throughput.",
    "gpu_pcie_tx_bytes": "PCIe transmit throughput.",
    "cpu_utilization_ratio": "CPU utilisation, 0..1.",
    "ram_used_bytes": "Host RAM in use.",
    "ram_total_bytes": "Host RAM total.",
    "disk_read_bytes": "Disk read throughput.",
    "disk_write_bytes": "Disk write throughput.",
    "tokens_per_second": "Instantaneous output token rate.",
    "tokens_per_second_mean": "Mean output token rate over the server's window.",
    "prefill_tokens_per_second_mean": "Mean prefill token rate over the server's window.",
    "cache_prompt_tokens": "Prompt tokens held in the conversation cache.",
    "cache_last_reused_tokens": "Prompt tokens reused on the most recent request.",
    "cache_last_prompt_tokens": "Prompt tokens on the most recent request.",
    "requests_kept": "Requests kept in the server's recent window.",
    "requests_total": "Requests served since the server started.",
    "prompt_tokens_total": "Prompt tokens processed since the server started.",
    "output_tokens_total": "Output tokens generated since the server started.",
    "cached_prompt_tokens_total": "Prompt tokens served from cache since the server started.",
    "speculative_drafts_offered_total": "Speculative drafts offered since the server started.",
    "speculative_drafts_accepted_total": "Speculative drafts accepted since the server started.",
    "prompt_seconds_total": "Cumulative prompt (prefill) time.",
    "decode_seconds_total": "Cumulative decode time.",
    "up": "1 when the Strata metrics endpoint answered, 0 when it did not.",
    "scrape_ok": "1 when this scrape produced values, 0 when it did not.",
    "scrape_duration_seconds": "Time spent fetching Strata's metrics.",
    "totals_since_timestamp_seconds": "Unix time at which the cumulative counters above began.",
    "info": "Static facts about the node, as labels.",
}

# Static facts become one info-style series with labels, rather than a dozen
# gauges that are always 1 and answer no question a filter could not.
INFO_LABELS = ("hardware_static.gpu_name", "hardware_static.cpu_name",
               "hardware_static.gpu_count", "hardware_static.cores",
               "hardware_static.threads")


def read_key(path: str) -> str:
    """The key, or "".  Never logged, never in argv, never in an error body."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("STRATA_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def dig(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def escape_label(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(doc: dict | None, err: str, elapsed: float) -> bytes:
    out: list[str] = []

    def emit(name, kind, value, labels=""):
        out.append("# HELP %s %s" % (name, HELP.get(name.split(PREFIX + "_", 1)[-1], name)))
        out.append("# TYPE %s %s" % (name, kind))
        out.append("%s%s %s" % (name, labels, value))

    ok = doc is not None
    emit(PREFIX + "_up", "gauge", 1 if ok else 0)
    emit(PREFIX + "_scrape_ok", "gauge", 1 if ok else 0)
    emit(PREFIX + "_scrape_duration_seconds", "gauge", "%.4f" % elapsed)

    if not ok:
        # The reason goes in a comment, not in a label: a label whose value is
        # an error string creates a new time series for every distinct message,
        # and the cardinality grows without bound exactly when things are going
        # wrong.  A comment is readable in `curl` and costs nothing in storage.
        out.append("# scrape failed: %s" % err.replace("\n", " ")[:300])
        return ("\n".join(out) + "\n").encode("utf-8")

    for dotted, (suffix, scale) in sorted(GAUGES.items()):
        v = dig(doc, dotted)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            emit("%s_%s" % (PREFIX, suffix), "gauge", _num(v * scale))

    for dotted, (suffix, scale) in sorted(COUNTERS.items()):
        v = dig(doc, dotted)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            emit("%s_%s" % (PREFIX, suffix), "counter", _num(v * scale))

    since = dig(doc, "totals.since")
    if isinstance(since, (int, float)):
        emit(PREFIX + "_totals_since_timestamp_seconds", "gauge", _num(since))

    labels = []
    for dotted in INFO_LABELS:
        v = dig(doc, dotted)
        if v is not None:
            labels.append('%s="%s"' % (dotted.split(".")[-1], escape_label(v)))
    if labels:
        emit(PREFIX + "_info", "gauge", 1, "{" + ",".join(labels) + "}")
    return ("\n".join(out) + "\n").encode("utf-8")


def _num(v) -> str:
    """Render without exponent notation where the value is a plain integer.

    Prometheus parses `1e+06` fine, but it is unreadable in the exposition
    endpoint that people curl when they are debugging, and reads as a float
    where the value is conceptually a count.
    """
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


class State:
    def __init__(self, base: str, keyfile: str, timeout: float) -> None:
        self.base, self.keyfile, self.timeout = base.rstrip("/"), keyfile, timeout

    def scrape(self) -> tuple[dict | None, str, float]:
        t0 = time.time()
        key = read_key(self.keyfile)
        if not key:
            return None, "no API key readable at %s" % self.keyfile, time.time() - t0
        req = urllib.request.Request(self.base + "/metrics", method="GET")
        req.add_header("Authorization", "Bearer" + " " + key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                doc = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return None, "HTTP %s from %s" % (e.code, self.base), time.time() - t0
        except Exception as e:  # noqa: BLE001
            return None, "%s: %s" % (type(e).__name__, e), time.time() - t0
        if not isinstance(doc, dict):
            return None, "the endpoint answered with %s, not an object" % type(doc).__name__, time.time() - t0
        return doc, "", time.time() - t0


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "llmlab-metrics-adapter/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            # Prometheus scrapes every 30 seconds forever.  A line per scrape is
            # noise nobody reads, and the interesting events are already exposed
            # as metrics.  Failures are the exception and are worth a line.
            if " 200 " not in (fmt % args):
                sys.stderr.write("[adapter] %s\n" % (fmt % args))

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("", "/metrics"):
                doc, err, el = state.scrape()
                return self._send(200, render(doc, err, el),
                                  "text/plain; version=0.0.4; charset=utf-8")
            if self.path == "/healthz":
                return self._send(200, json.dumps({"ok": True, "version": VERSION}).encode(),
                                  "application/json")
            return self._send(404, b"not found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--listen", default="localhost:9101")
    ap.add_argument("--strata", default="http://localhost:8080")
    ap.add_argument("--key-file", default="{{LAB_ROOT}}/services/secrets/strata-key.env")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)

    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        print("[adapter] --listen must be host:port, got %r" % args.listen, file=sys.stderr)
        return 2
    if host not in ("localhost", "localhost", "::1"):
        # Same reason the portal refuses: this exposes node utilisation and,
        # with a key that has broader rights than this process needs, whatever
        # that key can reach.  Public access is the proxy's job, not ours.
        print("[adapter] REFUSING to bind %s: bind localhost and put the "
              "reverse proxy in front." % host, file=sys.stderr)
        return 2

    state = State(args.strata, args.key_file, args.timeout)
    print("[adapter] %s listening on http://%s, translating %s/metrics"
          % (VERSION, args.listen, args.strata), flush=True)
    print("[adapter] key file: %s (read per scrape; never logged)" % args.key_file, flush=True)
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(state))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
