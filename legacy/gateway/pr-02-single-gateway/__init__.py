"""The LAN API gateway for the LLM Lab model service.

WHY THIS EXISTS.  Strata -- the engine on the Dell -- already accepts several
API keys (`server.py`'s `key_list()`, "#1344"), but the set is read from its
config at startup, so adding one means restarting the model service.  That is
forbidden here, and it is the wrong shape anyway: one shared credential per
deployment cannot be attributed, throttled or revoked per person.

So the gateway sits in front of Strata and owns the thing Strata does not: a
*per-user* key that can be issued, disabled and revoked while Strata runs
untouched.  It holds the one real Strata key server-side and never hands it
out, which is the whole point -- the people calling the model never see the
credential of the service they are calling.

WHAT IT IS NOT.  It is not a second authentication system for the portal.  The
portal keeps its Caddy basic_auth; `/v1/*` is a different door with a different
kind of credential (a Bearer key, because that is what OpenAI-compatible
clients send).  The two must not be conflated: an API key must never open a
console page, and a console login must never call the model.

It binds localhost only.  It is reached from the LAN through Caddy, which
already owns the TLS and the front-door policy.

WHAT THIS DOES *NOT* CLOSE, AND WHY THAT IS WRITTEN DOWN HERE.
-------------------------------------------------------------
Strata listens on `{{LAB_BIND_ADDR}}:8080`.  That means the model API is reachable from
the LAN *without* passing through this gateway, on the host's own address, for
as long as Strata is configured that way -- and it was reachable that way
before this package existed.  The only thing standing in front of it there is
the one shared Strata key.

So: **the gateway does not close the direct path.**  It adds per-user keys,
attribution, quotas and revocation on a *second* path, and that is worth having
on its own, but anyone who reads "we published an API with per-user keys" as
"the shared key is no longer reachable" has it backwards.  Closing :8080 means
either changing Strata's bind address (a restart of the model service, which is
out of scope) or a firewall rule (root on a host where sudo is not available to
this account, and the firewall could not be audited from here).  Both are
decisions for the operator, not something to be done quietly, so the direct
path is recorded as an open finding rather than papered over.

The practical consequence for anyone reading a security claim about this
deployment: `https://<host>:8443/v1/...` is gated per user and auditable;
`http://<host>:8080/v1/...` is one shared secret and nothing else.

WHAT IT RECORDS, AND WHAT IT REFUSES TO.
----------------------------------------
One line per request in `usage.jsonl`: which key, which route, which model, the
status, the latency broken into queue wait / time-to-first-byte / total, the
size of the answer, and the upstream's own token counts when it sent them.
Never the prompt, never the completion.  The first consumer is an HR system, so
the bodies crossing this process are CVs; the reasoning is in `telemetry.py`,
and the self test checks it against a sentinel string rather than trusting it.

WHERE IT SITS, IN THREE LINES:

    client --(its own key)--> Caddy :8443/v1/*  -->  this gateway, localhost:8200
                                                   |-- issues, throttles, counts
                                                   '--> Strata localhost:8080
                                                        (the real key, added here)

    admin --(ssh)--> `python3 -m deploy.dell.gateway.admin key create ...`
"""

__all__ = ["keystore", "limits", "gateway", "telemetry", "admin"]
