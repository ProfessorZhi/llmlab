# -*- coding: utf-8 -*-
"""Publication interface for the Feishu wiki space 「专项汇报」.

**This module does not write to Feishu, on purpose.**

The brief for this stage is explicit: design the publish interface, do not
perform the write.  So the interface is real -- it validates a report, resolves
its destination, renders the document that *would* be sent, and returns that
payload for a human to inspect -- while the one function that would mutate a
shared company wiki raises unless it is explicitly unlocked.

Why it is built this way rather than as a thin ``create_doc()``
---------------------------------------------------------------
Publishing to a company wiki is outward-facing and hard to undo: other people
read it, it may be indexed, and a wrong node placement is not something the
author can quietly fix.  A single function that *can* write is a function that
will eventually be called by accident -- from a retry loop, from a "fix the
listing" refactor, from a test that forgot to mock it.  Making the write path
refuse unless a caller passes an explicit acknowledgement turns that accident
into a loud error.

What a real implementation still needs (recorded here so the gap is not
rediscovered later):

  * an app credential with ``docx`` and ``wiki`` scopes, from the vault --
    NOT a password in a config file;
  * the target ``space_id`` and a parent ``node_token``, both of which are
    **untrusted inputs**: resolve them from the API at publish time rather than
    trusting a value copied into a config or remembered from an earlier call;
  * a ``redirect_uri`` allow-list check before any OAuth step, because a
    failure there looks like a credential problem and is not.

See ``docs/feishu-publish-interface.md`` for the workflow this participates in.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


class FeishuWriteRefused(RuntimeError):
    """Raised when something tries to write to Feishu without unlocking it."""


@dataclass
class PublishRequest:
    """Everything a publish decision needs, with provenance attached."""

    title: str
    markdown_path: str
    space_id: str = ""
    parent_node_token: str = ""
    tags: list = field(default_factory=list)
    #: Where the content came from, so a reader can tell a regenerated report
    #: from the original.
    provenance: dict = field(default_factory=dict)


@dataclass
class PublishPlan:
    """A reviewable, side-effect-free description of a would-be publish."""

    request: PublishRequest
    body_markdown: str
    content_sha256: str
    content_bytes: int
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "title": self.request.title,
            "target": {"space_id": self.request.space_id or None,
                       "parent_node_token": self.request.parent_node_token or None},
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
            "tags": list(self.request.tags),
            "provenance": dict(self.request.provenance),
            "warnings": list(self.warnings),
            "wrote": False,
        }


def plan_publish(request: PublishRequest, body_markdown: str) -> PublishPlan:
    """Validate and describe a publish.  Pure: reads nothing, writes nothing."""
    warnings = []
    if not request.title.strip():
        raise ValueError("a publish needs a title")
    if not request.space_id:
        warnings.append(
            "space_id is empty. The real implementation must resolve the wiki "
            "space id from the API rather than reading it from a config file.")
    if not request.parent_node_token:
        warnings.append(
            "parent_node_token is empty. A returned parent node is not "
            "authoritative -- resolve it at publish time.")
    if not request.provenance:
        warnings.append(
            "no provenance recorded. A published report must say which "
            "artifacts and which run it was generated from.")
    encoded = body_markdown.encode("utf-8")
    return PublishPlan(
        request=request,
        body_markdown=body_markdown,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
        warnings=warnings,
    )


def publish(plan: PublishPlan, *, acknowledged: bool = False):
    """Would publish ``plan``.  Refuses unless ``acknowledged`` is True.

    Even with ``acknowledged=True`` this raises: there is no credential path
    wired up in this project, and inventing one inside a "design the interface"
    stage is exactly the mistake the brief warns against.  The signature exists
    so the calling code and its tests are written against the real shape.
    """
    if not acknowledged:
        raise FeishuWriteRefused(
            "refusing to write to Feishu. This stage designs the interface only. "
            "Pass acknowledged=True once a credential path and a dry-run mode exist.")
    raise NotImplementedError(
        "no Feishu write path is implemented in this project (by design). "
        "Planned %r at %s; content sha256 %s."
        % (plan.request.title, time.strftime("%Y-%m-%dT%H:%M:%S"), plan.content_sha256[:16]))
