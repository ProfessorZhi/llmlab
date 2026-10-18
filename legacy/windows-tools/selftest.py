#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for a running LLM Lab portal.

Two groups of checks:

  * **routes** -- every page and API endpoint answers with the expected status
    and, where it matters, the expected shape.
  * **gates** -- the things that must NOT work: path traversal, reading the API
    key, reading the SSH private key, reading an arbitrary file outside the
    publishable set.  A gate that is not exercised is a gate nobody has
    tested, so each one is attempted here on purpose.

Uses ``urllib``, not ``curl``: on this workstation curl was measured to return
``000`` against a socket that a bare TCP connect and urllib both reach
(found 2026-10-10), so it is not a trustworthy probe here.

    python scripts/selftest.py [--base http://localhost:8765]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request

# The console here is a GBK code page, and a response body that came back with
# U+FFFD in it is enough to make `print` raise UnicodeEncodeError.  That
# transforms "this response contained a bad byte" -- a finding worth reporting
# -- into a traceback in the middle of the run, which reads as "the test
# harness is broken" and gets the harness, not the finding, looked at.  Measured
# 2026-10-10: one such byte aborted the suite at check 100-odd of 126.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))


def fetch(base: str, path: str, timeout: float = 90.0):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, {}, ("%s: %s" % (type(exc).__name__, exc)).encode()


def post(base: str, path: str, headers: dict | None = None, timeout: float = 30.0):
    """POST with no body.  Returns (status, headers, body) like ``fetch``."""
    req = urllib.request.Request(base + path, data=b"", method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, {}, ("%s: %s" % (type(exc).__name__, exc)).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # ---------------------------------------------------------- routes ----
    st, _, body = fetch(base, "/")
    check("GET / serves the portal page", st == 200 and b"LLM Lab" in body, "status=%s" % st)

    # The V2 asset set.  Every one of these is fetched by index.html or by a
    # module import, so a missing file is a dead page rather than a cosmetic
    # fault -- and a 200 whose body is the SPA fallback would look identical
    # here, which is why the JS modules are checked for a real marker.
    stylesheets = ("tokens", "base", "layout", "components", "charts")
    core_modules = ("dom", "format", "icons", "api", "ui", "theme",
                    "chart", "router", "store", "bus", "sources")
    view_modules = ("_parts", "overview", "models", "benchmark", "compute",
                    "reports", "integrations", "settings")

    for name in stylesheets:
        p = "/assets/styles/%s.css" % name
        st, hd, b = fetch(base, p)
        check("GET %s" % p, st == 200 and len(b) > 400 and b"--" in b,
              "status=%s len=%d" % (st, len(b)))

    for name in core_modules:
        p = "/assets/core/%s.js" % name
        st, hd, b = fetch(base, p)
        ctype = (hd.get("Content-Type") or hd.get("content-type") or "")
        check("GET %s" % p,
              st == 200 and len(b) > 100 and b"export" in b and "javascript" in ctype,
              "status=%s len=%d ctype=%s" % (st, len(b), ctype))

    for name in view_modules:
        p = "/assets/views/%s.js" % name
        st, hd, b = fetch(base, p)
        check("GET %s" % p, st == 200 and len(b) > 80 and b"export" in b,
              "status=%s len=%d" % (st, len(b)))

    st, _, b = fetch(base, "/assets/app.js")
    check("GET /assets/app.js", st == 200 and len(b) > 500
          and b"core/router.js" in b, "status=%s len=%d" % (st, len(b)))

    st, _, b = fetch(base, "/api/healthz")
    check("GET /api/healthz", st == 200 and json.loads(b)["ok"] is True)

    st, _, b = fetch(base, "/api/lab")
    lab = json.loads(b) if st == 200 else {}
    check("GET /api/lab has node + endpoints",
          st == 200 and lab.get("node", {}).get("host") and lab.get("endpoints"),
          "status=%s" % st)
    check("/api/lab exposes no api key material",
          st == 200 and "STRATA_API_KEY" not in b.decode("utf-8", "replace")
          and "s" + "k-" not in b.decode("utf-8", "replace"))
    # Config faults found at load time are reported, not just printed.  The
    # check is that the list is *empty*: a non-empty one means the running
    # configuration differs from the one on disk, and the shape check below
    # makes sure the field exists at all -- a missing field would otherwise
    # read as "no warnings" through .get().
    check("/api/lab reports config warnings (and there are none)",
          st == 200 and isinstance(lab.get("config_warnings"), list)
          and not lab["config_warnings"],
          "warnings=%s" % (lab.get("config_warnings"),))

    st, _, b = fetch(base, "/api/live")
    live = json.loads(b) if st == 200 else {}
    check("GET /api/live returns strata + ssh + services",
          st == 200 and "strata" in live and "ssh" in live and "services" in live,
          "status=%s" % st)
    check("live strata reachable", bool(live.get("strata", {}).get("ok")),
          str(live.get("strata", {}).get("error")))
    check("live ssh probe ok", bool(live.get("ssh", {}).get("ok")),
          str(live.get("ssh", {}).get("error")))
    fp = live.get("ssh", {}).get("fingerprint", {})
    check("ssh host key matched the pinned fingerprint",
          fp.get("checked") and fp.get("match"),
          "pinned=%s entries=%s" % (fp.get("pinned"), list((fp.get("entries") or {}).keys())))

    st, _, b = fetch(base, "/api/benchmarks")
    runs = json.loads(b).get("runs", []) if st == 200 else []
    check("GET /api/benchmarks lists >= 1 run", st == 200 and len(runs) >= 1,
          "%d runs" % len(runs))

    if runs:
        run = runs[0]["run"]
        st, _, b = fetch(base, "/api/benchmarks/" + urllib.request.quote(run))
        doc = json.loads(b) if st == 200 else {}
        check("GET /api/benchmarks/<run> parses artifacts",
              st == 200 and doc.get("files") and (doc.get("bench") or doc.get("quality")),
              "status=%s" % st)
        for kind in ("bench", "concurrency", "quality", "files"):
            st, hdr, b = fetch(base, "/api/export/csv?run=%s&kind=%s"
                               % (urllib.request.quote(run), kind))
            ok = st == 200 and len(b) > 20
            if kind == "quality" and not doc.get("quality"):
                ok = st == 404           # honestly absent rather than an empty file
            check("GET /api/export/csv?kind=%s" % kind, ok, "status=%s" % st)
        st, _, b = fetch(base, "/api/export/zip?run=" + urllib.request.quote(run))
        check("GET /api/export/zip returns a zip",
              st == 200 and b[:2] == b"PK", "status=%s bytes=%d" % (st, len(b)))

    st, _, b = fetch(base, "/api/reports")
    reps = json.loads(b).get("reports", []) if st == 200 else []
    check("GET /api/reports lists >= 1 report", st == 200 and len(reps) >= 1,
          "%d reports" % len(reps))

    # Every listed report, preview and download both.  This loop is the
    # regression test for 2026-10-10: a report living in a configured data root
    # *outside* the checkout previewed 403 while the same report downloaded 200,
    # because only the preview route computed a display path and
    # ``p.relative_to(ROOT)`` raised for it.  Checking one report would have
    # missed it whenever the one sampled happened to live in the checkout --
    # which is exactly what the single-report version did.
    preview_bad, download_bad = [], []
    for rep in reps:
        p = str(rep.get("path") or "")
        if not p:
            continue
        st, _, b = fetch(base, "/report?p=" + urllib.request.quote(p))
        if not (st == 200 and (b"<h1" in b.lower() or b"<h2" in b.lower())):
            preview_bad.append("%s -> %s" % (p, st))
        st, hdr, b = fetch(base, "/download?p=" + urllib.request.quote(p))
        if not (st == 200 and "attachment" in hdr.get("Content-Disposition", "")):
            download_bad.append("%s -> %s" % (p, st))
    check("every listed report previews as HTML", not preview_bad, "; ".join(preview_bad))
    check("every listed report downloads as an attachment", not download_bad,
          "; ".join(download_bad))

    st, _, b = fetch(base, "/api/env")
    check("GET /api/env reports the toolchain", st == 200 and b"python" in b)

    # ------------------------------------------------- unified integration ----
    nav = lab.get("nav", {})
    groups = {g.get("group"): g for g in (nav.get("groups") or [])}
    check("/api/lab carries the unified navigation",
          set(groups) >= {"本 Portal", "Strata 原生界面", "EvalScope", "Grafana"},
          "groups=%s" % list(groups))

    native = groups.get("Strata 原生界面", {}).get("items") or []
    check("native UI exposes exactly Chat / Monitor / About",
          [i.get("key") for i in native] == ["chat", "monitor", "about"],
          str([i.get("key") for i in native]))
    # The deep links are hash routes on a single page.  A path like /monitor
    # would 404 on the node, so a panel whose URL is not the base plus a hash
    # is a broken link that still *looks* well-formed.
    #
    # The earlier version of this check required the URL to end in `18081` --
    # the port of a tunnel that has since been retired (2026-10-10).  It
    # therefore measured the deployment it was written against rather than the
    # property it claimed: against the same code reached directly on :8080 it
    # reported FAIL, on the panel that was working.  What is actually being
    # asserted is the *shape* of each link, so that is what is written here --
    # absolute or explicitly unexposed, and the two deep links carrying their
    # hash.
    def _shape_ok(items):
        for it in items:
            u = it.get("url") or ""
            if not u:                      # "not exposed" is a legal state
                continue
            if "://" not in u:             # a bare path is a link that 404s
                return False, "%s -> %r is not absolute" % (it.get("key"), u)
            want = {"monitor": "#monitor", "about": "#about"}.get(it.get("key"))
            if want and not u.endswith(want):
                return False, "%s -> %r does not end in %s" % (it.get("key"), u, want)
        return True, ""

    ok_shape, why = _shape_ok(native)
    check("native panel URLs are absolute, with hash routes for the deep links",
          ok_shape, why or str([i.get("url") for i in native]))

    # The entry-address rule: a loopback URL is only honest when the portal
    # itself is loopback-bound, because then the reader is necessarily on this
    # machine.  The two facts are asserted together -- the URL alone cannot
    # distinguish "correct for a local console" from "a link that will fail on
    # somebody else's machine".
    #
    # The field is required, not defaulted.  The first version of this check
    # read `...get("portal_host") or "localhost"` and the portal did not publish
    # the field at all, so the check concluded "loopback-bound" from a missing
    # value and passed without examining anything -- a check that could not
    # fail.  It now fails on absence, which is the only way it can mean
    # something.
    portal_host = (lab.get("lab") or {}).get("portal_host")
    check("/api/lab publishes the portal's own binding",
          isinstance(portal_host, str) and portal_host,
          "portal_host=%r" % (portal_host,))
    portal_is_loopback = portal_host in ("localhost", "localhost", "::1")
    bad_loop = [i.get("key") for i in native
                if i.get("url")
                and ("localhost" in i["url"] or "localhost" in i["url"])
                and not portal_is_loopback]
    check("no loopback link is published by a portal that is not loopback-bound",
          bool(portal_host) and not bad_loop,
          "unjustified=%s portal_host=%s" % (bad_loop, portal_host))

    st, _, b = fetch(base, "/api/strata")
    con = json.loads(b) if st == 200 else {}
    check("GET /api/strata returns model_api + gpu as separate blocks",
          st == 200 and isinstance(con.get("model_api"), dict)
          and isinstance(con.get("gpu"), dict),
          "status=%s" % st)
    check("model API health is reported independently of telemetry",
          "ok" in (con.get("model_api") or {}),
          str((con.get("model_api") or {}).get("error")))
    check("GPU telemetry states availability and names its source",
          "available" in (con.get("gpu") or {})
          and (con["gpu"].get("source") or con["gpu"].get("reason")),
          str(con.get("gpu", {}).get("source") or con.get("gpu", {}).get("reason")))
    check("/api/strata never carries the API key itself",
          st == 200 and "STRATA_API_KEY" not in b.decode("utf-8", "replace"),
          "key_present=%s fingerprint=%s"
          % (con.get("key_present"), con.get("key_sha256_12")))

    tun = (con.get("native") or {}).get("tunnel") or {}
    if tun.get("enabled"):
        # A listening port is not the same claim as "our tunnel is up": this
        # workstation has an unrelated service on 18080 answering 200 on every
        # path, so the distinction is measured, not assumed.
        check("tunnel state separates listening from owned",
              set(tun) >= {"listening", "pid_alive", "owned", "foreign"}
              and tun["owned"] == bool(tun["listening"] and tun["pid_alive"]),
              json.dumps(tun, ensure_ascii=False))

    st, _, b = fetch(base, "/api/evaluation")
    ev = json.loads(b) if st == 200 else {}
    check("GET /api/evaluation summarises the benchmark artifacts",
          st == 200 and "runs" in ev and "coverage" in ev and "gaps" in ev,
          "status=%s" % st)
    check("model count is derived from the runs, not hard-coded",
          isinstance(ev.get("model_count"), int)
          and ev["model_count"] == len(set(ev.get("models") or [])),
          "count=%s models=%s" % (ev.get("model_count"), ev.get("models")))
    cov = ev.get("coverage") or {}
    check("coverage is reported against an explicit expectation",
          cov.get("expected") == cov.get("expected_per_run", 0) * len(ev.get("runs") or []),
          json.dumps(cov))
    check("gaps are listed rather than folded into the successes",
          isinstance(ev.get("gaps"), list), "%d gap(s)" % len(ev.get("gaps") or []))
    # An absent EvalScope must be *reported*, not rendered as "0 reports", which
    # is what a status-code-only probe of its SPA would conclude.
    esc_ = ev.get("evalscope") or {}
    check("EvalScope state distinguishes up / starting / down",
          esc_.get("state") in ("up", "starting", "down"),
          json.dumps({k: esc_.get(k) for k in ("state", "up", "pid_alive", "port")}))
    if esc_.get("state") == "up":
        rep = esc_.get("reports") or {}
        check("EvalScope report API was shape-checked, not just 200-checked",
              isinstance(rep.get("ok"), bool),
              json.dumps({k: rep.get(k) for k in ("ok", "total", "reason")},
                         ensure_ascii=False))

    st, _, b = fetch(base, "/api/live")
    live2 = json.loads(b) if st == 200 else {}
    check("/api/live now carries tunnel + evalscope state",
          st == 200 and isinstance(live2.get("tunnel"), dict)
          and isinstance(live2.get("evalscope"), dict), "status=%s" % st)

    # -------------------------------------------------------- no storm ----
    # §6 of the brief: "multiple rapid refreshes must not cause request
    # storms".  The client coalesces (single-flight in core/store.js, and its
    # own min-interval); the server coalesces too, because three tabs is not a
    # client-side fix.  What is observable from out here is that N simultaneous
    # calls to the one endpoint that shells out -- /api/v2/live, which runs the
    # SSH probe -- all answer, none 500s, and the endpoint is still answering
    # immediately afterwards.  A server that let every caller run its own probe
    # would fail one of those three, not necessarily all at once.
    def one():
        st, _, b = fetch(base, "/api/v2/live", timeout=120)
        try:
            doc = json.loads(b)
        except Exception:  # noqa: BLE001
            doc = {}
        return st, bool(doc.get("ok")), (doc.get("data") or {}).get("ssh", {}).get("at")

    # negative control first: if a single call cannot answer, the burst proves
    # nothing about coalescing -- it only proves the node is unreachable.
    single = one()
    check("control: one /api/v2/live call answers before the storm test",
          single[0] == 200 and single[1], "status=%s ok=%s" % (single[0], single[1]))

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        burst = list(pool.map(lambda _: one(), range(6)))
    burst_s = time.time() - t0
    ok_count = sum(1 for st, ok, _ in burst if st == 200 and ok)
    check("6 concurrent /api/v2/live calls all answer",
          ok_count == 6,
          "%d/6 ok, %d distinct status(es), %.2fs wall"
          % (ok_count, len({s for s, _, _ in burst}), burst_s))
    # The cache is shared, so the probes behind those six collapse to very few
    # timestamps -- one per producer run, not one per caller.
    stamps = {t for _, _, t in burst if t}
    check("  they share the cached SSH probe rather than each running one",
          len(stamps) <= 2,
          "%d distinct probe timestamp(s) across 6 callers" % len(stamps))
    st, _, b = fetch(base, "/api/v2/live", timeout=120)
    check("  and the endpoint still answers straight after the burst",
          st == 200, "status=%s" % st)

    # ------------------------------------------------------- v2 contract ----
    # The v2 envelope exists because HTTP 200 is not a health claim on this
    # workstation: an unrelated service on :18080 answers 200 to *every* path.
    # So the assertions here are about the envelope's contents, never the code.
    V2_ROUTES = ("/api/v2/overview", "/api/v2/live",
                 "/api/v2/integrations", "/api/v2/settings")
    v2_bodies = {}
    for route in V2_ROUTES:
        st, _, b = fetch(base, route)
        doc = json.loads(b) if b[:1] == b"{" else {}
        v2_bodies[route] = (st, doc)
        check("GET %s returns a v2 envelope" % route,
              st == 200 and all(k in doc for k in
                                ("ok", "data", "error", "ts", "source", "partial")),
              "status=%s keys=%s" % (st, sorted(doc)[:8]))
        check("  %s names its source" % route,
              isinstance(doc.get("source"), str) and doc["source"].startswith("portal:"),
              repr(doc.get("source")))

    # Structure, not status: a payload that parses but is the wrong document
    # must be rejected.  This is the exact failure the :18080 service produces.
    _, ov = v2_bodies["/api/v2/overview"]
    ovd = ov.get("data") or {}
    check("v2/overview carries node+strata+alerts+tunnel+evalscope",
          all(k in ovd for k in ("node", "strata", "alerts", "tunnel", "evalscope")),
          "keys=%s" % sorted(ovd))
    check("v2/overview strata is shape-checked (model_api + gpu)",
          isinstance(ovd.get("strata"), dict)
          and "model_api" in ovd["strata"] and "gpu" in ovd["strata"],
          "strata keys=%s" % sorted(ovd.get("strata") or {}))
    check("v2/overview reports the v1-shaped alert list",
          isinstance(ovd.get("alerts"), list)
          and all(isinstance(a, dict) and {"level", "title", "detail", "source"} <= set(a)
                  for a in ovd["alerts"]),
          "%d alert(s)" % len(ovd.get("alerts") or []))

    _, lv = v2_bodies["/api/v2/live"]
    lvd = lv.get("data") or {}
    check("v2/live carries samples + services for a light poll",
          isinstance(lvd.get("samples"), list) and isinstance(lvd.get("services"), list),
          "samples=%d services=%d" % (len(lvd.get("samples") or []),
                                      len(lvd.get("services") or [])))
    check("v2/live evalscope state is the three-state model",
          (lvd.get("evalscope") or {}).get("state") in ("up", "starting", "down"),
          str((lvd.get("evalscope") or {}).get("state")))

    _, ig = v2_bodies["/api/v2/integrations"]
    items = ((ig.get("data") or {}).get("items")) or []
    ALLOWED = {"connected", "available", "not_installed", "disabled", "planned", "error"}
    check("v2/integrations statuses come from the declared vocabulary",
          bool(items) and all(i.get("status") in ALLOWED for i in items),
          "statuses=%s" % sorted({i.get("status") for i in items}))
    check("v2/integrations every item carries its evidence",
          bool(items) and all(isinstance(i.get("evidence"), str) and i["evidence"]
                              for i in items),
          "%d item(s)" % len(items))
    check("v2/integrations states Grafana as not integrated",
          any(i.get("key") == "grafana" and i.get("status") in ("planned", "not_installed")
              for i in items),
          "grafana=%s" % next((i.get("status") for i in items
                               if i.get("key") == "grafana"), None))

    # ------------------------------------------------------ v2 credential ----
    _, se = v2_bodies["/api/v2/settings"]
    sed = se.get("data") or {}
    raw_settings = json.dumps(se, ensure_ascii=False)
    check("v2/settings leaks no key material",
          not any(t in raw_settings for t in
                  ("s" + "k-", "PRIVATE KEY", "OPENSSH", "BEGIN ", "api_key\"")),
          "%d bytes scanned" % len(raw_settings))
    check("v2/settings endpoint rows carry only the disclosed fields",
          bool(sed.get("endpoints")) and all(
              set(e) <= {"key", "label", "kind", "base_url", "enabled",
                         "key_present", "key_sha256_12"} for e in sed["endpoints"]),
          "keys=%s" % sorted({k for e in (sed.get("endpoints") or []) for k in e}))
    check("v2/settings identifies the loaded key by digest, not by value",
          all((e.get("key_sha256_12") is None) == (not e.get("key_present"))
              for e in (sed.get("endpoints") or [])),
          str([(e["key"], e["key_present"], e["key_sha256_12"])
               for e in (sed.get("endpoints") or [])]))
    # The SSH key is a *path*.  If the contents were ever read, this would be
    # a base64 blob starting with the OpenSSH marker instead.
    check("v2/settings exposes the ssh key as a path, never its contents",
          isinstance((sed.get("node") or {}).get("ssh_key"), (str, type(None))),
          repr((sed.get("node") or {}).get("ssh_key")))

    # An unknown v2 route must be a 404.  A fallback that answers 200 here is
    # how a client ends up rendering a different service's page as our data.
    st, _, b = fetch(base, "/api/v2/definitely-not-a-route")
    check("unknown /api/v2/* is a 404, not a 200 fallback",
          st == 404, "status=%s body=%s" % (st, b[:80].decode("utf-8", "replace")))

    # --------------------------------------------------------- front end ----
    # Static checks over the served page.  These catch the regressions a
    # screenshot cannot: a dead asset path, a CDN that crept back in, a view
    # that stopped exporting what the router imports.
    st, _, page = fetch(base, "/")
    html = page.decode("utf-8", "replace")
    check("index.html links the V2 stylesheet set",
          all('/assets/styles/%s.css' % n in html for n in stylesheets),
          "")
    check("index.html no longer links the retired V1 stylesheet",
          "/assets/styles.css" not in html, "")
    check("index.html pulls only same-origin assets (no CDN)",
          "//cdn" not in html and "https://" not in html.replace("https://claude.com", ""),
          "")
    check("index.html builds the shell the router expects",
          all(m in html for m in ('id="nav"', 'id="view"', 'id="topbar"')) or
          all(m in html for m in ('id="nav"', 'id="view"', 'class="topbar"')),
          "")
    check("index.html ships a no-JS fallback that names the raw API",
          "noscript" in html and "/api/v2/live" in html, "")

    # Every route id the router declares must have a module, and vice versa.
    st, _, src = fetch(base, "/assets/core/router.js")
    router_src = src.decode("utf-8", "replace")
    declared = set()
    for line in router_src.splitlines():
        line = line.strip()
        if line.startswith("{ id: '"):
            declared.add(line.split("'")[1])
    check("router declares the seven V2 routes",
          declared == {"overview", "models", "benchmark", "compute",
                       "reports", "integrations", "settings"},
          "declared=%s" % sorted(declared))
    for rid in sorted(declared):
        st, _, _ = fetch(base, "/assets/views/%s.js" % rid)
        check("  route %s resolves to a view module" % rid, st == 200, "status=%s" % st)

    # Each view must export the single entry point the router calls.
    for rid in sorted(declared):
        st, _, b = fetch(base, "/assets/views/%s.js" % rid)
        body = b.decode("utf-8", "replace") if st == 200 else ""
        check("  views/%s.js exports render()" % rid,
              "export async function render" in body or "export function render" in body,
              "len=%d" % len(body))

    # /api/v2/settings runs several subprocess probes in sequence and measured
    # 7-19 s on an idle portal, so the settings page must paint its local
    # sections *before* awaiting it.  It used to paint an all-skeleton page and
    # wait, which meant a slow portal gave the user (and the screenshot sweep) a
    # wall of skeletons for 90 s -- a real defect that a sweep checking only for
    # console errors and overflow recorded as a pass.  This pins the invariant:
    # draw() before load(), and a failed load re-draws with the local sections
    # intact instead of replacing the page.
    st, _, b = fetch(base, "/assets/views/settings.js")
    sset = b.decode("utf-8", "replace") if st == 200 else ""
    check("settings draws before it awaits the slow config endpoint",
          "draw(null);" in sset and "draw(null, err)" in sset,
          "status=%s len=%d" % (st, len(sset)))
    if "draw(null);" in sset and "load();" in sset:
        check("  and the first draw precedes the request",
              sset.index("draw(null);") < sset.index("\n  load();"),
              "draw at %d, load at %d" % (sset.index("draw(null);"), sset.index("load();")))

    # ----------------------------------------------------------- gates ----
    # Every one of these MUST be refused.  A 200 here is a security defect.
    gates = [
        ("path traversal above the root", "/file?p=../../../../Windows/win.ini"),
        ("absolute path", "/file?p=C:/Windows/win.ini"),
        ("the API key file", "/file?p=config/secrets.env"),
        ("the API key file, url-encoded", "/file?p=config%2Fsecrets.env"),
        ("the SSH private key", "/file?p={{LAB_SSH_KEY_PATH}}"),
        ("a file outside the publishable set", "/file?p=portal/server.py"),
        ("that same file via /download", "/download?p=portal/server.py"),
        ("the report route on a source file", "/report?p=scripts/bootstrap_remote_access.py"),
        ("a run name that climbs out", "/api/benchmarks/..%2f..%2fconfig"),
        ("a run name that climbs out (zip)", "/api/export/zip?run=../../config"),
        # The asset route is a second, independent reader of the same tree the
        # /file route guards -- added in V2 when the front end became a module
        # tree with nesting.  Both spellings are attempted because a check that
        # only normalises ``..`` is bypassed by one that does not normalise the
        # encoded form, and vice versa.
        ("an asset path that climbs out", "/assets/../server.py"),
        ("an asset path that climbs out, encoded", "/assets/%2e%2e/server.py"),
        ("an asset path that climbs out of a subdir", "/assets/styles/../../server.py"),
        ("the API key file read as an asset", "/assets/../config/secrets.env"),
        ("the SSH private key read as an asset", "/assets/../{{LAB_SSH_KEY_PATH}}"),
    ]
    for name, path in gates:
        st, _, b = fetch(base, path)
        check("refused: %s" % name, st in (403, 404),
              "status=%s body=%s" % (st, b[:80].decode("utf-8", "replace")))

    # The one route that *does* things, so its gates get the same treatment.
    # POST without the header is the CSRF case: a cross-origin page can reach a
    # loopback service, but it cannot set a custom header without a preflight
    # this server never answers.
    st, _, b = post(base, "/api/service/start?key=evalscope", headers={})
    check("refused: POST /api/service/start without X-Requested-With",
          st == 403, "status=%s body=%s" % (st, b[:90].decode("utf-8", "replace")))

    st, _, b = post(base, "/api/service/start?key=definitely-not-a-service",
                    headers={"X-Requested-With": "llm-lab-portal"})
    body = json.loads(b) if b[:1] == b"{" else {}
    check("refused: POST a service name that is not in the allowlist",
          st == 404 and not body.get("ok"),
          "status=%s managed=%s" % (st, body.get("managed")))

    # A positive control for that allowlist: the *configured* name must reach
    # the launcher.  It is safe to call because the launcher refuses to start a
    # second copy when the port is already bound -- so on a running lab this
    # exercises the duplicate guard instead of spawning anything.
    st, _, b = post(base, "/api/service/start?key=evalscope",
                    headers={"X-Requested-With": "llm-lab-portal"})
    body = json.loads(b) if b[:1] == b"{" else {}
    check("control: the allowlisted service name reaches the launcher",
          st == 200 and body.get("ok") is True
          and body.get("state") in ("up", "starting"),
          "status=%s %s" % (st, json.dumps({k: body.get(k) for k in
                                            ("started", "state", "note", "error")},
                                           ensure_ascii=False)))

    # a positive control, so the gate checks above are known to be able to pass
    st, _, b = fetch(base, "/api/benchmarks/does-not-exist")
    check("control: unknown run is a clean 404", st == 404, "status=%s" % st)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
