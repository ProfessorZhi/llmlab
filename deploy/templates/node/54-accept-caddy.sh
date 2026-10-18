#!/usr/bin/env bash
# Reverse-proxy acceptance.  Runs on the node, against the LAN address the
# browser will use -- not against loopback, because the whole point of this
# component is the path from outside, and a check that dials localhost tests
# Caddy's config while leaving the listener, the bind address and the TLS chain
# unexamined.
#
# WHY THIS IS A SCRIPT AND NOT A FEW CURLS.  Three of the properties the brief
# names cannot be established by reading the Caddyfile, and one of them is
# actively contradicted by it:
#
#   * "nothing is anonymous" is a property of what Caddy *does* with a request
#     that carries no Authorization header.  A `basic_auth` line in a file is a
#     statement of intent.
#   * "the bind is constrained" was FALSE in the first version of the template,
#     which named a host in the site address and therefore bound every address
#     on the port.  Nothing but a socket read says otherwise.
#   * "client-supplied identity headers are destroyed" is invisible end to end
#     while every endpoint ignores them, which is exactly the case today.  It is
#     checked against Caddy's own adaptation of the config -- the machine's
#     reading of the file -- and the limitation of that check is written down
#     rather than glossed.
#
# Read-mostly.  The two POSTs are negative controls aimed at a service key that
# does not exist, so a failure to refuse them costs nothing but is still caught;
# the real-service mutation is asserted not to have happened.
#
# No credential is printed, and this script does not load them into its own
# environment: the Python below opens the 600 file itself.  Anything on a
# terminal ends up in a scrollback buffer and in whatever logs the terminal.
set -euo pipefail

PY=/usr/bin/python3
CADDY_BASE={{LAB_ROOT}}/services/caddy
ADDR={{LAB_HOST}}
PORT=8443

echo "=== sockets: the listener this deployment intended, and the ones it must not have ==="
"$PY" - <<'PY'
import socket, sys
# Bare TCP, no ICMP: a refused port and a filtered one are different faults and
# a curl status cannot tell them apart -- but a connect() error string does.
for host in ("{{LAB_HOST}}", "localhost", "{{LAB_PEER_HOST}}"):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, 8443)); print("  tcp %-15s:8443 -> connected" % host)
    except OSError as e:
        print("  tcp %-15s:8443 -> %s" % (host, e))
    finally:
        s.close()
PY

echo
"$PY" - <<'PY'
import json, os, re, ssl, subprocess, sys, urllib.error, urllib.request

ADDR, PORT = "{{LAB_HOST}}", 8443
BASE = "https://%s:%d" % (ADDR, PORT)
CADDY_BASE = "{{LAB_ROOT}}/services/caddy"
AUTH_ENV = "{{LAB_ROOT}}/services/secrets/caddy-auth.env"
ACCESS_LOG = os.path.join(CADDY_BASE, "logs", "access.log")

bad, notes, n_ck = [], [], [0]


def ck(label, cond, detail=""):
    n_ck[0] += 1
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not cond:
        bad.append(label)


def note(label, detail):
    print("  ..   %s  -- %s" % (label, detail))
    notes.append("%s: %s" % (label, detail))


