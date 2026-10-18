"""Per-user API keys: issue, look up, disable, revoke.

WHAT IS STORED.  A digest, never the key.  The key is handed to the operator
once, at creation, and after that the store cannot reproduce it -- a backup of
this file is not a backup of the credentials, which is the property that makes
it safe to keep beside the data it belongs to.

WHY SHA-256 AND NOT BCRYPT.  A slow KDF exists to make *guessing* expensive,
and guessing only pays when the secret space is small enough to search -- a
human password.  These keys are `secrets.token_urlsafe(32)`, i.e. 256 bits from
the OS CSPRNG; there is nothing to guess and a KDF would buy no security while
charging every request a few hundred milliseconds.  The plan hashed a password
in it and borrowed its reasoning; this borrows only what applies.

WHY THE COMPARISON IS CONSTANT-TIME.  A digest lookup that returns early on the
first differing byte leaks, through timing, how much of a guess was right.  It
is a weak leak against a 256-bit secret and a free one to close, so it is
closed: every record is compared, and `hmac.compare_digest` does the comparing.

WHY A LOOKUP WALKS EVERY RECORD.  It is the honest shape at this size -- tens
of keys, not millions -- and it is the shape that cannot be got wrong: an index
keyed by a prefix is a second structure to keep consistent, and a lookup that
returns the first match is a lookup whose timing depends on where in the file
the key happens to sit.

TWO PROCESSES WRITE THIS FILE, AND THAT IS THE HARD PART.
--------------------------------------------------------
The gateway (`gateway.py`) holds the store in memory and periodically persists
usage counters; the admin CLI (`admin.py`) is a *separate process* that issues,
disables and revokes.  A naive "load once, write the whole file later" store
loses exactly the thing that matters: the CLI revokes a key, the gateway's next
periodic flush writes its own stale copy back over the file, and the revocation
silently disappears.  The failure is invisible -- no error, no log line -- and
it is discovered when the key that was supposed to be dead keeps working.

So the file is treated as the authority and memory as a cache:

  * **Before reading or writing, check the file's stamp** (mtime_ns, size).  If
    it moved, re-read it.  A revocation by the CLI is therefore live on the
    next request, with no restart and no signal.
  * **Merge on re-read, do not adopt.**  The file wins on everything a human
    decided -- existence, label, note, disabled, revoked.  Memory wins on the
    two fields only this gateway writes -- `requests` and `last_used_at` -- so a
    flush never erases a decision, and a decision never erases a count.
  * **Never clobber what could not be read.**  If the file changed under us and
    can no longer be parsed, the flush is *skipped* rather than written.  A
    store that cannot be read is not an empty store, and writing the stale copy
    over it would turn a five-second permissions problem into permanent loss.

That is a read-modify-write, not a lock, so two processes can still interleave.
The first version of this file said decisions could not be lost because "they
are never written by the side that merges" -- which is wrong, and the self test
caught it.  The merging side writes the *whole file*, so this sequence loses a
revocation outright:

    gateway: stat -> unchanged -> build the file -> .......... -> rename
    admin:                             revoke, rename

The gateway read before the decision landed and wrote after it, so its stale
copy -- revocation absent -- becomes the file.  Nothing logs it; the discovery
is that a key which was supposed to be dead keeps working.

CORRECTION: the write is now a compare-and-swap on the file stamp, taken again
immediately before the rename.  That does not make the sequence above
impossible, it makes it require the interleaving to fall inside the rename
itself rather than inside an `fsync` -- thousands of times narrower, and the
window no longer scales with how long the write takes.  A writer that loses the
race *abandons* its write instead of forcing it: the flusher stays dirty and
picks the change up on the next tick, and the CLI retries from a fresh read.
The residual window is documented rather than claimed away; a cross-process lock
file would close it, and would bring its own staleness handling and its own way
of failing, which is not a trade worth making for counters written every few
seconds by one process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time

# The prefix is a courtesy to whoever finds a key in a log, a config or a
# pastebin: it says what the string is and where to revoke it.  It is checked
# on the way in so that a key from another system cannot be presented here and
# answered with "unknown" when the real answer is "wrong service".
KEY_PREFIX = "llmlab_"

# 32 bytes of urlsafe base64 is 43 characters and 256 bits.  Stated as the
# number of bytes rather than the length of the string, because the length is a
# consequence and the entropy is the requirement.
KEY_BYTES = 32

# The fields only the gateway writes.  Everything else in a record belongs to
# whoever ran the CLI, and a re-read takes the file's value for it.
#
# The counters are split rather than kept as one total because the interesting
# question is never "how many calls" -- it is "how many worked", "how many
# failed", and "how often was this system told to slow down".  A single total
# answers none of those, and reconstructing them later from a log means reading
# the log, which is the thing an operator opens this file to avoid.
_MINE = ("requests", "requests_ok", "requests_failed",
         "requests_rate_limited", "last_used_at")


def _now() -> str:
    """UTC, second precision, ISO-8601 with a Z.

    Second precision on purpose: this timestamp answers "when was it last
    used", and a field that sometimes carries microseconds is a field whose
    diffs are one record wide for no reason but formatting.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Moved(Exception):
    """The store changed between our read and our write.

    Raised instead of writing, never instead of reading: the whole point is
    that a writer which has lost the race must abandon its stale copy rather
    than publish it over somebody else's decision.
    """


