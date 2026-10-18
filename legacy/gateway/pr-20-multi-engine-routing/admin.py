"""Issue, inspect, disable and revoke API keys -- from the server, by hand.

    python3 -m deploy.dell.gateway.admin key create hr-system --note OPS-1234
    python3 -m deploy.dell.gateway.admin key list
    python3 -m deploy.dell.gateway.admin key disable k_ab12cd34
    python3 -m deploy.dell.gateway.admin key revoke  k_ab12cd34
    python3 -m deploy.dell.gateway.admin usage

WHY A CLI AND NOT A WEB PAGE.  The portal has no identity of its own: Caddy
authenticates the *request* with one shared basic_auth credential and then
deletes every client-supplied identity header at the edge, so by the time a
request reaches the portal there is nothing in it that says which human sent
it.  Two colleagues with the same login are indistinguishable, and "an admin
button" would be a button that anyone holding the shared password can press.
The brief anticipated exactly this and said the first version should manage keys
over a protected server-side CLI rather than expose an unsafe web control.
This is that CLI.  The console's API Gateway panel is deliberately read-only.

It is a *separate process* from the gateway, and the gateway notices its writes
without a restart -- see the cross-process section of `keystore.py`.  That is
what makes revocation usable: the answer to "this key leaked" is one command
and a second, not a restart of the thing serving the model.

WHAT IT WILL NOT DO.  It will not print a key that already exists -- only
creation shows a plaintext, once, and there is no command that can bring one
back, because the store does not contain one.  It also never prints a digest,
which is the value a key is checked against and therefore the value that turns
a list into a lookup table.

`usage` READS TWO THINGS, AND SAYS SO.  The per-key counts come from the key
store, which the gateway persists.  The process totals come from `status.json`,
which only exists while the gateway is running -- and it is absent, not zeroed,
when it is not.  Printing 0 requests for a stopped gateway would be a number
that means the opposite of what it looks like, so the command reports whether
it could read the live view at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .keystore import KeyStore

DEFAULT_STORE = "{{LAB_ROOT}}/llm-lab-data/api-gateway/keys.json"
DEFAULT_STATUS = "{{LAB_ROOT}}/llm-lab-data/api-gateway/status.json"

# Shown once, at the moment the key exists.  It is spoken to a human rather than
# written anywhere, so it is a sentence and not a field.
_CREATE_EPILOGUE = """
This is the only time this key will be shown -- the store keeps a digest, not
the key, and no command can recover it.  If it is lost, issue another and revoke
this one.  Paste it into the system's own configuration, not into a ticket, a
chat, a shell history or a file inside the checkout.

  base URL   https://{{LAB_HOST}}:8443/v1
  model      ask the gateway:  curl -H "Authorization: Bearer $KEY" \\
                                 https://{{LAB_HOST}}:8443/v1/models

  export OPENAI_BASE_URL=https://{{LAB_HOST}}:8443/v1
  export OPENAI_API_KEY=<the key above>