def creds():
    """Read the two credential pairs.  Missing file is a hard stop, not a skip:
    an acceptance run that cannot authenticate would report every protected
    route as correctly protected."""
    env = {}
    with open(AUTH_ENV) as fh:
        for line in fh:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                env[k] = v
    out = {}
    for who in ("ADMIN", "VIEWER"):
        u = env.get("CADDY_%s_USER" % who, "")
        p = env.get("CADDY_%s_PASSWORD" % who, "")
        if not u or not p:
            sys.exit("REFUSING: %s does not hold both credentials" % AUTH_ENV)
        out[who.lower()] = (u, p)
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect, so the 3xx itself can be inspected.

    urllib follows redirects by default, which silently converts "the proxy
    answers 308 to /strata/ with the right Location" into "the page loads" --
    the same response a working redirect and a missing one both produce when the
    follow succeeds.  Returning None here makes urllib raise, and the caller
    sees the 3xx and its headers.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def req(method, path, auth=None, headers=None, verify=True, follow=True):
    """Return (status, body_bytes, response_headers).

    The headers come back as the message object itself, not a dict.  A dict of
    them is case-sensitive, which is wrong twice over here: header names are
    case-insensitive by definition, and Go canonicalises them -- Caddy emits
    ``Www-Authenticate``, so a dict lookup for ``WWW-Authenticate`` misses a
    header that is present and reads as "the proxy sends no auth challenge".
    """
    r = urllib.request.Request(BASE + path, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if auth:
        import base64
        tok = base64.b64encode(("%s:%s" % auth).encode()).decode()
        r.add_header("Authorization", "Basic " + tok)
    if verify:
        ctx = ssl.create_default_context(cafile=os.path.join(
            CADDY_BASE, "data", "pki", "authorities", "local", "root.crt"))
    else:
        # Only for the checks that must reach the *proxy* rather than test the
        # chain; the chain has its own check above and it is not skipped.
        ctx = ssl._create_unverified_context()
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:  # noqa: BLE001
        return None, ("%s: %s" % (type(e).__name__, e)).encode(), {}


c = creds()

print("=== the bind, checked as a socket rather than read off the config ===")
# A Caddy site address names a host for certificate selection and request
# matching; it does NOT decide which socket opens.  The first version of the
# template read as though it did and consequently listened on every address, so
# this asserts the negative: a real local address other than the chosen one must
# refuse.  localhost is used because it is always present -- an address that
# happens to be absent refuses for the wrong reason and would pass vacuously.
import socket as _s
s = _s.socket(); s.settimeout(4)
try:
    s.connect(("localhost", PORT))
    ck("loopback:8443 is NOT listening (the bind is constrained)", False,
       "connected -- the listener is on every address")
except OSError as e:
    ck("loopback:8443 is NOT listening (the bind is constrained)", True, e)
finally:
    s.close()

print()
print("=== TLS: the chain must validate against this deployment's own CA ===")
# "Take care of certificates properly; do not simply disable verification for
# convenience" -- the way to satisfy both halves is to verify, for real, against
# the root this Caddy signed with.  If this passes, the browser check is a trust
# anchor install and not a leap of faith.
#
# Credentials are passed because this route is behind the proxy: the first
# version asked for /api/healthz with no Authorization and read the resulting
# 401 as a TLS failure.  It was not one -- a 401 is a full response over a
# verified connection -- but a check whose failure mode is indistinguishable
# from success is worse than no check, so it now requires the 200.
try:
    st, body, _ = req("GET", "/api/healthz", auth=c["viewer"], verify=True)
    ck("chain validates against data/pki/.../root.crt (hostname included)", st == 200,
       "status %s" % st)
except Exception as e:  # noqa: BLE001
    ck("chain validates against data/pki/.../root.crt (hostname included)", False, e)

print()
print("=== authentication: an unauthenticated API request must be answered by the")
print("    proxy, not by the portal -- so the body must not be a portal response ===")
st, body, hdrs = req("GET", "/api/healthz", verify=False)
ck("no credentials -> 401", st == 401, "status %s" % st)
ck("no credentials -> WWW-Authenticate present (it is the proxy answering)",
   "basic" in (hdrs.get("WWW-Authenticate") or "").lower(),
   repr(hdrs.get("WWW-Authenticate")))
ck("no credentials -> the portal's own error shape is absent",
   b"no route for" not in body and b'"version"' not in body,
   body[:80])

st, body, _ = req("GET", "/api/healthz", auth=c["viewer"], verify=False)
ck("viewer credentials -> 200", st == 200, "status %s" % st)
try:
    j = json.loads(body)
    ck("viewer response is the portal's healthz, with a version",
       j.get("ok") is True and isinstance(j.get("version"), str), j)
except Exception as e:  # noqa: BLE001
    ck("viewer response is the portal's healthz, with a version", False, e)

st, body, _ = req("GET", "/api/v2/overview", auth=c["admin"], verify=False)
ck("admin credentials -> 200 on a v2 read route", st == 200, "status %s" % st)
try:
    j = json.loads(body)
    ck("v2 route answers in the documented envelope",
       isinstance(j, dict) and "ok" in j and "data" in j and "ts" in j,
       sorted(j.keys()))
except Exception as e:  # noqa: BLE001
    ck("v2 route answers in the documented envelope", False, e)

print()
print("=== authorisation: a viewer must not reach the state-changing surface ===")
# The key is deliberately one that does not exist.  The question here is whether
# the request is *opened* at all, and a name nothing answers keeps the blast
# radius of a bug at zero -- while still failing loudly if the route turns out
# to be reachable.
st, body, _ = req("POST", "/api/service/stop?key=__probe__", auth=c["viewer"],
                  headers={"X-Requested-With": "llm-lab-portal"}, verify=False)
ck("viewer POST /api/service/stop -> 401 (stopped at the proxy)", st == 401,
   "status %s" % st)
ck("viewer POST never reached the portal's handler", b"no POST route" not in body
   and b"the console does not switch" not in body, body[:80])

st, body, _ = req("POST", "/api/service/stop?key=__probe__", auth=c["admin"],
                  verify=False)
ck("admin POST without X-Requested-With -> 403 from the *portal*"
   " (proves the credential opens the route)", st == 403, "status %s" % st)
ck("that 403 is the portal's cross-origin gate, not a proxy refusal",
   b"X-Requested-With" in body, body[:100])

# The real-service variant, with the state read on both sides.  A refusal that
# is only observed as a status code is a refusal that could have half-happened.
def grafana_state():
    p = subprocess.run(["/usr/bin/python3", "deploy/templates/labctl.py", "status"],
                       cwd="{{LAB_ROOT}}/llm-lab", capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if line.startswith("grafana"):
            return line.split()
    return None


before = grafana_state()
st, body, _ = req("POST", "/api/service/stop?key=grafana", auth=c["viewer"],
                  headers={"X-Requested-With": "llm-lab-portal"}, verify=False)
after = grafana_state()
ck("viewer POST .../key=grafana -> 401", st == 401, "status %s" % st)
ck("and grafana's state is untouched by the attempt", before == after,
   "%s -> %s" % (before, after))

print()
print("=== identity headers: a client must not be able to name itself ===")
st, body, _ = req("GET", "/api/healthz", headers={"X-Forwarded-User": "admin"},
                  verify=False)
ck("X-Forwarded-User with no credentials -> still 401", st == 401, "status %s" % st)

# End-to-end this is invisible: no endpoint reads such a header, so a request
# with one and a request without are indistinguishable in the response.  The
# check that CAN fail is on Caddy's adaptation of its own config -- if the strip
# were dropped, the adapted routes would not carry the deletion ops.  What this
# does not prove is stated plainly rather than implied.
adapt = subprocess.run([os.path.join(CADDY_BASE, "caddy"), "adapt",
                        "--config", os.path.join(CADDY_BASE, "Caddyfile"),
                        "--adapter", "caddyfile"],
                       capture_output=True, text=True)
if adapt.returncode != 0:
    ck("the config adapts", False, adapt.stderr[:200])
else:
    ck("the config adapts", True)
    # Read the compiled JSON, not the text.  The directive is emitted as a
    # `headers` handler with ``request.delete`` -- `{"handler":"headers",
    # "request":{"delete":["X-Forwarded-User"]}}`` -- and the first version of
    # this check searched for ``"op": "delete"``, which is how the same idea
    # looks in the *proxy* handler and appears nowhere for a header rewrite.
    # It reported zero deletions on a config that deletes all seven.
    deleted = set()

    def _collect(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "delete" and isinstance(v, list):
                    deleted.update(v)
                else:
                    _collect(v)
        elif isinstance(o, list):
            for v in o:
                _collect(v)

    try:
        _collect(json.loads(adapt.stdout))
    except Exception as e:  # noqa: BLE001
        ck("the adapted config parses", False, e)
    names = ["X-Forwarded-User", "X-Forwarded-Email", "X-Forwarded-Groups",
             "Remote-User", "X-Remote-User", "X-Auth-Request-User",
             "X-Auth-Request-Email"]
    missing = [n for n in names if n not in deleted]
    ck("every identity header is compiled into a request-header deletion",
       not missing, "missing from request.delete: %s" % missing)
    note("scope of this check", "compiled-in deletion is proven; a live response "
         "cannot show the strip because no route echoes headers")

print()
print("=== routing: three things are published behind the prefix, and the rest is")
print("    not published at all ===")
st, body, _ = req("GET", "/grafana/", auth=c["viewer"], verify=False)
ck("viewer reaches /grafana/ (Grafana answers, not a 502)",
   st in (200, 302), "status %s" % st)

st, body, _ = req("GET", "/strata/", auth=c["viewer"], verify=False)
ck("viewer reaches /strata/ and it is Strata's console",
   st == 200 and b"web/app.js" in body, "status %s, %d bytes" % (st, len(body)))

# The decisive one for the prefix strip.  This path exists in Strata's console
# and nowhere in the portal, and it needs no API key, so a 200 with Strata's own
# source in it cannot be produced by any route except the one under test: the
# portal has no /strata/* handler, so a missed strip or an unhandled prefix
# returns the portal's 404 and is caught here rather than in a browser.
st, body, _ = req("GET", "/strata/web/app.js", auth=c["viewer"], verify=False)
ck("the prefix is stripped: Strata's asset is served at /strata/web/app.js",
   st == 200 and b"v1/chat/completions" in body,
   "status %s, %d bytes" % (st, len(body)))

# Relative resolution is against the page's directory, so /strata with no
# trailing slash resolves the console's own fetches one segment too high.  The
# redirect is what keeps that from happening, and it is asserted rather than
# trusted.
st, body, hdrs = req("GET", "/strata", auth=c["viewer"], verify=False, follow=False)
loc = hdrs.get("Location") or ""
ck("/strata redirects to /strata/ (relative fetches would otherwise escape)",
   st in (301, 302, 307, 308) and loc.rstrip("/").endswith("/strata"),
   "status %s, Location %r" % (st, loc))

st, body, _ = req("GET", "/v1/models", auth=c["admin"], verify=False)
ck("Strata's API is NOT published at the root: /v1/models is the portal's 404",
   st == 404 and b"no route for" in body, "status %s, %s" % (st, body[:70]))

st, body, _ = req("GET", "/prometheus/api/v1/query?query=up", auth=c["admin"],
                  verify=False)
ck("Prometheus is NOT published: its API is the portal's 404",
   st == 404 and b"no route for" in body, "status %s, %s" % (st, body[:70]))

st, body, _ = req("GET", "/evalscope/", auth=c["viewer"], verify=False)
ck("EvalScope is NOT published (its build emits root-absolute asset paths)",
   st == 404 and b"no route for" in body, "status %s, %s" % (st, body[:70]))

st, body, _ = req("GET", "/api/service", auth=c["admin"], verify=False)
ck("admin reaches the admin-only path without a 401 or a 502",
   st not in (401, 502), "status %s" % st)

# The front door is switchable by the host and NOT by the console, and this is
# the check that separates those two ideas.  Caddy is `managed` -- the cron hook
# must be able to revive it -- and `portal_managed = false`, because the reply to
# "stop the proxy" travels through the proxy: the operator would watch the page
# die and lose the button that turns it back on.  Run with admin credentials
# specifically, so a 401 from the proxy cannot be mistaken for the refusal.
st, body, _ = req("POST", "/api/service/stop?key=caddy", auth=c["admin"],
                  headers={"X-Requested-With": "llm-lab-portal"}, verify=False)
ck("even admin cannot stop the proxy from the console", st == 403, "status %s" % st)
ck("and the refusal is the portal's own, naming the reason",
   b"does not switch" in body, body[:120])
st, body, _ = req("GET", "/api/healthz", auth=c["admin"], verify=False)
ck("the proxy is still up after that attempt", st == 200, "status %s" % st)

print()
print("=== the browser is never handed a loopback address (Section 2) ===")
# The failure this guards against is specific: a page that tells a remote
# browser to open http://localhost:3000 sends it to the *viewer's own* machine,
# where the connection fails in a way indistinguishable from the service being
# down.
#
# The portal publishes TWO addresses per component on purpose.  `url` is the
# internal one and saying so is the point -- a console that shows where a
# service listens on the node is informative, and Section 2 asks for the two to
# be decoupled rather than for one of them to be hidden.  What the browser is
# *invited to open* is `entry.url`, and that is what must not be loopback.
#
# This check is what surfaced the defect it now guards: `entry_url` reasoned
# "the portal binds loopback, therefore the reader is on this host", which
# stopped being true the moment a proxy took the front door.  Every component
# without a public URL got handed a loopback address.
st, body, _ = req("GET", "/api/v2/integrations", auth=c["admin"], verify=False)
try:
    doc = json.loads(body)
    data = doc.get("data") or {}
    # `items`, not `services`.  The envelope's payload key is not guessable from
    # the route name and this check found that out by reporting six failures
    # against a payload that was correct -- the shape was wrong, not the data.
    rows = data.get("items")
    if not isinstance(rows, list):
        rows = [v for v in data.values() if isinstance(v, dict) and "key" in v]
    by = {r.get("key"): r for r in rows if isinstance(r, dict)}
    ck("the integrations payload carries one row per component",
       {"strata", "evalscope", "grafana"} <= set(by), sorted(by))
    for k in sorted(by):
        e = by[k].get("entry") or {}
        note("row %s" % k, "status=%s  entry.via=%s  entry.url=%r  internal=%r"
             % (by[k].get("status"), e.get("via"), e.get("url"), e.get("internal_url")))
    for key in ("strata", "grafana"):
        e = (by.get(key) or {}).get("entry") or {}
        ck("%s: entry.url is the proxy address, not loopback" % key,
           str(e.get("via")) == "reverse-proxy"
           and e.get("url", "").startswith("https://")
           and "localhost" not in str(e.get("url")),
           e)
        ck("%s: entry.internal_url still records the loopback address" % key,
           "localhost" in str(e.get("internal_url")), e.get("internal_url"))
    # EvalScope has no public URL, so the honest answer is "not exposed" --
    # NOT "here is the loopback address".  These two are the whole difference
    # between a reader being told the truth and being sent to their own machine.
    e = (by.get("evalscope") or {}).get("entry") or {}
    ck("evalscope, which has no public url, is reported as not exposed",
       e.get("via") == "not-exposed" and e.get("url") == "",
       e)
    # "Not exposed to a browser" is not the same as "we do not know where it
    # listens".  The row keeps the server-side address so the console can say
    # localhost:9000 without offering it as a link; dropping it would make the
    # two addresses look like one that went missing.
    ck("evalscope's row still records where it listens on the node",
       "localhost" in str((by.get("evalscope") or {}).get("url")),
       (by.get("evalscope") or {}).get("url"))
except Exception as exc:  # noqa: BLE001
    ck("the integrations payload carries entry urls", False, "parse: %s" % exc)

for path in ("/grafana/", "/strata/"):
    st, body, _ = req("GET", path, auth=c["viewer"], verify=False)
    hits = sorted(set(re.findall(rb"127\.0\.0\.1:\d+", body)))
    ck("%s names no loopback address to the browser" % path, not hits,
       [h.decode() for h in hits])

print()
print("=== audit trail: it exists, it rolls, and it does not carry credentials ===")
if not os.path.exists(ACCESS_LOG):
    ck("access log written", False, "%s is absent" % ACCESS_LOG)
else:
    ck("access log written", True, "%d bytes" % os.path.getsize(ACCESS_LOG))
    blob = open(ACCESS_LOG, "rb").read()
    import base64
    plain = [who for who, (u, p) in c.items() if p.encode() in blob]
    ck("neither password appears in the log in plaintext", not plain, plain)
    toks = {who: base64.b64encode(("%s:%s" % v).encode()).decode()
            for who, v in c.items()}
    tok_leak = [who for who, t in toks.items() if t.encode() in blob]
    ck("nor as a basic-auth token", not tok_leak, tok_leak)

    # Caddy writes the full request header set into the access log and redacts
    # the credential-bearing ones.  The claim worth testing is therefore not
    # "the word Authorization is absent" -- it is present, and its presence is
    # correct -- but "every value that IS logged is the redaction placeholder".
    # Reading this the other way round is how the first version of this script
    # reported a leak on a log that redacts, which is a false alarm of the same
    # family as the false pass.
    authvals, recs = [], 0
    for line in blob.splitlines():
        if not line.strip().startswith(b"{"):
            continue
        recs += 1
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        h = (r.get("request") or {}).get("headers") or {}
        for k, v in h.items():
            if k.lower() == "authorization":
                authvals.extend(v if isinstance(v, list) else [v])
    if not authvals:
        note("access-log credentials", "%d records, none carried an Authorization "
             "header -- redaction is untested by this run" % recs)
    else:
        distinct = sorted(set(authvals))
        ck("every logged Authorization value is Caddy's redaction placeholder",
           distinct == ["REDACTED"],
           "%d values across %d records; distinct: %s"
           % (len(authvals), recs, distinct))
    note("log lines", "%d JSON records so far" % recs)

print()
if bad:
    for b in bad:
        print("FAIL %s" % b)
    sys.exit(1)
print("VERDICT: PASS -- %d checks, %d notes on what this does not cover."
      % (n_ck[0], len(notes)))
PY