def digest_of(key: str) -> str:
    """The digest a presented key is looked up by.  Hex, so it is greppable."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_one_of_ours(key: str) -> bool:
    """Whether a presented key even claims to be from here.

    Deliberately NOT an authorisation decision -- a wrong prefix is still
    rejected by the lookup.  It exists so the gateway can answer "this is not
    one of our keys" differently from "this key was revoked", which is the
    difference between an operator looking in the wrong place and an operator
    looking in the right one.
    """
    return isinstance(key, str) and key.startswith(KEY_PREFIX)


class KeyStore:
    """A JSON file of key records, guarded by one lock.

    One lock, not one per record: every operation here is a read-modify-write
    of the whole file, so finer locking would only create the appearance of
    concurrency while the writer serialised anyway.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._dirty = False
        # The stamp of the version we are holding.  `None` means "no file", but
        # it is also what `os.stat` returns for a file that has just gone
        # missing -- so "stamp is None" is only ever compared, never trusted on
        # its own.  See `_refresh_locked`.
        self._stamp: tuple[int, int] | None = self._stat_key()
        self._load()

    # -- the file ---------------------------------------------------------
    def _stat_key(self) -> tuple[int, int] | None:
        """Identify the file version cheaply, without reading it.

        mtime_ns rather than mtime: two writes inside the same second are
        ordinary here (issue then revoke), and a stamp with second resolution
        would call the second one "no change".
        """
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _read_file(self) -> list[dict] | None:
        """Parse the store, or None if it cannot be read or understood *now*.

        None is not an empty list, and the difference is the whole point: an
        unreadable store must never be mistaken for a store with no keys in it.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return None
        records = doc.get("keys") if isinstance(doc, dict) else None
        if not isinstance(records, list):
            return None
        return records

    def _load(self) -> None:
        records = self._read_file()
        if records is None:
            # Startup is the one place where refusing is strictly better than
            # continuing: a process that comes up with an empty view of a store
            # it could not read will answer 401 to every colleague and point
            # every investigation at the clients.
            raise RuntimeError("cannot read the key store at %s" % self.path)
        self._records = records
        self._stamp = self._stat_key()

    def _merge_locked(self, fresh: list[dict]) -> None:
        """Fold our counters into a freshly-read list, in place."""
        mine = {r.get("digest"): r for r in self._records if r.get("digest")}
        for rec in fresh:
            old = mine.get(rec.get("digest"))
            if not old:
                continue
            for field in ("requests", "requests_ok", "requests_failed",
                          "requests_rate_limited"):
                if int(old.get(field) or 0) > int(rec.get(field) or 0):
                    rec[field] = old.get(field)
            # ISO-8601 with a fixed format and a Z sorts lexicographically, so
            # the string comparison is a time comparison.
            if str(old.get("last_used_at") or "") > str(rec.get("last_used_at") or ""):
                rec["last_used_at"] = old.get("last_used_at")

    def _refresh_locked(self) -> bool:
        """Re-read the file if it moved.  True if our view is current.

        False means "it changed and I could not understand the new version";
        every writer must treat that as "do not write", and every reader as
        "answer from what I already have".
        """
        stamp = self._stat_key()
        if stamp == self._stamp:
            return True
        if stamp is None:
            # The file existed a moment ago and now does not.  That is not an
            # empty store, it is a store someone is mid-way through replacing,
            # and adopting "[]" here would delete every key in memory.
            print("[keystore] %s disappeared; keeping the loaded records"
                  % self.path, file=sys.stderr, flush=True)
            self._stamp = None
            return False
        fresh = self._read_file()
        if fresh is None:
            print("[keystore] %s changed but cannot be read; not re-reading it"
                  % self.path, file=sys.stderr, flush=True)
            return False
        self._merge_locked(fresh)
        self._records = fresh
        self._stamp = stamp
        return True

    def _save_locked(self) -> None:
        """Write the whole file atomically, 0600, if nobody else got there first.

        `os.replace` is the reason a reader never sees a half-written store;
        the mode is set on the temporary file before the rename so there is no
        instant at which the final path exists with wider permissions.

        The stamp is taken twice: once before the body is built and once
        immediately before the rename.  If it moved in between, this raises
        `Moved` and deletes its own temporary file.  That second read is what
        keeps a background counter flush from erasing a revocation written
        while the flush was busy -- see the module docstring, where that bug is
        written down.
        """
        expected = self._stat_key()
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # `mkstemp`, not a name built from the pid: two stores in one process
        # (which is what the self test is, and what a future "reload" would be)
        # would otherwise pick the same temporary path and rename each other's
        # half-written file into place.  It also opens 0600, which is the mode
        # this file must have.
        fd, tmp = tempfile.mkstemp(dir=directory,
                                   prefix=os.path.basename(self.path) + ".tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "keys": self._records}, fh,
                          indent=2, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            if self._stat_key() != expected:
                raise Moved(self.path)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._stamp = self._stat_key()
        self._dirty = False

    def _commit_locked(self, mutate):
        """Refresh, apply `mutate`, write -- retrying if we lost the race.

        `mutate` must only touch `self._records`, because a retry starts by
        replacing that list with a fresh read; anything it kept elsewhere would
        be applied twice.
        """
        for _ in range(5):
            if not self._refresh_locked():
                raise RuntimeError("refusing to write: the store at %s changed "
                                   "and cannot be read" % self.path)
            result = mutate()
            try:
                self._save_locked()
                return result
            except Moved:
                continue
        raise RuntimeError("the key store at %s is being written by something "
                           "else; try again" % self.path)

    def flush(self) -> None:
        """Persist pending last-used / counter changes, if any.

        Losing the race here is not retried: a counter that lands five seconds
        later is indistinguishable from one that landed now, and the entry stays
        dirty so the next tick picks it up.  Forcing the write through would be
        exactly the mistake this method is shaped to avoid.
        """
        with self._lock:
            if not self._dirty:
                return
            if not self._refresh_locked():
                # The file moved and we cannot read it.  Writing our copy now
                # would be a write that deletes whatever we could not see.
                return
            try:
                self._save_locked()
            except Moved:
                return

    # -- lifecycle -------------------------------------------------------
    def issue(self, label: str, note: str = "") -> tuple[str, dict]:
        """Create a key.  Returns (plaintext, record) -- plaintext ONCE.

        The caller is responsible for showing it to exactly one person and
        then forgetting it.  Nothing here logs it, and the record that is
        written does not contain it.
        """
        label = (label or "").strip()
        if not label:
            raise ValueError("a key needs a label: it is the only thing that "
                             "says who to ask when the key is the problem")
        key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)

        def make() -> dict:
            # 8 hex characters of the digest make an identifier that is stable,
            # short enough to read aloud, and does not reveal the key.  Collisions
            # are resolved by extending until unique, rather than assumed away.
            ident = "k_" + digest_of(key)[:8]
            taken = {r.get("id") for r in self._records}
            n = 8
            while ident in taken:
                n += 2
                ident = "k_" + digest_of(key)[:n]
            record = {
                "id": ident,
                "label": label,
                "note": note or "",
                "digest": digest_of(key),
                "created_at": _now(),
                "disabled": False,
                "revoked": False,
                "last_used_at": None,
                "requests": 0,
                "requests_ok": 0,
                "requests_failed": 0,
                "requests_rate_limited": 0,
            }
            self._records.append(record)
            return dict(record)

        with self._lock:
            return key, self._commit_locked(make)

    def _find_locked(self, value: str) -> dict | None:
        """The record whose digest matches, or None.

        The key is looked up by value, and the identifier by value too, so
        that `llmlab_...` can never collide with `k_...` -- the prefix check
        keeps the two namespaces apart before the comparison happens.
        """
        if looks_like_one_of_ours(value):
            want = digest_of(value)
        elif isinstance(value, str) and value.startswith("k_"):
            want = None
        else:
            return None
        found = None
        raw = want.encode() if want else b""
        for rec in self._records:
            if want is not None:
                if hmac.compare_digest(raw, str(rec.get("digest", "")).encode()):
                    found = rec
            else:
                if hmac.compare_digest(value.encode(), str(rec.get("id", "")).encode()):
                    found = rec
        return found

    def authenticate(self, key: str) -> tuple[dict | None, str]:
        """Resolve a presented key to its record.

        Returns (record, reason) where reason is one of: "ok", "unknown",
        "disabled", "revoked".  A revoked or disabled key returns the record
        so the caller can name it in the refusal -- "your key was revoked" is
        actionable in a way that "401" is not.

        This is where the CLI's decisions take effect: the stamp check costs
        one `stat`, and it is what makes `admin.py revoke` live immediately
        rather than at the next gateway restart.  A revocation that needs a
        restart is a revocation that will not happen during the incident it is
        for.
        """
        if not isinstance(key, str) or not key:
            return None, "unknown"
        with self._lock:
            self._refresh_locked()
            rec = self._find_locked(key)
            if rec is None:
                return None, "unknown"
            if rec.get("revoked"):
                return dict(rec), "revoked"
            if rec.get("disabled"):
                return dict(rec), "disabled"
            return dict(rec), "ok"

    def touch(self, ident: str) -> None:
        """Record that an authorised key was used.

        In memory, and persisted lazily by `flush()`.  Writing the file on
        every request would make the store a per-request fsync, which is a
        cost the model call would pay for a timestamp nobody reads that
        quickly -- and on a busy box it would be the gateway, not the model,
        that fell over.
        """
        with self._lock:
            for rec in self._records:
                if rec.get("id") == ident:
                    rec["last_used_at"] = _now()
                    rec["requests"] = int(rec.get("requests") or 0) + 1
                    self._dirty = True
                    return

    _OUTCOMES = {"ok": "requests_ok", "failed": "requests_failed",
                 "rate_limited": "requests_rate_limited"}

    def record(self, ident: str, outcome: str) -> None:
        """Attribute one finished request to a key.

        Called after the fact rather than at admission, because at admission the
        outcome is not yet known -- and a counter that counts things that have
        not happened is how "success rate" ends up pinned at 100%.

        "cancelled" -- a caller who hung up mid-answer -- deliberately has no
        counter.  It is in `requests` (via `touch`) and in the usage log, and
        there is no per-key field for it because "how often did this system
        abandon a request" is a question about a client-side bug, not a quota.
        It must still be *accepted* here: the first version raised ValueError on
        any outcome it did not recognise, which meant every disconnect left a
        traceback in the log and lost the request's counters with it.
        """
        field = self._OUTCOMES.get(outcome)
        if field is None:
            if outcome == "cancelled":
                return
            raise ValueError("unknown outcome %r" % (outcome,))
        with self._lock:
            for rec in self._records:
                if rec.get("id") == ident:
                    rec[field] = int(rec.get(field) or 0) + 1
                    self._dirty = True
                    return

    def set_state(self, ident: str, *, disabled: bool | None = None,
                  revoked: bool | None = None) -> dict:
        """Disable / enable / revoke by identifier.  Returns the new record.

        Disable and revoke are separate on purpose.  Disable is "stop, and I
        may turn it back on"; revoke is "this key is finished and the record
        should say so forever".  Collapsing them loses the answer to "was this
        turned off on purpose, and is it coming back?" -- which is the first
        question asked during an incident.
        """
        def change() -> dict:
            rec = self._find_locked(ident)
            if rec is None:
                raise KeyError(ident)
            if disabled is not None:
                rec["disabled"] = bool(disabled)
            if revoked is not None:
                rec["revoked"] = bool(revoked)
                if revoked:
                    # A revoked key is not a disabled one: it must not be
                    # possible to re-enable it by accident.
                    rec["disabled"] = True
            return dict(rec)

        with self._lock:
            return self._commit_locked(change)

    def list_records(self, include_revoked: bool = True) -> list[dict]:
        """Every record, digest stripped.

        The digest is the one field that must never leave this module: it is
        not the key, but it is the thing a key is checked against, and a list
        endpoint that hands it out turns a read-only view into a lookup table.
        """
        with self._lock:
            self._refresh_locked()
            out = []
            for rec in self._records:
                if not include_revoked and rec.get("revoked"):
                    continue
                out.append({k: v for k, v in rec.items() if k != "digest"})
            return out

    def get(self, ident: str) -> dict | None:
        with self._lock:
            self._refresh_locked()
            rec = self._find_locked(ident)
            if rec is None:
                return None
            return {k: v for k, v in rec.items() if k != "digest"}
