"""What the gateway records, and -- more carefully -- what it refuses to.

THE PRIVACY RULE IS THE DESIGN CONSTRAINT, NOT AN AFTERTHOUGHT.  The first
consumer of this gateway is an HR system, so the request bodies that pass
through it are CVs: names, phone numbers, employers, sometimes health or family
details.  A gateway that logs bodies "for debugging" turns itself into an
unmanaged copy of the personal data of every candidate the company ever screens
-- in a file nobody classified, on a box whose backups nobody thought about.

So the default is: **no request body and no response body is ever written
anywhere.**  What is recorded is the *shape* of the traffic -- which key, which
model, how long, how big, what status -- which is exactly what an operator needs
to answer "who is using the box and is it healthy", and which contains nothing
about any person.

Three artefacts, three different lifetimes:

  * `usage.jsonl` -- one line per request, append-only, rotated by size.  This
    is the record an operator reads to attribute load to a system.
  * `status.json` -- one document, rewritten every few seconds, holding the
    live picture the console displays (limits, queue depth, totals, per-key
    counters).  It is derived, not authoritative: lose it and nothing is lost.
  * the key store -- authoritative, and owned by `keystore.py`, not here.

TOKEN COUNTS ARE PASSED THROUGH OR ABSENT, NEVER INVENTED.  The brief is
explicit that a fabricated usage figure is worse than no figure, and it is
right: an estimated token count that looks exact gets quoted in a monthly
report as if it were measured.  So `usage` here is the upstream's own object,
verbatim, or `null` when the upstream did not send one.  Nothing multiplies a
character count by a constant and calls it usage.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Meters:
    """Running counters and latency sums, under one lock.

    Sums and a count rather than a list of samples: the question the console
    asks is "what is the average latency", and keeping every sample to answer it
    turns a monitor into a memory leak with a graph attached.  The averages are
    labelled as averages of what was measured, with the sample count beside
    them, so nobody mistakes them for percentiles.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = _now()
        self._t0 = time.monotonic()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.requests = 0
            self.ok = 0
            self.failed = 0
            self.cancelled = 0
            self.rate_limited = 0
            self.rejected = 0          # 401 / 404 / 413 / 411, never forwarded
            self.bytes_out = 0
            self.queue_ms_sum = 0.0
            self.ttft_ms_sum = 0.0
            self.upstream_ms_sum = 0.0
            self.total_ms_sum = 0.0
            self.latency_samples = 0
            self.errors: dict[str, int] = {}
            self.status_counts: dict[str, int] = {}
            # Tokens, and requests, attributed to the *key* that spent them.
            # The brief asks for consumption "by Gateway Key", not by IP or by
            # session, and the key is the only thing at this door that stands
            # for a person.  Kept here and not in the key store because it is a
            # count of what the upstream reported, not a decision anybody made;
            # the store's per-key request counters remain the durable record.
            self.tokens_by_key: dict[str, dict] = {}
            self.models: list | None = None
            self.upstream_ok: bool | None = None
            self.upstream_status: int | None = None
            self.upstream_checked_at: str | None = None
            self.upstream_error: str | None = None

    def count_request(self, outcome: str, status: int, *, queue_ms: float,
                      ttft_ms: float, upstream_ms: float, total_ms: float,
                      bytes_out: int, error: str | None = None) -> None:
        """Tally one forwarded request.

        `cancelled` is a category of its own, and deliberately not folded into
        `failed`.  A caller who stops reading a stream has not been failed by
        anybody -- but counting it as a failure makes the success rate fall
        whenever somebody closes a tab, which teaches the reader to ignore the
        number.  It is still counted in `requests`, so the three categories do
        not have to add up and the difference is visible rather than hidden.
        """
        with self._lock:
            self.requests += 1
            key = "%dxx" % (status // 100)
            self.status_counts[key] = self.status_counts.get(key, 0) + 1
            if outcome in ("ok", "failed", "cancelled"):
                setattr(self, outcome, getattr(self, outcome) + 1)
            self.bytes_out += max(0, bytes_out)
            self.queue_ms_sum += queue_ms
            self.ttft_ms_sum += ttft_ms
            self.upstream_ms_sum += upstream_ms
            self.total_ms_sum += total_ms
            self.latency_samples += 1
            if error:
                self.errors[error] = self.errors.get(error, 0) + 1

    def note_tokens(self, key_id: str, usage) -> None:
        """Add one upstream-reported `usage` block to a key's totals.

        Only integers the upstream actually sent are added.  A missing block,
        a string, a null or a float from a chatty compatibility layer is
        ignored rather than coerced, because the alternative -- estimating
        tokens from the text -- is a number that looks like a measurement and
        is not.  The count of keys that have *any* totals is published beside
        the totals for the same reason: a per-key figure whose key was never
        seen reporting usage should read as absent, not as zero.
        """
        if not isinstance(usage, dict) or not key_id:
            return
        fields = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                fields[name] = value
        if not fields:
            return
        with self._lock:
            row = self.tokens_by_key.setdefault(
                key_id, {"prompt_tokens": 0, "completion_tokens": 0,
                         "total_tokens": 0, "reports": 0})
            for name, value in fields.items():
                row[name] += value
            row["reports"] += 1

    def count_rate_limited(self) -> None:
        with self._lock:
            self.rate_limited += 1

    def count_rejected(self, reason: str) -> None:
        """A request refused before it was forwarded: a bad key, a route that is
        not published, an oversized body.  Counted separately from upstream
        failures because they are a different kind of event -- one is somebody
        misconfigured, the other is the model service."""
        with self._lock:
            self.rejected += 1
            self.errors[reason] = self.errors.get(reason, 0) + 1

    def note_upstream(self, ok: bool, status: int | None,
                      error: str | None = None, models: list | None = None) -> None:
        with self._lock:
            self.upstream_ok = ok
            self.upstream_status = status
            self.upstream_error = error
            self.upstream_checked_at = _now()
            if models is not None:
                self.models = models

    def snapshot(self) -> dict:
        with self._lock:
            n = self.latency_samples

            def avg(total: float):
                return round(total / n, 1) if n else None

            return {
                "requests": self.requests,
                "ok": self.ok,
                "failed": self.failed,
                "cancelled": self.cancelled,
                "rate_limited": self.rate_limited,
                "rejected": self.rejected,
                "bytes_out": self.bytes_out,
                "tokens_by_key": {k: dict(v) for k, v in self.tokens_by_key.items()},
                "status_counts": dict(self.status_counts),
                "errors": dict(self.errors),
                "latency_ms": {
                    "samples": n,
                    "avg_queue": avg(self.queue_ms_sum),
                    "avg_ttft": avg(self.ttft_ms_sum),
                    "avg_upstream": avg(self.upstream_ms_sum),
                    "avg_total": avg(self.total_ms_sum),
                },
                "upstream": {
                    "ok": self.upstream_ok,
                    "status": self.upstream_status,
                    "checked_at": self.upstream_checked_at,
                    "error": self.upstream_error,
                },
                "models": self.models,
            }

    def uptime_s(self) -> int:
        return int(time.monotonic() - self._t0)


class UsageLog:
    """One JSON line per request, with the body deliberately absent.

    Rotated by size to a single previous generation.  An append-only log on an
    unattended box is a disk that fills up on a schedule nobody wrote down, and
    the failure arrives as "the model stopped answering" -- because the process
    cannot write its own telemetry.
    """

    def __init__(self, path: str, max_bytes: int = 8 * 1024 * 1024):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def write(self, record: dict) -> None:
        """Append one line.  Never raises into the request path: telemetry that
        can fail a request is telemetry that turns a logging problem into an
        outage."""
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self._rotate_if_needed()
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                pass


class StatusFile:
    """The console's view of this process, rewritten whole.

    Written with `os.replace` so a reader never sees half a document.  The
    portal is a separate process and reads this file rather than importing the
    gateway: a console that has to be restarted whenever the gateway changes is
    a console that gets restarted rarely, which is to say that the coupling
    would show up as staleness rather than as an error.
    """

    def __init__(self, path: str):
        self.path = path
        self._last_error: str | None = None

    def write(self, document: dict) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory or ".",
                                       prefix=os.path.basename(self.path) + ".tmp.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(document, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self._last_error = None
        except OSError as exc:
            # Reported once per distinct problem rather than per write: a
            # status write failing every five seconds would otherwise fill the
            # log with the same line and bury whatever else is there.
            message = "%r" % (exc,)
            if message != self._last_error:
                self._last_error = message
                print("[gateway] cannot write the status file: %s" % message,
                      flush=True)
