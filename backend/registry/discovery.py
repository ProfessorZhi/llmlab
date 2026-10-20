"""Read-only engine **candidate** discovery for the platform dashboard.

Scope, stated up front because this module is easy to misread
-------------------------------------------------------------
This is a *candidate* pipeline, not an auto-registration feature. A candidate is
a claim that "something may be listening here, as reported by an already
authorized source". It is **not**:

* a registration — nothing is written to the registry store;
* a verification — no probe is issued, no health is claimed;
* an ownership claim — ``owner_binding`` is always ``None`` and no code path in
  this module can set it;
* a share or an invoke grant — ``shareable`` and ``invokable`` are ``False``.

Discovery reads only **injectable descriptors** supplied by an already
authorized component (a platform launch receipt, an operator-maintained
descriptor, or a test fixture). It never scans ports, never walks ``/proc``,
never reads another user's process, and never opens a socket. A source that
cannot answer returns ``source_unavailable``; it does not guess.

Why not reuse the UID-shaped seams
----------------------------------
``backend.observability.engines.EngineRecord`` and
``backend.registry.ownership`` are built around a **Linux UID**
(``owner_uid``, ``StaticOwnershipProver`` for ``(host, port) -> uid``). The
platform account is a separate identity that may share a username with a Linux
account and still be an unrelated principal. This module therefore carries no
UID at all: attribution requires an explicit, evidenced binding supplied later
by an operator-approved path, and every candidate ships with
``owner_binding = None``.

TTL / fail-closed
-----------------
A snapshot is stamped with ``generated_at`` and a ``ttl_seconds``. Consumers
must call :meth:`DiscoverySnapshot.for_display` with their own clock: once the
snapshot is older than its TTL the candidates are withheld and the reason says
so. A stale inventory is not an inventory.

Stable JSON
-----------
:meth:`DiscoverySnapshot.to_dict` emits a fixed key set (no timestamps of
"now", no environment data, no credentials) so a future dashboard can render it
without special-casing.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

#: A candidate may only point at this machine's loopback interface. Discovery
#: never describes a remote endpoint: that is a manual registration decision
#: with its own authorization, not something an inventory sweep may imply.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Statuses a candidate can carry. There is deliberately no "healthy" or
#: "owned" here: those words belong to verification and binding, which this
#: module does not perform.
STATUS_DETECTED_UNVERIFIED = "detected_unverified"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"

#: Defaults chosen to keep one bad source from producing an unbounded page.
DEFAULT_TTL_SECONDS = 30
DEFAULT_MAX_CANDIDATES = 64
MAX_NAME_LENGTH = 120

ALLOWED_SCHEMES = frozenset({"http", "https"})


class DiscoveryError(ValueError):
    """A descriptor is malformed or unsafe to describe."""


class DiscoverySource(Protocol):
    """An already-authorized supplier of descriptors.

    ``read(now)`` returns mappings shaped ``{"name": str, "target_url": str}``.
    Implementations must not perform network I/O of their own; a platform launch
    receipt reader reads a local receipt file, and a fixture returns constants.
    """

    name: str
    kind: str

    def read(self, now: datetime) -> Iterable[Mapping[str, object]]: ...


@dataclass(frozen=True)
class StaticDescriptorSource:
    """The simplest authorized source: descriptors handed in by a caller.

    ``kind`` is surfaced in the DTO so a dashboard can say *where* a claim came
    from (``launch_receipt`` vs ``descriptor``). It is never used as proof of
    ownership.
    """

    name: str
    descriptors: tuple[Mapping[str, object], ...] = ()
    kind: str = "descriptor"

    def read(self, now: datetime) -> Iterable[Mapping[str, object]]:
        del now
        return self.descriptors


@dataclass(frozen=True)
class UnavailableSource:
    """A source the deployment cannot read (no permission, not installed).

    It exists so "we could not look" is a first-class outcome rather than an
    empty list that quietly reads as "nothing is running".
    """

    name: str
    reason: str
    kind: str = "unavailable"

    def read(self, now: datetime) -> Iterable[Mapping[str, object]]:
        del now
        raise DiscoveryError(self.reason)


@dataclass(frozen=True)
class EngineCandidate:
    """One immutable, unclaimed candidate."""

    candidate_id: str
    display_name: str
    host: str
    port: int
    scheme: str
    source: str
    source_kind: str
    status: str
    observed_at: str
    reason: str | None = None

    # The four fields that would turn a candidate into a platform object. They
    # are pinned to their safe values; nothing here assigns them.
    owner_binding: str | None = None
    registered: bool = False
    shareable: bool = False
    invokable: bool = False

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "target": {
                "scheme": self.scheme,
                "host": self.host,
                "port": self.port,
            },
            "source": self.source,
            "source_kind": self.source_kind,
            "status": self.status,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "owner_binding": self.owner_binding,
            "registered": self.registered,
            "shareable": self.shareable,
            "invokable": self.invokable,
        }


@dataclass(frozen=True)
class DiscoverySnapshot:
    """An immutable inventory result with its own freshness contract."""

    generated_at: datetime
    ttl_seconds: int
    candidates: tuple[EngineCandidate, ...]
    unavailable_sources: tuple[str, ...] = ()
    truncated: bool = False
    reason: str | None = None

    @property
    def expires_at(self) -> datetime:
        return self.generated_at + timedelta(seconds=self.ttl_seconds)

    def is_stale(self, now: datetime) -> bool:
        return now >= self.expires_at

    def for_display(self, now: datetime) -> dict:
        """JSON shape for a dashboard; withholds everything once stale."""
        if self.is_stale(now):
            return {
                "generated_at": _iso(self.generated_at),
                "expires_at": _iso(self.expires_at),
                "stale": True,
                "reason": "snapshot_stale",
                "counts": {"total": 0, "detected_unverified": 0,
                           "source_unavailable": 0, "unavailable_sources": len(self.unavailable_sources)},
                "candidates": [],
                "unavailable_sources": list(self.unavailable_sources),
                "truncated": self.truncated,
            }
        counts = {"total": len(self.candidates), "detected_unverified": 0,
                  "source_unavailable": 0, "unavailable_sources": len(self.unavailable_sources)}
        for candidate in self.candidates:
            if candidate.status == STATUS_DETECTED_UNVERIFIED:
                counts["detected_unverified"] += 1
            elif candidate.status == STATUS_SOURCE_UNAVAILABLE:
                counts["source_unavailable"] += 1
        return {
            "generated_at": _iso(self.generated_at),
            "expires_at": _iso(self.expires_at),
            "stale": False,
            "reason": self.reason,
            "counts": counts,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "unavailable_sources": list(self.unavailable_sources),
            "truncated": self.truncated,
        }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _candidate_id(source: str, scheme: str, host: str, port: int) -> str:
    digest = hashlib.sha256(f"{source}|{scheme}://{host}:{port}".encode("utf-8")).hexdigest()
    return "cand_" + digest[:16]


def _loopback_host(host: str) -> str | None:
    """Return the canonical loopback host, or ``None`` when it is not one."""
    lowered = host.lower()
    if lowered in LOOPBACK_HOSTS:
        return "::1" if lowered == "::1" else "127.0.0.1"
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return None
    if address.is_loopback:
        return "::1" if address.version == 6 else "127.0.0.1"
    return None


def _parse_descriptor(raw: Mapping[str, object], source: str, kind: str,
                      observed_at: str) -> EngineCandidate | None:
    """Validate one descriptor. ``None`` means "refuse to describe this"."""
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    target = raw.get("target_url")
    if not isinstance(name, str) or not name.strip():
        return None
    display = name.strip()[:MAX_NAME_LENGTH]
    if not isinstance(target, str) or not target.strip():
        return None
    try:
        parts = urlsplit(target.strip())
    except ValueError:
        return None
    if parts.scheme not in ALLOWED_SCHEMES:
        return None
    # A credential in a URL is a secret in an inventory string.
    if parts.username or parts.password:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if parts.hostname is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    # Port 0 is "any port", not an endpoint; anything outside the TCP range is
    # a malformed descriptor rather than a target worth describing.
    if not 1 <= port <= 65535:
        return None
    host = _loopback_host(parts.hostname)
    if host is None:
        return None
    return EngineCandidate(
        candidate_id=_candidate_id(source, parts.scheme, host, port),
        display_name=display,
        host=host,
        port=port,
        scheme=parts.scheme,
        source=source,
        source_kind=kind,
        status=STATUS_DETECTED_UNVERIFIED,
        observed_at=observed_at,
        reason="unverified_source_descriptor",
    )


def discover(
    sources: Sequence[DiscoverySource],
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> DiscoverySnapshot:
    """Read every authorized source into one deduplicated snapshot.

    Ordering is deterministic: sources in the order given, descriptors in the
    order returned, first appearance of a ``(host, port, scheme)`` target wins.
    Duplicates from a later source are dropped rather than merged, so a weaker
    source can never overwrite a stronger one's claim.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DiscoveryError("now must be timezone-aware")
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise DiscoveryError("ttl_seconds must be a positive integer")
    if not isinstance(max_candidates, int) or max_candidates <= 0:
        raise DiscoveryError("max_candidates must be a positive integer")

    observed_at = _iso(moment)
    seen: set[tuple[str, int, str]] = set()
    candidates: list[EngineCandidate] = []
    unavailable: list[str] = []
    truncated = False

    for source in sources:
        name = str(getattr(source, "name", "unnamed"))
        kind = str(getattr(source, "kind", "descriptor"))
        try:
            descriptors = list(source.read(moment))
        except Exception:  # noqa: BLE001 - a broken source is data, not a crash
            unavailable.append(name)
            candidates.append(
                EngineCandidate(
                    candidate_id=_candidate_id(name, "http", "127.0.0.1", 0),
                    display_name=name,
                    host="127.0.0.1",
                    port=0,
                    scheme="http",
                    source=name,
                    source_kind=kind,
                    status=STATUS_SOURCE_UNAVAILABLE,
                    observed_at=observed_at,
                    reason="source_unavailable",
                )
            )
            continue

        for raw in descriptors:
            candidate = _parse_descriptor(raw, name, kind, observed_at)
            if candidate is None:
                continue
            key = (candidate.scheme, candidate.port, candidate.host)
            if key in seen:
                continue
            if len(candidates) >= max_candidates:
                truncated = True
                continue
            seen.add(key)
            candidates.append(candidate)

    return DiscoverySnapshot(
        generated_at=moment,
        ttl_seconds=ttl_seconds,
        candidates=tuple(candidates),
        unavailable_sources=tuple(unavailable),
        truncated=truncated,
    )


