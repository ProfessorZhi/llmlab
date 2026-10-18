#!/usr/bin/env python3
"""Start, stop and inspect the LLM Lab services on this host.

This is the *outside* view of the same machinery the portal exposes over HTTP.
It does not implement start or stop: it imports `legacy/console/server.py` and calls
the very same `start_service()` / `stop_service()` the API does.  A second
implementation would be a second set of rules about what counts as "the
process we started", and the two would disagree exactly once -- on the day a
pid had been recycled and one of them killed a stranger.

Why this exists at all, given the portal already has buttons: the portal cannot
start itself.  There has to be something able to launch it at boot, and that
something must respect the same "the user stopped this on purpose" flag the
portal writes -- otherwise a service shut down through the UI comes back on the
next reboot and the switch the operator used turns out to be decoration.

WHAT THIS IS NOT: systemd.  On this host the account has no sudo and no
lingering, so services run as ordinary detached processes and this script is
what supervises them from cron.  There is no socket activation, no restart-on-
crash-with-backoff, no cgroup accounting.  A process that dies between cron
ticks stays dead until the next tick.  That limitation is stated here because
a deployment that quietly lacks it is worse than one that says so.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent          # <checkout>/deploy/templates/labctl.py

# Service notes are Chinese, and a console encoding that cannot represent them
# does not degrade gracefully -- it raises on the print and takes the whole
# command with it, which reads as "the tool is broken" rather than "the console
# is".  Setting the stream explicitly costs nothing and removes the case.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

# The config is a property of the deployment, not of this script, so it comes
# from the environment exactly as it does for the portal.  Defaulting to the
# site file when it is present is what makes `@reboot` work without the cron
# line having to carry it -- and a cron line is a bad place to keep something
# that must not be got wrong.
os.environ.setdefault("LAB_CONFIG", str(ROOT / "config" / "dell-site.example.toml"))

_spec = importlib.util.spec_from_file_location("llm_lab_server", ROOT / "legacy" / "console" / "server.py")
srv = importlib.util.module_from_spec(_spec)
sys.modules["llm_lab_server"] = srv
_spec.loader.exec_module(srv)


def _keys() -> list[str]:
    """Every service this host can manage, portal first.

    The portal is first because it is the one thing here whose absence makes
    every other status unreadable -- a report about Grafana is not much use if
    the page that would show it is down.
    """
    keys = sorted(srv.managed_services())
    return (["portal"] if "portal" in keys else []) + [k for k in keys if k != "portal"]


def _one(key: str) -> dict:
    return srv.service_state(key)


def cmd_status(args) -> int:
    keys = [args.service] if args.service else _keys()
    print("%-16s %-9s %-6s %-7s %-6s %s"
          % ("SERVICE", "STATE", "PORT", "PID", "DEPS", "NOTE"))
    worst = 0
    for k in keys:
        st = _one(k)
        down = st.get("deps_down") or []
        print("%-16s %-9s %-6s %-7s %-6s %s"
              % (k, st["state"], st["port"] or "-", st["pid"] or "-",
                 ("!" + ",".join(down)) if down else "-",
                 st["note"][:38]))
        if st["state"] not in ("up", "starting"):
            worst = 1
    print()
    print("state root : %s" % srv.STATE)
    print("config     : %s" % srv.CONFIG_PATH)
    print("deps column: `!x` marks a declared dependency that is not up, so a")
    print("             service can be up while the chain it belongs to is not.")
    return worst


def _report(code: int, payload: dict) -> int:
    bad = not payload.get("ok", False)
    stream = sys.stderr if bad else sys.stdout
    print("%s: %s" % ("REFUSED" if bad else "ok", payload.get("note")
                      or payload.get("error") or payload.get("state") or ""), file=stream)
    if payload.get("error") and payload.get("note"):
        print("  error: %s" % payload["error"], file=stream)
    for k in ("pid", "pgid", "state", "argv"):
        if payload.get(k) not in (None, ""):
            print("  %-5s %s" % (k, payload[k]), file=stream)
    # A service whose declared dependency is down still starts -- refusing would
    # mean the page that shows you what is broken is the page you cannot open --
    # so the gap is reported here, where a scripted deploy will see it, rather
    # than only implied by empty charts later.
    if payload.get("deps_down"):
        print("  deps  NOT UP: %s (it will start with an empty data set)"
              % ", ".join(payload["deps_down"]), file=sys.stderr)
    return 0 if not bad else (code or 1)


def cmd_start(args) -> int:
    keys = [args.service] if args.service else _keys()
    rc = 0
    for k in keys:
        code, payload = srv.start_service(k)
        print("== start %s" % k)
        rc |= _report(code, payload)
    return rc


def cmd_stop(args) -> int:
    keys = [args.service] if args.service else list(reversed(_keys()))
    rc = 0
    for k in keys:
        code, payload = srv.stop_service(k)
        print("== stop %s" % k)
        rc |= _report(code, payload)
    return rc


def cmd_disable(args) -> int:
    """Stop *and* remember, so an autostart cannot reverse the decision."""
    code, payload = srv.stop_service(args.service)
    print("== disable %s" % args.service)
    rc = _report(code, payload)
    rec = srv._read_proc_record(args.service)
    print("  disabled_by_user = %s" % rec.get("disabled_by_user"))
    return rc


def cmd_enable(args) -> int:
    """Clear the decision and start, which is the only thing that may clear it
    besides an explicit start."""
    srv._merge_proc_record(args.service, disabled_by_user=False)
    code, payload = srv.start_service(args.service)
    print("== enable %s" % args.service)
    return _report(code, payload)


def cmd_autostart(args) -> int:
    """What `@reboot` calls.

    Deliberately not the same as `start` over every service: a service whose
    config says `enabled = false`, or whose record says the user stopped it, is
    skipped rather than resurrected.  An autostart that overrides the operator
    is a bug that only shows up after a reboot, which is the worst possible
    time to discover it.
    """
    print("[labctl] autostart on %s at %s"
          % (srv.socket.gethostname(), srv.time.strftime("%Y-%m-%dT%H:%M:%S")))
    rc = 0
    for k in _keys():
        st = _one(k)
        if not st["enabled"]:
            print("  skip %-16s enabled=false in config" % k)
            continue
        if st["stopped_by_user"]:
            print("  skip %-16s stopped by the operator; will not reverse that" % k)
            continue
        if st["state"] in ("up", "starting"):
            print("  ok   %-16s already %s" % (k, st["state"]))
            continue
        code, payload = srv.start_service(k)
        print("  %-4s %-16s %s" % ("up" if payload.get("ok") else "FAIL", k,
                                   payload.get("note") or payload.get("error") or ""))
        rc |= 0 if payload.get("ok") else 1
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="labctl", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="one line per service, plus where state lives")
    p.add_argument("service", nargs="?")
    p.set_defaults(fn=cmd_status)

    for name, fn, helptext in (
            ("start", cmd_start, "start if not already running; refuses a disabled service"),
            ("stop", cmd_stop, "stop, and record that the operator wanted it stopped"),
            ("enable", cmd_enable, "clear the stopped-by-operator flag and start"),
            ("disable", cmd_disable, "stop, and keep it stopped across reboots")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("service", nargs="?")
        p.set_defaults(fn=fn)

    p = sub.add_parser("autostart", help="bring up everything the operator has not stopped")
    p.set_defaults(fn=cmd_autostart)

    args = ap.parse_args(argv)
    if args.cmd in ("enable", "disable") and not args.service:
        ap.error("%s requires a service name" % args.cmd)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
