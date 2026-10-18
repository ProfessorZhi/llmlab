"""Audit records that are structurally incapable of holding a credential.

An audit trail for a password-checking subsystem has one obvious failure mode:
someone adds "the password that failed" or "the hash we compared against" to a
record, because it would be *useful* while debugging, and the log becomes a
credential store. Rather than relying on reviewers to notice, the record type
refuses to be constructed with a field whose name looks like credential
material, and the field container is immutable once built.

This is a speed bump, not a proof — a determined caller can still smuggle a
password in under the key ``note``. It is here to make the accident loud and to
make the *shape* of an audit record something a test can assert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

# Names that must never appear as an audit field. Substring matching, so
# `admin_password`, `pwhash` and `session_token` are all caught.
_FORBIDDEN_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "pwhash",
    "secret",
    "shadow",
    "token",
    "credential",
    "hash",
)


def _assert_safe_keys(fields: Mapping[str, Any]) -> None:
    for key in fields:
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError(f"audit field {key!r} may not appear in an audit record")


@dataclass(frozen=True)
class AuditEvent:
    """One thing that happened.

    ``reason`` is the internal :class:`~backend.auth_linux.reasons.DenialReason`,
    which is exactly the precision that must not reach a client. It is safe here
    because audit records stay on the privileged side of the boundary.
    """

    event: str
    outcome: str
    reason: str | None = None
    actor: str | None = None
    host_id: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_safe_keys(self.fields)
        # Freeze so a sink cannot be handed a record that later changes shape.
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "outcome": self.outcome,
            "reason": self.reason,
            "actor": self.actor,
            "host_id": self.host_id,
            "fields": dict(self.fields),
        }


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    """Discards everything. Used where no audit destination is configured."""

    def record(self, event: AuditEvent) -> None:  # pragma: no cover - trivial
        return None


class ListAuditSink:
    """Keeps records in memory.

    Intended for tests and dry runs. It is not a durable audit trail and says so,
    because an audit sink that silently keeps records only until the process
    exits is worse than none if anyone mistakes it for compliance.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def reasons(self) -> list[str | None]:
        return [event.reason for event in self.events]


__all__ = [
    "AuditEvent",
    "AuditSink",
    "ListAuditSink",
    "NullAuditSink",
]