def bind_candidate(
    candidate: EngineCandidate,
    *,
    platform_principal: str,
    evidence_kind: str,
    evidence_id: str,
) -> EngineCandidate:
    """Attach an **explicitly evidenced** owner binding to a copy.

    This function does not verify anything itself: it exists so the shape of a
    bound candidate is reviewed once, and so the default path (never calling it)
    keeps every candidate unowned. Callers must have already approved the
    evidence through an operator-controlled path; equal usernames, matching
    passwords, self-reported headers or guessed process names are *not*
    evidence and must never be passed here.
    """
    if not isinstance(candidate, EngineCandidate):
        raise DiscoveryError("candidate must be an EngineCandidate")
    if evidence_kind not in {"operator_policy", "launch_receipt"}:
        raise DiscoveryError(f"unsupported evidence kind: {evidence_kind!r}")
    if not platform_principal or not evidence_id:
        raise DiscoveryError("a binding needs a platform principal and an evidence id")
    bound = replace(candidate, owner_binding=platform_principal)
    # A binding is still not a registration, a share, or an invoke right.
    if bound.registered or bound.shareable or bound.invokable:
        raise DiscoveryError("binding must not grant registration, sharing or invoke")
    return bound


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_TTL_SECONDS",
    "STATUS_DETECTED_UNVERIFIED",
    "STATUS_SOURCE_UNAVAILABLE",
    "DiscoveryError",
    "DiscoverySnapshot",
    "DiscoverySource",
    "EngineCandidate",
    "StaticDescriptorSource",
    "UnavailableSource",
    "bind_candidate",
    "discover",
]
