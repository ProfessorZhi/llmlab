#!/usr/bin/env bash
# Portal acceptance, run on the node against its own loopback port.
#
# The order matters, and that is why this is a script rather than a few curls
# pasted into a session: the brief requires the portal be proven *before*
# anything is exposed to the LAN, and requires that "proven" mean more than a
# 200.  An unrelated service on :18080 answers 200 to every path, so a status
# code is not evidence of anything here.  Each check asserts a structure -- a
# key the API documents, and where possible a value that can be cross-checked
# because it could not be right unless the thing it claims to be reading really
# answered.
#
# Read-only.  The one POST is a negative control that must be refused.
set -euo pipefail

PY=/usr/bin/python3

echo "=== reachability: bare TCP first, because a refused port and a slow one"
echo "    are different faults and a curl status cannot tell them apart ==="
"$PY" - <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("localhost", 8765)); print("  tcp localhost:8765  -> connected")
except OSError as e:
    print("  tcp localhost:8765  -> %s" % e); sys.exit(1)
finally:
    s.close()
PY

echo
"$PY" - <<'PY'
import json, sys, urllib.error, urllib.parse, urllib.request

BASE = "http://localhost:8765"
bad, notes = [], []


def get(path, raw=False):
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body.decode("utf-8", "replace")))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def ck(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not cond:
        bad.append(label)


def note(label, detail):
    print("  ..   %s  -- %s" % (label, detail))
    notes.append("%s: %s" % (label, detail))


def nums(obj, out=None):
    """Every numeric leaf under obj.  Used where the exact field name is a
    detail of the adapter and the claim is only that a real reading exists."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            nums(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            nums(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(obj)
    return out


print("--- the node probe is local, and it read this host ---")
st, live = get("/api/v2/live")
ck("/api/v2/live is a v2 envelope", st == 200 and isinstance(live, dict)
   and set(("ok", "data", "source")) <= set(live), "status=%s" % st)
ck("live ok=true", (live or {}).get("ok") is True, (live or {}).get("error"))
data = (live or {}).get("data") or {}
ssh = data.get("ssh") or {}
ck("a node block is present", bool(ssh), sorted(data))
ck("the probe reported success", ssh.get("ok") is True, "ok=%r error=%r" % (ssh.get("ok"), ssh.get("error")))
ck("it read this host's name", bool(ssh.get("hostname")), ssh.get("hostname"))

mem = ssh.get("mem") or {}
total_mib = mem.get("total_mib")
ck("memory was read from this machine, not estimated",
   isinstance(total_mib, (int, float)) and total_mib > 1024,
   "MemTotal=%s MiB (the host has ~30 GiB)" % total_mib)
ck("CPU count is a real reading", isinstance(ssh.get("cpu_count"), int) and ssh["cpu_count"] > 0,
   ssh.get("cpu_count"))
ck("disk facts came from this host's filesystems", bool(ssh.get("disks")), ssh.get("disks"))
# Every row must be a real filesystem.  A `df` header row parses as a row with
# a mount name and no numbers, and it slipped through exactly once -- invisible
# here because the header was localised, so the bogus mount read as a label
# rather than as something obviously wrong.
disk_rows = ssh.get("disks") or []
ck("no disk row is a header masquerading as a filesystem",
   all(isinstance(d.get("used_pct"), (int, float)) and d.get("mount") for d in disk_rows),
   disk_rows)
note("load average", ssh.get("loadavg"))
note("uptime (s)", ssh.get("uptime_s"))

raw = json.dumps(live, ensure_ascii=False)
for token in ("ssh: connect", "Permission denied", "no such identity", "id_ed25519",
              "command not found", "REMOTE_PROBE"):
    ck("the payload carries no %r (no SSH hop happened)" % token, token not in raw)

print()
print("--- Strata: actually reachable, with the credential actually loaded ---")
st, sc = get("/api/strata")
ck("/api/strata answers", st == 200 and isinstance(sc, dict), "status=%s" % st)
sc = sc or {}
ck("the credential is loaded", sc.get("key_present") is True, "key_present=%r" % sc.get("key_present"))
ck("and it is the key migrated from the workstation",
   sc.get("key_sha256_12") == "4b4fd7d83fcb", "sha=%r" % sc.get("key_sha256_12"))
ck("Strata is dialled over loopback on this host",
   "localhost:8080" in str(sc.get("base_url", "")), sc.get("base_url"))

ma = sc.get("model_api") or {}
ck("the model API answered (this needs a key, so it proves the key works)",
   bool(ma), sorted(ma)[:8] if isinstance(ma, dict) else ma)
if isinstance(ma, dict):
    note("models advertised", ma.get("models"))

gpu = sc.get("gpu") or {}
ck("a GPU block is present", bool(gpu), sorted(gpu) if isinstance(gpu, dict) else gpu)
gnums = nums(gpu)
ck("the GPU block carries real numeric telemetry", len(gnums) >= 2, gpu)

print()
print("--- the entry-address rule: nothing loopback escapes to a browser ---")
st, lab = get("/api/lab")
ck("/api/lab answers", st == 200 and isinstance(lab, dict), "status=%s" % st)
lab = lab or {}
labtxt = json.dumps(lab, ensure_ascii=False)
for token in ("Scripts/", ".exe", "PowerShell", "C:\\\\"):
    ck("the payload names no Windows artifact %r" % token, token not in labtxt)

# The rule: a loopback link is legitimate *only* because this portal is itself
# loopback-bound, so the reader is necessarily on this box.  If the binding
# ever changes, these links become live lies -- so the two facts are checked
# together rather than the URL alone.
#
# The field is required, not defaulted.  `...get("portal_host") or "localhost"`
# was the first version of this line, and the portal was not publishing the
# field, so this check derived "loopback-bound" from a missing value and passed
# without inspecting anything.  An acceptance check that cannot fail is worse
# than no check, because it is counted as evidence.
portal_host = (lab.get("lab") or {}).get("portal_host")
ck("the portal publishes its own binding (the fact that licenses every loopback link)",
   isinstance(portal_host, str) and bool(portal_host), "portal_host=%r" % (portal_host,))
default_loopback = portal_host in ("localhost", "localhost", "::1")
for grp in (lab.get("nav") or {}).get("groups", []):
    for it in grp.get("items", []):
        u = it.get("url") or ""
        if not u or ("localhost" not in u and "localhost" not in u):
            continue
        via = it.get("via")
        ck("loopback url for %r is justified by a loopback-bound portal" % it.get("key"),
           default_loopback and via in ("loopback", None),
           "via=%r url=%r" % (via, u))

print()
print("--- the historical artifacts are readable from this deployment ---")
st, runs = get("/api/benchmarks")
runs = (runs or {}).get("runs") if isinstance(runs, dict) else None
ck("/api/benchmarks returns a run list", isinstance(runs, list), "status=%s" % st)
if runs:
    note("benchmark runs visible", "%d run(s); newest=%s" % (len(runs), runs[0].get("run")))
    note("roots searched", sorted({r.get("root") for r in runs if r.get("root")}))
    first = str(runs[0].get("run"))
    st, det = get("/api/benchmarks/" + urllib.parse.quote(first))
    ck("the newest run opens and lists its files",
       st == 200 and isinstance(det, dict) and bool(det.get("files")),
       "status=%s files=%s" % (st, len((det or {}).get("files") or [])))
else:
    note("no benchmark runs present yet", "the archive has not been staged on this host")

st, reps = get("/api/reports")
reps = (reps or {}).get("reports") if isinstance(reps, dict) else None
ck("/api/reports returns a report list", isinstance(reps, list), "status=%s" % st)
if reps:
    note("reports visible", "%d report(s); newest=%s" % (len(reps), reps[0].get("path")))
    note("report roots searched", sorted({r.get("root") for r in reps if r.get("root")}))
    # Every listed report, not just the first.  The `path` a listing publishes
    # is fed straight back in as `?p=`, so the two routes are tested as a
    # round-trip: if the value that is displayed does not resolve again, the
    # link on the page is broken even though both endpoints answer.
    #
    # This loop is why the bug was found.  The single-report version passed on
    # the workstation, where every report lives inside the checkout; the first
    # report on the node lives in the data root outside it, and preview raised
    # while download worked -- the shape that looks like a permissions problem
    # and is actually a path-arithmetic one.
    preview_ok = download_ok = 0
    for rep in reps:
        rp = str(rep["path"])
        st, doc = get("/report?p=" + urllib.parse.quote(rp), raw=True)
        if st == 200 and isinstance(doc, bytes) and len(doc) > 200:
            preview_ok += 1
        else:
            ck("report previews: %s" % rp, False, "status=%s" % st)
        st, blob = get("/download?p=" + urllib.parse.quote(rp), raw=True)
        if st == 200 and isinstance(blob, bytes) and len(blob) > 100:
            download_ok += 1
        else:
            ck("report downloads: %s" % rp, False, "status=%s" % st)
    ck("every listed report previews", preview_ok == len(reps),
       "%d/%d" % (preview_ok, len(reps)))
    ck("every listed report downloads", download_ok == len(reps),
       "%d/%d" % (download_ok, len(reps)))
else:
    note("no reports present yet", "the archive has not been staged on this host")

print()
print("--- the gates still hold on this host ---")
for rel in ("config/secrets.env", "../../../etc/passwd",
            "{{LAB_SSH_KEY_PATH}}", "/etc/shadow"):
    st, _ = get("/download?p=" + urllib.parse.quote(rel))
    ck("refused: %s" % rel, st in (403, 404), "status=%s" % st)

print()
print("--- the write path is gated, not merely undocumented ---")
req = urllib.request.Request(BASE + "/api/service/start?key=evalscope", data=b"", method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        st = r.status
except urllib.error.HTTPError as e:
    st = e.code
ck("POST without X-Requested-With is refused", st == 403, "status=%s" % st)

req = urllib.request.Request(BASE + "/api/service/stop?key=strata", data=b"",
                             method="POST", headers={"X-Requested-With": "llm-lab-portal"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        st, body = r.status, json.loads(r.read().decode())
except urllib.error.HTTPError as e:
    st, body = e.code, None
ck("a service the portal did not start cannot be stopped through it",
   st == 404, "status=%s %s" % (st, body))

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
print("VERDICT: PASS -- the portal runs on the node, reads real state through "
      "a local probe, holds a working credential, and publishes no address "
      "that resolves on somebody else's machine.")
PY
