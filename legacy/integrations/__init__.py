# -*- coding: utf-8 -*-
"""Adapters between LLM Lab and the things that serve tokens.

The contract
------------
Every adapter in this package is a **read-mostly, network-facing client** and
must obey four rules, because the console runs on a workstation and the things
it talks to are production inference services:

1. **No shell.**  An adapter speaks HTTP or SFTP.  It never builds a command
   string, never takes one from a caller, and never executes one remotely.
   (The single exception in this project is the portal's fixed read-only SSH
   probe, which lives in ``portal/server.py`` and is a hard-coded constant.)

2. **Credentials come from a file, never from an argument.**  An adapter is
   handed a *path* to a secrets file and reads the one variable it needs.  Key
   material is never a function parameter, never a log line, never part of a
   response object, and never echoed in an exception message.

3. **Nothing is mutated.**  No adapter restarts a service, writes a file on the
   node, changes a runtime parameter, or cancels somebody else's request.

4. **Unavailability is reported, not smoothed over.**  If an endpoint answers
   with an error, or a metric cannot be read, the adapter returns that fact
   with its cause.  It does not substitute a cached or historical value and
   present it as current.

Why the registry exists
-----------------------
``config/llm-lab.example.toml`` names an adapter per endpoint with ``kind = "..."``.
``load()`` is the single place that maps that string to a class, so an unknown
kind fails loudly at configuration time instead of silently producing an
endpoint that never answers.
"""

from __future__ import annotations

__all__ = ["load", "KINDS"]

KINDS = {
    "strata": "integrations.strata:StrataClient",
    "openai-compatible": "integrations.openai_compatible:OpenAICompatibleClient",
}


def load(kind: str, **kwargs):
    """Instantiate the adapter registered under ``kind``.

    Raises ``KeyError`` for an unregistered kind -- deliberately: a typo in the
    config should stop the console from claiming an endpoint works.
    """
    if kind not in KINDS:
        raise KeyError(
            "unknown endpoint kind %r; known kinds: %s" % (kind, ", ".join(sorted(KINDS))))
    module_name, class_name = KINDS[kind].split(":")
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)(**kwargs)