The TLS certificate on that address is signed by this host's own CA; the
example in examples/openai_client.py shows how to trust it, rather than how to
turn verification off.
"""


def _fmt_time(value) -> str:
    return value if value else "-"


def _state(rec: dict) -> str:
    if rec.get("revoked"):
        return "revoked"
    if rec.get("disabled"):
        return "disabled"
    return "active"


def _human(records: list[dict]) -> str:
    if not records:
        return "(no keys)"
    width = max(max(len(str(r.get("label") or "")) for r in records), 5)
    who_w = max(max(len(str(r.get("owner") or "-")) for r in records), 5)
    lines = ["%-14s  %-*s  %-*s  %-9s  %6s  %6s  %6s  %6s  %s"
             % ("ID", width, "SYSTEM", who_w, "OWNER", "STATE", "REQ", "OK",
                "FAIL", "429", "LAST USED"),
             "%s  %s  %s  %s  %s  %s  %s  %s  %s"
             % ("-" * 14, "-" * width, "-" * who_w, "-" * 9, "-" * 6,
                "-" * 6, "-" * 6, "-" * 6, "-" * 19)]
    for r in sorted(records, key=lambda x: str(x.get("label") or "")):
        lines.append("%-14s  %-*s  %-*s  %-9s  %6d  %6d  %6d  %6d  %s"
                     % (r.get("id") or "?", width, r.get("label") or "",
                        who_w, r.get("owner") or "-",
                        _state(r), int(r.get("requests") or 0),
                        int(r.get("requests_ok") or 0),
                        int(r.get("requests_failed") or 0),
                        int(r.get("requests_rate_limited") or 0),
                        _fmt_time(r.get("last_used_at"))))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# key <verb>
# ---------------------------------------------------------------------------
def cmd_create(store: KeyStore, args) -> int:
    key, rec = store.issue(args.label, note=args.note or "",
                           owner=args.owner or "")
    print("created %s for %r" % (rec["id"], rec["label"]))
    if rec.get("owner"):
        print("bound to %s" % rec["owner"])
    print()
    print(key)
    print(_CREATE_EPILOGUE)
    return 0


def cmd_bind(store: KeyStore, args) -> int:
    """Set or clear the identity binding a registered endpoint is checked against.

    Separate from `enable`/`disable` on purpose: those decide whether a key
    works, this decides *who it is*.  Clearing it (`--clear`) is supported and
    is not the same as revoking: the key keeps working on the single upstream
    and loses access to shared models, which is the right answer when a key was
    bound to the wrong person.

    A bare `bind <id>` with nothing else is refused rather than read as a
    request to clear: the two commands a person means when they type that are
    "set" (and they forgot the name) and "clear" (and they forgot the flag), and
    one of those two silently strips a key's access to every shared model.
    """
    if not args.clear and not args.owner:
        print("a bind wants an owner, or --clear to remove one: "
              "`key bind %s <user|uid:N>` or `key bind %s --clear`"
              % (args.ident, args.ident), file=sys.stderr)
        return 2
    try:
        rec = store.bind(args.ident, "" if args.clear else args.owner)
    except KeyError:
        print("no key with id or value %r" % (args.ident,), file=sys.stderr)
        return 1
    if rec.get("owner"):
        print("%s is now bound to %s" % (rec["id"], rec["owner"]))
    else:
        print("%s is no longer bound to anybody; it keeps the single upstream "
              "and cannot reach a registered model" % rec["id"])
    return 0


def cmd_list(store: KeyStore, args) -> int:
    records = store.list_records(include_revoked=not args.active_only)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        print(_human(records))
    return 0


def cmd_show(store: KeyStore, args) -> int:
    rec = store.get(args.ident)
    if rec is None:
        print("no key with id or value %r" % args.ident, file=sys.stderr)
        return 1
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def _change(store: KeyStore, ident: str, **kwargs) -> int:
    """Apply one state change and print what it now says.

    The new state is printed rather than assumed: `enable` on a revoked key
    does not resurrect it (revocation is meant to be final), and a command that
    reported "enabled" while the record still said `revoked` would be a command
    whose statement and whose effect disagree.
    """
    try:
        rec = store.set_state(ident, **kwargs)
    except KeyError:
        print("no key with id %r" % ident, file=sys.stderr)
        return 1
    state = _state(rec)
    print("%s is now %s" % (rec["id"], state))
    if state == "revoked":
        print("a revoked key stays revoked: this is intentional, and the record "
              "keeps saying so")
    return 0


def cmd_disable(store: KeyStore, args) -> int:
    return _change(store, args.ident, disabled=True)


def cmd_enable(store: KeyStore, args) -> int:
    return _change(store, args.ident, disabled=False)


def cmd_revoke(store: KeyStore, args) -> int:
    return _change(store, args.ident, revoked=True)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------
def _read_status(path: str) -> tuple[dict | None, str]:
    """The gateway's live view, or (None, why not).

    The reason is returned rather than swallowed because "the gateway is not
    running" and "the gateway is running and nobody is calling it" are the two
    readings an operator most needs to tell apart, and a bare 0 answers neither.
    """
    if not path:
        return None, "no status file configured"
    if not os.path.isfile(path):
        return None, "no status file at %s (the gateway writes it while running)" % path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "the status file at %s cannot be read: %r" % (path, exc)
    if not isinstance(doc, dict):
        return None, "the status file at %s is not a document" % path
    return doc, ""


def cmd_usage(store: KeyStore, args) -> int:
    records = store.list_records(include_revoked=not args.active_only)
    status, why = _read_status(args.status_file)

    totals = {"requests": 0, "requests_ok": 0, "requests_failed": 0,
              "requests_rate_limited": 0}
    for r in records:
        for field in totals:
            totals[field] += int(r.get(field) or 0)

    if args.json:
        print(json.dumps({
            "store": store.path,
            "gateway": status,
            "gateway_unavailable": why or None,
            "totals_from_keys": totals,
            "keys": records,
        }, indent=2, ensure_ascii=False))
        return 0

    print("store        : %s" % store.path)
    if status is None:
        # Not "0" and not "(down)": this command cannot see the process, and the
        # difference between "it is not running" and "it is running and idle" is
        # the answer to the question being asked.
        print("gateway      : cannot read the live view -- %s" % why)
    else:
        up = status.get("upstream") or {}
        limits = status.get("limits") or {}
        print("gateway      : %s, up %ss, listening on %s"
              % (status.get("service") or "?", status.get("uptime_s") or 0,
                 status.get("listen") or "?"))
        print("               status written %s" % (status.get("generated_at") or "?"))
        if up.get("ok") is None:
            print("upstream     : not checked yet")
        else:
            print("upstream     : %s (last check %s)"
                  % ("ok" if up.get("ok") else "FAILING",
                     up.get("checked_at") or "?"))
            if up.get("error"):
                print("               %s" % up["error"])
        print("limits       : %s global, %s per key, queue %s, wait %ss"
              % (limits.get("global_limit"), limits.get("per_user_limit"),
                 limits.get("queue_limit"), limits.get("wait_timeout_s")))
        print("               in flight %s, waiting %s (peaks %s / %s)"
              % (limits.get("active"), limits.get("waiting"),
                 limits.get("peak_active"), limits.get("peak_waiting")))
        t = status.get("totals") or {}
        if t:
            print("since start  : %s requests (%s ok, %s failed, %s cancelled, "
                  "%s refused by a limit)" % (t.get("requests"), t.get("ok"),
                                              t.get("failed"), t.get("cancelled"),
                                              t.get("rate_limited")))
    print()
    print(_human(records))
    print()
    print("%-14s  %-*s  %-9s  %6d  %6d  %6d  %6d"
          % ("TOTAL", 5, "", "", totals["requests"], totals["requests_ok"],
             totals["requests_failed"], totals["requests_rate_limited"]))
    print()
    print("counters are written by the gateway; a key's totals do not move while "
          "it is stopped.")
    print("a cancelled request (the caller hung up) is counted in REQ and in "
          "neither OK nor FAIL.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="admin",
        description="manage LLM Lab API keys (the admin surface for the gateway)")
    ap.add_argument("--store", default=DEFAULT_STORE,
                    help="the key store file (default: %(default)s)")
    ap.add_argument("--status-file", default=DEFAULT_STATUS,
                    help="the gateway's live view, read by `usage` "
                         "(default: %(default)s)")
    sub = ap.add_subparsers(dest="command", required=True)

    key = sub.add_parser("key", help="create, inspect and revoke keys")
    ksub = key.add_subparsers(dest="key_command", required=True)

    p = ksub.add_parser("create", help="make a key and show it once")
    p.add_argument("label",
                   help="the owning system, e.g. hr-system, oa-system, "
                        "knowledge-base")
    p.add_argument("--note", default="", help="free text, e.g. a ticket id")
    p.add_argument("--owner", default="", metavar="USER",
                   help="bind the key to a platform user (or uid:<n>), which is "
                        "what a shared model's ACL is checked against; omit it "
                        "and the key simply cannot reach a registered model")
    p.set_defaults(func=cmd_create)

    p = ksub.add_parser("bind", help="set or clear a key's identity binding")
    p.add_argument("ident", help="a key id (k_...) or the key itself")
    p.add_argument("owner", nargs="?", default="",
                   help="a platform user name or uid:<number>")
    p.add_argument("--clear", action="store_true",
                   help="remove the binding (the key keeps working upstream)")
    p.set_defaults(func=cmd_bind)

    p = ksub.add_parser("list", help="every key, without digests")
    p.add_argument("--json", action="store_true")
    p.add_argument("--active-only", action="store_true",
                   help="hide revoked keys")
    p.set_defaults(func=cmd_list)

    p = ksub.add_parser("show", help="one key, as JSON")
    p.add_argument("ident", help="a key id (k_...) or the key itself")
    p.set_defaults(func=cmd_show)

    p = ksub.add_parser("disable", help="stop a key, reversibly")
    p.add_argument("ident")
    p.set_defaults(func=cmd_disable)

    p = ksub.add_parser("enable", help="undo a disable")
    p.add_argument("ident")
    p.set_defaults(func=cmd_enable)

    p = ksub.add_parser("revoke", help="end a key, permanently")
    p.add_argument("ident")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("usage", help="per-key and process totals")
    p.add_argument("--json", action="store_true")
    p.add_argument("--active-only", action="store_true",
                   help="hide revoked keys")
    p.set_defaults(func=cmd_usage)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = KeyStore(args.store)
    except RuntimeError as exc:
        # Not a traceback: the interesting part of this failure is the path and
        # the fact that it is a store problem, and a traceback buries both.
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        return args.func(store, args)
    except (ValueError, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
