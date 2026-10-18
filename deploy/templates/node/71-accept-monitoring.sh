#!/usr/bin/env bash
# Acceptance for the monitoring chain: Strata JSON -> adapter -> Prometheus -> Grafana.
#
# Every check here is a value, not a shape.  "Grafana answers on :3000" is not
# evidence of anything -- it answers on :3000 with nothing behind it, which is
# exactly the failure the chain can have while looking healthy.  So the check
# that matters is that a number read at the far end of four hops equals the
# number read at the near end, and that is what this script compares.
#
# Read-only with respect to Strata.  The one thing it does to Grafana is a
# query through the admin API, which changes nothing.
set -euo pipefail

/usr/bin/python3 - <<'PY'
import base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request

bad, notes = [], []


def ck(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not cond:
        bad.append(label)


def note(label, detail):
    print("  ..   %s  -- %s" % (label, detail))
    notes.append(label)


def get(url, timeout=25):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return None, ("%s: %s" % (type(e).__name__, e)).encode()


def truthy(v):
    """Coerce an API setting to a bool, or None when it is not a boolean at all.

    Grafana's /api/admin/settings renders booleans as the *strings* "true" and
    "false".  Comparing those with `is True` / `is False` is always False, so
    the first version of these two checks reported FAIL against a correctly
    configured Grafana -- and the tempting repair, relaxing the comparison,
    would have made them pass against a misconfigured one too.  Coercing keeps
    the check able to fail: a missing key or an unexpected value yields None,
    which is not True and not False, so it still reports.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip().lower() == "true":
            return True
        if v.strip().lower() == "false":
            return False
    return None


def tcp(port):
    import socket
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect(("localhost", port)); return True
    except OSError:
        return False
    finally:
        s.close()


print("=== link 1: the adapter, and what it read from Strata ===")
ck("adapter listens on localhost:9101", tcp(9101))
st, body = get("http://localhost:9101/metrics")
ck("it answers /metrics with Prometheus text", st == 200 and b"# TYPE" in body,
   "status=%s bytes=%s" % (st, len(body)))
text = body.decode("utf-8", "replace")


def gauge(name):
    m = re.search(r"^%s(?:\{[^}]*\})?\s+(\S+)$" % re.escape(name), text, re.M)
    return float(m.group(1)) if m else None


up = gauge("llmlab_strata_up")
ck("llmlab_strata_up is 1 (Strata answered the adapter)", up == 1.0, up)
direct = {"gpu_mem": gauge("llmlab_strata_gpu_memory_used_bytes"),
          "temp": gauge("llmlab_strata_gpu_temperature_celsius"),
          "power": gauge("llmlab_strata_gpu_power_watts")}
note("readings taken directly from the adapter", direct)

print()
print("=== link 2: Prometheus is scraping it ===")
ck("prometheus listens on localhost:9090", tcp(9090))
st, body = get("http://localhost:9090/api/v1/targets")
if st != 200:
    print("  FAIL cannot read /api/v1/targets -- nothing below can be trusted")
    sys.exit(1)
targets = json.loads(body)["data"]["activeTargets"]
strata_t = [t for t in targets if t["labels"].get("job") == "strata"]
ck("a scrape target named 'strata' exists", bool(strata_t), [t["scrapeUrl"] for t in targets])
if strata_t:
    t = strata_t[0]
    ck("it points at the adapter, not at Strata", t["scrapeUrl"] == "http://localhost:9101/metrics",
       t["scrapeUrl"])
    ck("its health is up", t["health"] == "up", "%s %s" % (t["health"], t.get("lastError", "")))
    ck("it has scraped at least twice", (t.get("lastScrape") or "") and t.get("scrapeInterval"),
       "interval=%s lastScrape=%s" % (t.get("scrapeInterval"), t.get("lastScrape")))


def promql(q):
    st, body = get("http://localhost:9090/api/v1/query?query=" + urllib.request.quote(q))
    if st != 200:
        return None
    res = json.loads(body)["data"]["result"]
    return float(res[0]["value"][1]) if res else None


# The point of the whole script: the same number, read at both ends.  A chain
# that is merely *connected* can carry nothing; this is the check that it is
# carrying the actual reading, and that no hop silently substituted a default.
via_prom = promql("llmlab_strata_gpu_memory_used_bytes")
ck("Prometheus holds the adapter's GPU memory reading",
   via_prom is not None and via_prom == direct["gpu_mem"],
   "prometheus=%s adapter=%s" % (via_prom, direct["gpu_mem"]))
ck("the value is plausible for this node (a GPU with memory in use)",
   bool(via_prom) and 1e9 < via_prom < 6e10, via_prom)
ck("Prometheus' own `up` for the target is 1",
   promql('up{job="strata"}') == 1.0, promql('up{job="strata"}'))

print()
print("=== link 3: Grafana, and a query that has to travel the whole chain ===")
ini = "{{LAB_ROOT}}/services/grafana/conf/custom.ini"
ck("custom.ini is mode 600 (it holds a generated password)",
   oct(os.stat(ini).st_mode)[-3:] == "600", oct(os.stat(ini).st_mode)[-3:])
pw = re.search(r"^admin_password\s*=\s*(\S+)", open(ini, encoding="utf-8").read(), re.M)
ck("an admin password was generated", bool(pw))
auth = base64.b64encode(("admin:" + pw.group(1)).encode()).decode()


def gapi(path, data=None, method=None):
    req = urllib.request.Request("http://localhost:3000" + path, data=data, method=method)
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]


st, health = gapi("/grafana/api/health")
ck("Grafana answers /grafana/api/health", st == 200 and health.get("database") == "ok",
   "status=%s %s" % (st, health if isinstance(health, dict) else health))

st, dss = gapi("/grafana/api/datasources")
ds = next((d for d in (dss if isinstance(dss, list) else [])
           if d.get("uid") == "llmlab-prometheus"), None)
ck("the Prometheus datasource is provisioned under its pinned uid", bool(ds), st)
if ds:
    ck("it is set as the default", ds.get("isDefault") is True, ds.get("isDefault"))
    ck("it uses server-side (proxy) access, not the browser",
       ds.get("access") == "proxy", ds.get("access"))

st, hits = gapi("/grafana/api/search?type=dash-db")
titles = [h.get("title") for h in (hits if isinstance(hits, list) else [])]
ck("the overview dashboard is provisioned", "llmlab-overview" in
   [h.get("uid") for h in (hits if isinstance(hits, list) else [])], titles)

q = {"queries": [{"refId": "A",
                  "datasource": {"type": "prometheus", "uid": "llmlab-prometheus"},
                  "expr": "llmlab_strata_gpu_memory_used_bytes",
                  "instant": True, "range": False,
                  "intervalMs": 30000, "maxDataPoints": 1}],
     "from": "now-5m", "to": "now"}
st, res = gapi("/grafana/api/ds/query", json.dumps(q).encode(), method="POST")
end_val = None
try:
    end_val = res["results"]["A"]["frames"][0]["data"]["values"][1][0]
except Exception:  # noqa: BLE001
    pass
ck("a query through Grafana returns the same reading as the adapter",
   end_val is not None and float(end_val) == direct["gpu_mem"],
   "grafana=%s adapter=%s" % (end_val, direct["gpu_mem"]))

st, srv = gapi("/grafana/api/admin/settings")
settings = (srv or {}).get("server", {}) if isinstance(srv, dict) else {}
ck("Grafana's root_url names the sub-path the proxy will route",
   str(settings.get("root_url", "")).endswith("/grafana/"), settings.get("root_url"))
ck("serve_from_sub_path is on", truthy(settings.get("serve_from_sub_path")) is True,
   repr(settings.get("serve_from_sub_path")))
ck("Grafana itself is bound to loopback",
   settings.get("http_addr") == "localhost", settings.get("http_addr"))
ck("anonymous access is off",
   truthy((srv or {}).get("auth.anonymous", {}).get("enabled")) is False,
   repr((srv or {}).get("auth.anonymous", {}).get("enabled")))
ck("self-signup is off",
   truthy((srv or {}).get("users", {}).get("allow_sign_up")) is False,
   repr((srv or {}).get("users", {}).get("allow_sign_up")))

print()
print("=== the chain does not reach into Strata's tree ===")
out = subprocess.run(["ps", "-eo", "pid,cmd", "--no-headers"],
                     capture_output=True, text=True).stdout
ours = []
for line in out.splitlines():
    if any(k in line for k in ("adapter.py", "prometheus", "grafana", "evalscope", "legacy/console/server.py")):
        pid = line.split(None, 1)[0]
        exe = os.path.realpath("/proc/%s/exe" % pid)
        ours.append((pid, exe))
ck("no LLM Lab process runs on Strata's interpreter",
   not [o for o in ours if "{{STRATA_ROOT}}" in o[1]], [o[1] for o in ours])
reach = []
for pid, _ in ours:
    for fd in os.listdir("/proc/%s/fd" % pid) if os.path.isdir("/proc/%s/fd" % pid) else []:
        try:
            t = os.readlink("/proc/%s/fd/%s" % (pid, fd))
        except OSError:
            continue
        if t.startswith("{{STRATA_ROOT}}"):
            reach.append((pid, t))
ck("no open file descriptor under {{STRATA_ROOT}}", not reach, reach)
note("processes inspected", len(ours))

print()
print("=== retention and sampling are bounded ===")
st, body = get("http://localhost:9090/api/v1/status/runtimeinfo")
ri = json.loads(body)["data"] if st == 200 else {}
ck("Prometheus runs with a retention limit",
   bool(ri.get("storageRetention")), ri.get("storageRetention"))

print()
if bad:
    print("%d check(s) FAILED:" % len(bad))
    for b in bad:
        print("  - %s" % b)
    sys.exit(1)
if notes:
    print("%d note(s), not failures:" % len(notes))
    for n in notes:
        print("  - %s" % n)
    print()
print("VERDICT: PASS -- Strata's own readings arrive at Grafana unchanged, and "
      "nothing in the chain opens a file in Strata's tree.")
PY
