"""Which registered engine a model name goes to, and on whose authority.

WHY THIS EXISTS.  The gateway was built for one upstream: Strata, holding one
key, reachable at one address.  The lab now has several people who each run
their own engine on this box (a vLLM here, a Strata there), and the question
"let me call carol's model" has two bad answers and one good one.

    * Copying the weights to a second directory is not sharing: it is two
      copies to keep in sync on a GPU that is already full.
    * Starting a second resident model is not sharing either: the card holds
      one, and `parallel` is 1.
    * The good answer is that the *engine stays where it is* and the gateway
      learns to forward to more than one address -- while keeping the property
      that makes it worth having, which is that the caller's identity is
      decided by a server-side key record and not by anything the caller says.

So this module is three things and no more:

  1. **A schema** for a routing snapshot: engines (address + where its secret
     lives) and endpoints (a public model alias, the engine it lives on, the
     private model id that engine expects, an owner, and an ACL).  The snapshot
     is produced elsewhere -- by the portal's registry -- and read here.

  2. **A reader for it that trusts nothing it was not told to.**  The file must
     be where an administrator put it, owned by root or by this process's own
     uid, and not writable by group or other.  If it is missing, malformed,
     ambiguous or untrustworthy, the router loads *nothing*: every alias falls
     back to the single legacy upstream, which is exactly what the gateway did
     before this file existed.  New routing is opt-in and it fails closed.

  3. **An ACL check** whose subject comes from `keystore.KeyStore`, i.e. from
     the key record that was matched by digest during authentication.  A caller
     cannot say who they are: the `model` field is a *name to look up*, never a
     claim of ownership, and the `X-*` headers that might have carried a
     username are destroyed on the way in (see `gateway.py`).

WHAT IS DELIBERATELY NOT HERE.

  * **No DNS.**  An engine address must be an IP literal, loopback by default.
    A hostname would be resolved at connect time, which is the shape DNS
    rebinding needs: the name checks out when the snapshot is validated and
    points somewhere else when the socket is opened.  An allowlist of literal
    addresses that the administrator passes on the command line is the only way
    to reach a non-loopback engine, and the snapshot cannot widen it -- a
    registry that could add an address could exfiltrate the lab's traffic.
  * **No redirects.**  An engine that answers 3xx is refused, not followed and
    not relayed.  `http.client` would not follow one either, but "we do not
    follow it" is weaker than "a redirect off an engine is an error".
  * **No per-user GPU share.**  The engine API does not report what a request
    cost in VRAM, and the card is shared, so any number here would be invented.
    See `Gateway.resource_status`.
  * **No `file://` or arbitrary URL.**  Two transports exist: `http` on a
    loopback (or administrator-allowlisted) IP literal, and `unix` on a socket
    path.  Anything else fails to parse, which is a refusal rather than a code
    path that has to remember to refuse.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import stat
import threading
import time

from .keystore import normalise_principal

# Stated in the file so that a snapshot written against a later, incompatible
# shape is refused by name rather than half-understood.
SCHEMA = "llmlab.endpoint-routing/1"

# An endpoint is in exactly one of these states.  "revoked" is not "suspended":
# withdrawing a share is a decision about other people, and the record keeps
# saying so.
STATES = ("active", "suspended", "revoked")

# The three ACL kinds the brief names.  `private` is the default in the sense
# that it is what an endpoint gets if it is written without an `acl` block --
# the omit-means-share-with-everyone mistake is not available.
ACL_KINDS = ("private", "users", "all")

# Why a request was refused, as a stable string.  These reach the usage log and
# the client, and they are the vocabulary an operator greps for.
NO_PRINCIPAL = "principal_unbound"
NOT_SHARED = "not_shared_with_you"
REVOKED = "endpoint_revoked"
SUSPENDED = "endpoint_suspended"
ENGINE_UNUSABLE = "engine_unusable"


class SnapshotError(Exception):
    """The snapshot cannot be used.  Always caught, never fatal to the process."""


# ---------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------
def parse_allowed_addresses(values):
    """Command-line addresses, as networks.

    A bare address is a /32 or /128; a CIDR is taken as written.  Hostnames are
    refused here for the same reason they are refused in the snapshot: the only
    name that would resolve is one an attacker could also change.
    """
    out = []
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise SystemExit(
                "--allow-upstream-address wants an IP literal or CIDR, got %r "
                "(hostnames are refused: a name can be re-pointed after the "
                "snapshot was approved)" % (raw,))
        out.append(net)
    return out


def address_allowed(host: str, allowed) -> tuple[bool, str]:
    """Whether this literal address may host an engine."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, ("%r is not an IP literal; engine addresses must be "
                       "literals so that nothing is resolved at connect time"
                       % (host,))
    if ip.is_loopback:
        return True, ""
    for net in allowed:
        if ip.version == net.version and ip in net:
            return True, ""
    return False, ("%s is neither loopback nor on the operator's allowlist "
                   "(--allow-upstream-address)" % (host,))


class _UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client` over an `AF_UNIX` socket.

    A local engine that has no port is a normal shape here, and it is the one
    that cannot be reached from off the box at all.  The `Host` header is still
    sent (something has to go on the request line's authority), but the
    connection goes to the socket path and nothing is resolved.
    """

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._unix_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


# ---------------------------------------------------------------------------
# engines and endpoints
# ---------------------------------------------------------------------------
class Engine:
    """One registered engine: where it is, and the secret this gateway calls it with.

    The secret is read once, at load, and lives in this object.  It is never
    written to the status file (which reports `secret_present` instead), never
    put in a log line and never sent to a caller -- the same rule the single
    Strata key has always followed, now per engine, so that "share my model"
    does not mean "hand over my engine's credential".
    """

    def __init__(self, engine_id: str, transport: str, target, secret: str,
                 models_path: str = "/v1/models", why: str = ""):
        self.engine_id = engine_id
        self.transport = transport            # "http" | "unix"
        self.target = target                  # (host, port) | socket path
        self.secret = secret
        self.models_path = models_path
        self.why = why                        # why it is unusable, if it is
        # Health as last observed.  `None` means "not yet probed", which is not
        # the same as "up" and must not be shown as if it were.
        self.healthy: bool | None = None
        self.status: int | None = None
        self.checked_at: str | None = None
        self.error: str | None = None

    @property
    def secret_present(self) -> bool:
        return bool(self.secret)

    @property
    def usable(self) -> bool:
        return self.secret_present and not self.why

    @property
    def address(self) -> str:
        if self.transport == "unix":
            return "unix:%s" % (self.target,)
        return "%s:%d" % self.target

    def connect(self, timeout: float):
        if self.transport == "unix":
            return _UnixHTTPConnection(self.target, timeout)
        host, port = self.target
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def public(self) -> dict:
        """Everything about this engine that is not a secret."""
        return {
            "engine_id": self.engine_id,
            "transport": self.transport,
            "address": self.address,
            "secret_ref_present": self.secret_present,
            "usable": self.usable,
            "why": self.why or None,
            "healthy": self.healthy,
            "status": self.status,
            "checked_at": self.checked_at,
            "error": self.error,
        }


class Endpoint:
    """One model on one engine, and who may call it.

    `owner_uid` is the registry's statement of who owns the model, not the
    caller's.  It is compared against the principal that `KeyStore` produced
    for the presented key; if the two do not match and the ACL does not say
    otherwise, the request is refused.  Neither value in that comparison comes
    from the request.
    """

    def __init__(self, endpoint_id: str, engine_id: str, model_alias: str,
                 model_id: str, owner_uid: str, acl_kind: str,
                 acl_principals=(), state: str = "active", note: str = ""):
        self.endpoint_id = endpoint_id
        self.engine_id = engine_id
        self.model_alias = model_alias
        self.model_id = model_id
        self.owner_uid = owner_uid
        self.acl_kind = acl_kind
        self.acl_principals = frozenset(acl_principals or ())
        self.state = state
        self.note = note

    def allows(self, principal: str | None) -> tuple[bool, str]:
        """(allowed, why not).  The whole authorisation decision, in one place."""
        if self.state == "revoked":
            return False, REVOKED
        if self.state == "suspended":
            return False, SUSPENDED
        if not principal:
            # The key is real and its digest matched -- it just carries no owner
            # binding, so there is nothing server-side to compare against.  The
            # legacy upstream stays available to it; a registered endpoint does
            # not, because the only way to say yes here would be to believe the
            # caller.
            return False, NO_PRINCIPAL
        if principal == self.owner_uid:
            return True, ""
        if self.acl_kind == "all":
            return True, ""
        if self.acl_kind == "users" and principal in self.acl_principals:
            return True, ""
        return False, NOT_SHARED

    def public(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "engine_id": self.engine_id,
            "model_alias": self.model_alias,
            "model_id": self.model_id,
            "owner_uid": self.owner_uid,
            "acl": {"kind": self.acl_kind,
                    "principals": sorted(self.acl_principals)},
            "state": self.state,
            "note": self.note or None,
        }


class Route:
    """A resolved request: this endpoint, on this engine."""

    __slots__ = ("endpoint", "engine")

    def __init__(self, endpoint: Endpoint, engine: Engine):
        self.endpoint = endpoint
        self.engine = engine


class Refusal:
    """A resolved request that will not be forwarded, and why."""

    __slots__ = ("status", "code", "detail", "reason")

    def __init__(self, status: int, code: str, detail: str, reason: str):
        self.status = status
        self.code = code
        self.detail = detail
        self.reason = reason


# WHAT A CALLER IS TOLD, AND WHAT THEY ARE NOT.
#
# The operator-facing reason (`not_shared_with_you`, `endpoint_revoked`, ...)
# travels in `usage.jsonl` and in the router's counters.  It is deliberately NOT
# turned into a sentence for the caller, because "this model exists and is not
# for you" is a different answer from "there is no such name" -- and a caller
# who can tell those apart can find every private alias in the lab by guessing
# at it, one 403 at a time.  An ACL whose subject list is enumerable is not
# much of an ACL.
#
# So an endpoint the caller may not use answers exactly what a name that does
# not exist answers.  The one exception is a key that carries no owner binding
# at all: that is a statement about the *key*, not about any model, it is
# identical for every name the caller tries, and it is the one refusal whose
# whole value is that an operator can read it and go and fix it.  See §7 of
# `docs/dell-migration/phase6-multi-engine-routing.md` for the residual.
NO_SUCH_MODEL = "this gateway does not serve a model by that name"
NO_PRINCIPAL_TEXT = ("this API key carries no owner binding on the server, so "
                     "it cannot be given access to a registered model; ask the "
                     "operator to bind the key to a user")


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------
def _require(mapping, key, where):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError("%s: %r must be a non-empty string" % (where, key))
    return value.strip()


def _unique(values, where):
    seen = set()
    for v in values:
        if v in seen:
            raise SnapshotError("%s: %r appears twice" % (where, v))
        seen.add(v)


def _vet_engine(raw, allowed):
    """One engine block, or a SnapshotError naming what is wrong with it."""
    where = "engine %r" % (raw.get("engine_id"),) if isinstance(raw, dict) else "engine"
    if not isinstance(raw, dict):
        raise SnapshotError("%s: not an object" % where)
    engine_id = _require(raw, "engine_id", where)
    where = "engine %r" % (engine_id,)

    # `kind` is optional, but if it is written it has to be the one protocol
    # this gateway can actually speak.  Ignoring it would mean a snapshot could
    # announce a protocol nothing here implements and still get forwarded.
    kind = raw.get("kind")
    if kind is not None and kind != "openai-compatible":
        raise SnapshotError("%s: kind must be \"openai-compatible\", got %r"
                            % (where, kind))

    up = raw.get("upstream")
    if not isinstance(up, dict):
        raise SnapshotError("%s: needs an `upstream` object" % where)
    transport = up.get("transport")
    if transport == "http":
        host = _require(up, "host", where + " upstream")
        port = up.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise SnapshotError("%s: `port` must be an integer in 1..65535"
                                % (where,))
        ok, why = address_allowed(host, allowed)
        if not ok:
            raise SnapshotError("%s: %s" % (where, why))
        target = (host, port)
    elif transport == "unix":
        path = _require(up, "path", where + " upstream")
        if not os.path.isabs(path):
            raise SnapshotError("%s: the unix socket path must be absolute"
                                % (where,))
        target = path
    else:
        # "file", "ftp", "gopher" and every other scheme land here, as do
        # typos.  There is no code path below that could fetch them.
        raise SnapshotError("%s: transport must be \"http\" or \"unix\", got %r"
                            % (where, transport))

    secret_ref = raw.get("secret_ref")
    if secret_ref is None:
        raise SnapshotError("%s: needs a `secret_ref`" % (where,))
    if not isinstance(secret_ref, dict):
        raise SnapshotError("%s: `secret_ref` must be an object" % (where,))
    if secret_ref.get("value") is not None:
        # An inline secret in the snapshot would be a credential in a file the
        # registry writes, readable by whatever writes it.  Only a reference.
        raise SnapshotError("%s: put the secret in a file and name it with "
                            "`file`+`var`; inline secrets are refused" % (where,))
    ref_file = _require(secret_ref, "file", where + " secret_ref")
    ref_var = _require(secret_ref, "var", where + " secret_ref")
    if not os.path.isabs(ref_file):
        raise SnapshotError("%s: the secret file path must be absolute"
                            % (where,))

    models_path = raw.get("models_path") or "/v1/models"
    # A path, and only a path: it is put on a request line verbatim, so a
    # control character or a space in it is not a typo to be tolerated, it is
    # something `http.client` would refuse to send -- at probe time, in a
    # background thread, which is a poor place to discover a bad snapshot.
    if (not isinstance(models_path, str) or not models_path.startswith("/")
            or any(ch.isspace() or not ch.isprintable() for ch in models_path)):
        raise SnapshotError("%s: `models_path` must be a path with no spaces or "
                            "control characters" % (where,))
    return engine_id, transport, target, ref_file, ref_var, models_path


def _vet_endpoint(raw, engine_ids):
    if not isinstance(raw, dict):
        raise SnapshotError("endpoint: not an object")
    endpoint_id = _require(raw, "endpoint_id", "endpoint")
    where = "endpoint %r" % (endpoint_id,)
    engine_id = _require(raw, "engine_id", where)
    if engine_id not in engine_ids:
        raise SnapshotError("%s: names engine %r, which is not in this snapshot"
                            % (where, engine_id))
    model_alias = _require(raw, "model_alias", where)
    model_id = _require(raw, "model_id", where)
    owner_uid = normalise_principal(raw.get("owner_uid"))
    if not owner_uid:
        raise SnapshotError("%s: `owner_uid` must be a platform user name or "
                            "`uid:<number>`" % (where,))

    acl = raw.get("acl")
    if acl is None:
        acl_kind, acl_principals = "private", ()
    elif not isinstance(acl, dict):
        raise SnapshotError("%s: `acl` must be an object" % (where,))
    else:
        acl_kind = acl.get("kind")
        if acl_kind not in ACL_KINDS:
            raise SnapshotError("%s: acl.kind must be one of %s, got %r"
                                % (where, "/".join(ACL_KINDS), acl_kind))
        raw_principals = acl.get("principals") or []
        if not isinstance(raw_principals, list):
            raise SnapshotError("%s: acl.principals must be a list" % (where,))
        names = []
        for item in raw_principals:
            who = normalise_principal(item)
            if not who:
                raise SnapshotError("%s: acl.principals has %r, which is not a "
                                    "user name or `uid:<number>`" % (where, item))
            names.append(who)
        _unique(names, where + " acl.principals")
        acl_principals = names
        if acl_kind == "private" and acl_principals:
            raise SnapshotError("%s: acl.kind is private but names other people; "
                                "use kind \"users\"" % (where,))

    state = raw.get("state") or "active"
    if state not in STATES:
        raise SnapshotError("%s: state must be one of %s, got %r"
                            % (where, "/".join(STATES), state))
    return Endpoint(endpoint_id, engine_id, model_alias, model_id, owner_uid,
                    acl_kind, acl_principals, state, raw.get("note") or "")


class Router:
    """The loaded snapshot, re-read when the file moves.

    One lock, held for the length of a load.  Requests take a *reference* to the
    current tables under the lock and then read them without it, so a reload
    never blocks a request behind a file read -- and a request in flight keeps
    the view it started with, which is what makes a revocation take effect on
    the *next* request rather than half-way through one.
    """

    def __init__(self, path: str, *, allowed_addresses=(), reserved_aliases=None,
                 key_reader=None):
        self.path = path
        self.allowed = list(allowed_addresses or [])
        # Names the operator already published with `--alias`.  A snapshot that
        # redeclares one is ambiguous, and an ambiguous name is a startup error
        # rather than a silent precedence rule (same reasoning as
        # `gateway.parse_aliases`).
        self.reserved_aliases = dict(reserved_aliases or {})
        self._read_key = key_reader

        self._lock = threading.Lock()
        self._stamp = None
        # The tables are replaced as *one* attribute rather than as two.  A
        # reader takes the reference once and then works from it, so a reload
        # that lands mid-request cannot pair a new engine table with an old
        # endpoint table -- one snapshot is in force for the whole of one
        # decision, which is the property the ACL depends on.
        self._tables: tuple[dict, dict] = ({}, {})
        # NOT called `digest`, and not published under that name either.
        # `node/72-accept-gateway.sh` asserts that `status.json` contains no
        # `"digest"` key at all, because a list of *key* digests there would
        # turn the console's view into a lookup table.  This is the hash of a
        # snapshot file, which is a different thing -- but it arrives in the
        # same document, and a name collision would mean a phrase like "carries
        # no digest" quietly stopping being true.  Renamed rather than
        # weakened: the existing check is worth keeping exactly as strict as it
        # is.
        self.snapshot_sha256: str | None = None
        self.revision = None
        self.generated_at: str | None = None
        self.source: str | None = None
        self.loaded_at: str | None = None
        self.error: str | None = None
        self.rejected: tuple[dict, ...] = ()
        # Counters, so the console can show that a snapshot is *being* used
        # rather than only that one was loaded.
        self.routed = 0
        self.refused = 0
        self.reloads = 0
        self.load()

    @property
    def engines(self) -> dict:
        return self._tables[0]

    @property
    def by_alias(self) -> dict:
        return self._tables[1]

    @property
    def endpoints(self) -> tuple:
        return tuple(self._tables[1].values())

    # -- loading ---------------------------------------------------------
    @staticmethod
    def _trust(path: str) -> tuple[bool, str]:
        """Whether this file is one an administrator put there.

        The rule is the one the key store already lives by: a file that decides
        who may call which model must not be writable by anyone who is not
        allowed to make that decision.  Group- or other-writable is refused
        outright; a different owner is refused too, because a file the gateway's
        own user could rewrite is a file a compromised gateway could rewrite.

        This is the "read-only and trusted" half of the brief's "approved or
        signed, or read-only and trusted".  A detached signature is the other
        half and is not implemented; the digest of what *was* loaded is
        published in the status document, so an operator can tell what is in
        force, and `docs/dell-migration/phase6-multi-engine-routing.md` says
        which of the two this is.
        """
        try:
            info = os.stat(path)
        except OSError as exc:
            return False, "cannot stat %s: %s" % (path, exc)
        if not stat.S_ISREG(info.st_mode):
            return False, "%s is not a regular file" % (path,)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False, ("%s is group- or other-writable (mode %o); a routing "
                           "snapshot decides access, so it must not be writable "
                           "by anyone but its owner"
                           % (path, stat.S_IMODE(info.st_mode)))
        if info.st_uid not in (0, os.getuid()):
            return False, ("%s is owned by uid %d, which is neither root nor "
                           "this process (%d)" % (path, info.st_uid, os.getuid()))
        return True, ""

    def _stamp_of(self):
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_uid, stat.S_IMODE(info.st_mode))

    def maybe_reload(self) -> bool:
        """Re-read if the file moved.  Cheap enough for the request path: one
        `stat`, which is the same price the key store already pays per request,
        and it is what makes a withdrawn share take effect without a restart.
        """
        stamp = self._stamp_of()
        if stamp == self._stamp:
            return False
        self.load()
        return True

    def load(self) -> None:
        """Read and validate.  Never raises: a snapshot that cannot be trusted
        leaves the router empty, which leaves the legacy path in charge.
        """
        stamp = self._stamp_of()
        try:
            ok, why = self._trust(self.path)
            if not ok:
                raise SnapshotError(why)
            with open(self.path, "rb") as fh:
                raw = fh.read()
            digest = hashlib.sha256(raw).hexdigest()
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict):
                raise SnapshotError("the snapshot must be a JSON object")
            if doc.get("schema") != SCHEMA:
                raise SnapshotError("schema must be %r, got %r"
                                    % (SCHEMA, doc.get("schema")))
            engines, endpoints = self._parse(doc)
        except (SnapshotError, OSError, ValueError, UnicodeDecodeError) as exc:
            with self._lock:
                self._stamp = stamp
                self._tables = ({}, {})
                self.snapshot_sha256 = None
                self.revision = None
                self.loaded_at = None
                self.error = str(exc)
                self.rejected = ()
            print("[routing] snapshot not loaded: %s" % (exc,), flush=True)
            return

        with self._lock:
            self._stamp = stamp
            self._tables = (engines, dict(endpoints))
            self.snapshot_sha256 = digest
            self.revision = doc.get("revision")
            self.generated_at = doc.get("generated_at")
            self.source = doc.get("source")
            self.loaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.error = None
            self.reloads += 1
            self.rejected = tuple(getattr(self, "_last_rejected", ()))

    def _parse(self, doc):
        """Validate into tables.  Raises SnapshotError; returns (engines, by_alias).

        An entry that is individually malformed is *dropped with a reason*
        rather than taking the whole snapshot down -- one bad line in a registry
        export should not silently revert everybody to the single upstream, and
        the reason is published so it is not invisible either.
        """
        raw_engines = doc.get("engines")
        raw_endpoints = doc.get("endpoints")
        if not isinstance(raw_engines, list) or not isinstance(raw_endpoints, list):
            raise SnapshotError("`engines` and `endpoints` must both be lists")

        rejected = []
        # Reset first: this is read by `load` only on the success path, and a
        # stale list from the previous load would attribute old reasons to a
        # snapshot that never produced them.
        self._last_rejected = []
        engines: dict[str, Engine] = {}
        for raw in raw_engines:
            try:
                engine_id, transport, target, ref_file, ref_var, models_path = \
                    _vet_engine(raw, self.allowed)
                if engine_id in engines:
                    raise SnapshotError("engine %r appears twice" % (engine_id,))
            except SnapshotError as exc:
                rejected.append({"what": "engine", "why": str(exc)})
                continue
            secret = ""
            why = ""
            if self._read_key is not None:
                secret = self._read_key(ref_file, ref_var)
                if not secret:
                    # An engine whose credential could not be read is not
                    # routable: forwarding without it would turn a config
                    # problem into a 401 that looks like the caller's fault.
                    why = ("no %s in %s" % (ref_var, ref_file))
            engines[engine_id] = Engine(engine_id, transport, target, secret,
                                        models_path, why)

        by_alias: dict[str, Endpoint] = {}
        for raw in raw_endpoints:
            try:
                ep = _vet_endpoint(raw, engines)
            except SnapshotError as exc:
                # Malformed *and dropped*, not malformed *and fatal*: one bad
                # line in a registry export should not revert everybody to the
                # single upstream, and the reason is published so it is not
                # invisible either.
                rejected.append({"what": "endpoint", "why": str(exc)})
                continue
            # Ambiguity is the other case, and it is *not* dropped quietly.
            # A name with two meanings is a name whose requests can go to the
            # wrong engine -- the same reasoning that makes a duplicated
            # `--alias` a startup error in `gateway.parse_aliases` rather than
            # something to skip.
            if ep.model_alias in self.reserved_aliases:
                raise SnapshotError(
                    "endpoint %r publishes %r, which the operator already "
                    "published with --alias; a name with two meanings is "
                    "refused" % (ep.endpoint_id, ep.model_alias))
            if ep.model_alias in by_alias:
                raise SnapshotError(
                    "endpoint %r and %r both publish %r"
                    % (by_alias[ep.model_alias].endpoint_id,
                       ep.endpoint_id, ep.model_alias))
            by_alias[ep.model_alias] = ep

        if not engines:
            reasons = "; ".join(r["why"] for r in rejected) or "no engine entries"
            raise SnapshotError("no usable engine entries (%s)" % reasons)
        self._last_rejected = rejected
        return engines, by_alias

    # -- decisions -------------------------------------------------------
    def current(self):
        """The tables in force, read without the lock.

        Bound to a local *before* the two subscripts, which is the whole point:
        `self._tables[0], self._tables[1]` is two attribute loads, and a reload
        landing between them would return a pair no snapshot ever had -- an
        endpoint table from one revision beside an engine table from another.
        One load, one revision, one decision.
        """
        tables = self._tables
        return tables[0], tables[1]

    def decide(self, model: str | None, principal: str | None):
        """`None` (not ours -- use the legacy path), a `Route`, or a `Refusal`.

        A name this snapshot does not publish is *not* an endpoint, so the
        request continues to the single upstream exactly as it did before the
        snapshot existed.  That is the whole of "the default route's behaviour
        is unchanged".
        """
        if not model:
            return None
        engines, by_alias = self.current()
        endpoint = by_alias.get(model)
        if endpoint is None:
            return None
        allowed, reason = endpoint.allows(principal)
        if not allowed:
            self.refused += 1
            if reason == NO_PRINCIPAL:
                return Refusal(403, "endpoint_forbidden", NO_PRINCIPAL_TEXT,
                               reason)
            return Refusal(404, "model_not_found", NO_SUCH_MODEL, reason)
        engine = engines.get(endpoint.engine_id)
        if engine is None or not engine.usable:
            # Reached only by a caller who *is* entitled -- the entitlement
            # check is above -- so this answer leaks nothing to anybody: they
            # already know the alias, and "the engine behind it is missing its
            # credential" is the difference between a typo and a broken deploy
            # for the one person who can act on it.
            self.refused += 1
            why = "the engine is not in this snapshot" if engine is None else (
                engine.why or "the engine has no usable credential")
            return Refusal(503, "engine_unavailable",
                           "the engine behind this model is unavailable: %s" % why,
                           ENGINE_UNUSABLE)
        self.routed += 1
        return Route(endpoint, engine)

    def public_aliases(self, principal: str | None) -> list[str]:
        """The aliases this caller may see.  Drives `/v1/models`.

        Entitled *and* callable: an endpoint whose engine has no usable
        credential is not listed, because "a model this caller has" which always
        answers 503 is not a model this caller has.  This is the list that makes
        a private model unenumerable -- a name that is not in it was never
        handed out.
        """
        engines, by_alias = self.current()
        out = []
        for alias, ep in by_alias.items():
            engine = engines.get(ep.engine_id)
            if engine is None or not engine.usable:
                continue
            if ep.allows(principal)[0]:
                out.append(alias)
        return sorted(out)

    # -- observation -----------------------------------------------------
    def status(self) -> dict:
        engines, by_alias = self.current()
        return {
            # `loaded` is the question an operator asks.  False with an `error`
            # beside it is the state that matters: a snapshot was configured and
            # is *not* in force, which is not the same as none being configured
            # (that reads as `routing: null` one level up).
            "loaded": self.snapshot_sha256 is not None,
            "path": self.path,
            "schema": SCHEMA,
            "snapshot_sha256": self.snapshot_sha256,
            "revision": self.revision,
            "source": self.source,
            "generated_at": self.generated_at,
            "loaded_at": self.loaded_at,
            "error": self.error,
            "reloads": self.reloads,
            "routed": self.routed,
            "refused": self.refused,
            "engines": [e.public() for e in engines.values()],
            "endpoints": [ep.public() for ep in by_alias.values()],
            "rejected_entries": list(self.rejected),
        }
