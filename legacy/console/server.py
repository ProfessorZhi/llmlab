#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM Lab portal -- the single, dependency-free local web entry point.

Design rules this file obeys.  They are the brief, not decoration:

  * **stdlib only.**  Starting the console must never require a ``pip install``.
    If the venvs are missing or broken the portal still comes up and says so.
  * **The API key stays server-side.**  It is read from ``config/secrets.env``,
    used on an outbound request, and never written into a response body, a log
    line, an error string, or a URL.  The secrets panel reports presence and a
    truncated digest -- never the value.
  * **Exactly one remote command.**  The SSH probe below is a fixed, hard-coded,
    read-only string.  There is no code path anywhere in this file that accepts
    a shell command from the browser, from a query parameter, or from the config
    file.  That is what "design the interface, do not add arbitrary shell
    execution" means in practice.
  * **No telemetry theatre.**  When a measurement is unavailable the portal says
    so and says why.  It never substitutes a stale number for a live one.

Run directly (``python portal/server.py``) or via ``scripts/start.ps1``.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "portal" / "web"

# The code tree and the *state* tree are two different things and must be able
# to live in two different places.  On a development box they coincide.  On a
# server the checkout is a git tree somebody else may update and reset, while
# pid files, logs and metric samples belong under a state root that survives
# that.  Both overrides are optional, and leaving them unset keeps the original
# in-tree behaviour byte-for-byte.
CONFIG_PATH = Path(os.environ.get("LAB_CONFIG") or (ROOT / "config" / "llm-lab.toml"))

# The endpoint protocol lives in integrations/, not here.  One implementation
# means the console and any future script cannot drift apart on what /health
# versus /v1/status actually require.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from integrations.strata import (  # noqa: E402
    StrataClient,
    native_panels,
    read_secret as _read_secret_abs,
)

# Files the browser may never reach, whatever the query string says.  A
# blocklist alone would be fragile, so this is paired with the prefix allowlist
# in ``_resolve_public`` below: both must pass.
DENY_NAMES = {"secrets.env", "local.toml", "strata-key.env", "id_ed25519_llmlab"}
DENY_RE = re.compile(r"(^|[\\/])id_[a-z0-9_]+$|\.key$|\.pem$|\.pfx$|\.p12$", re.I)
PUBLIC_PREFIXES = ("reports", "data/benchmarks", "docs", "portal/web")
PUBLIC_FILES = {"README.md", "config/llm-lab.example.toml"}

VERSION = "1.0.0"


# ===========================================================================
# configuration
# ===========================================================================

def load_config() -> dict:
    if tomllib is None:
        return {}
    try:
        with open(CONFIG_PATH, "rb") as fh:
            doc = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("[portal] config unreadable (%s); using defaults" % exc, file=sys.stderr)
        return {}

    # A site file states only what differs from the shared base, so a second
    # deployment does not become a second copy of the config that then drifts.
    # The overlay wins; tables merge, everything else is replaced wholesale --
    # which is what makes replacing a *list* (a launch argv, say) work, while
    # still letting a site adjust one key without restating its siblings.
    ext = str(((doc.get("site", {}) or {}).get("extend", "")) or "").strip()
    if ext:
        base = Path(ext).expanduser()
        if not base.is_absolute():
            base = (CONFIG_PATH.parent / base).resolve()
        try:
            with open(base, "rb") as fh:
                doc = _deep_merge(tomllib.load(fh), doc)
        except Exception as exc:  # noqa: BLE001
            print("[portal] site base %s unreadable (%s); using the overlay alone"
                  % (base, exc), file=sys.stderr)
    _check_services(doc)
    return doc


# An environment variable name.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The fields a service entry may carry.  Used to spot a stray field that TOML
# has filed under `env`.
_SERVICE_FIELDS = frozenset((
    "label", "url", "port", "probe_host", "managed", "portal_managed", "enabled",
    "launch", "cwd", "note", "depends_on", "env",
))


# Startup config problems, kept so they can be *served* rather than only
# printed.  A warning on stderr reaches whoever is watching the console at that
# moment -- which, for a service started by a cron shim, is nobody.  A config
# fault that nobody sees is indistinguishable from a correct config, so this
# list is published through /api/lab as well.
CONFIG_WARNINGS: list[str] = []


def _check_services(doc: dict) -> None:
    """Reject `env` entries that are really service fields in the wrong table.

    Why this exists: in TOML a sub-table header claims every key that follows
    it, so a plain field written *after* `[services.grafana.env]` becomes an
    environment variable with that field's name.  Writing `note = "..."` one
    line too low produced `env={"note": "..."}` -- no error, no warning, and
    the field silently missing from the service.

    The first version of this check tested only whether the name was a *legal*
    environment variable, on the theory that a scoping mistake would produce an
    illegal one.  It does not: `note` is a perfectly legal environment variable
    name, the check passed, and the bug it was written to catch went straight
    through it -- which is the whole lesson, and why the rule below is stated
    against the service schema instead.  `http_proxy` is lowercase and legal
    and stays; `note` is lowercase and *is a field name*, so it goes.
    """
    for key, svc in ((doc.get("services", {}) or {}) or {}).items():
        if not isinstance(svc, dict):
            continue
        env = svc.get("env") or {}
        for name in list(env):
            bad = None
            if not _ENV_NAME.match(str(name)):
                bad = "not a legal environment variable name"
            elif str(name) in _SERVICE_FIELDS:
                bad = ("collides with the service field %r, which means it was "
                       "written *below* the [services.%s.env] table header and "
                       "TOML has filed it under env -- move it above the header"
                       % (name, key))
            if bad:
                msg = "%s.env[%r]: %s" % (key, name, bad)
                CONFIG_WARNINGS.append(msg)
                print("[portal] dropping %s" % msg, file=sys.stderr)
                del env[name]


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursive overlay: tables merge, everything else is replaced."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = _deep_merge(cur, v)
        else:
            out[k] = v
    return out


CONFIG = load_config()
NODE = CONFIG.get("node", {})
PORTAL = CONFIG.get("portal", {})


def _cfg_path(s) -> Path | None:
    """Resolve one configured path, refusing one that names another platform.

    ``Path("/srv/x")`` is *drive-relative* on Windows, so ``ROOT / "/srv/x"``
    quietly yields ``D:\\srv\\x`` -- a wrong path that looks like a working one,
    which is the worst of both.  A leading slash in the config is a statement
    about a Linux deployment; the honest handling is to say so and yield
    nothing, so the reader sees "not configured" instead of a directory that
    was invented by the path library.

    Defined before ``_state_root`` because that function runs at import.
    """
    s = str(s or "").strip()
    if not s:
        return None
    if s.startswith("/") and os.name == "nt":
        print("[portal] ignoring %r: an absolute POSIX path in config on Windows "
              "would be silently remapped to this drive, not honoured" % s,
              file=sys.stderr)
        return None
    p = Path(s).expanduser()
    return p if p.is_absolute() else (ROOT / p)


def _state_root() -> Path:
    """Where this instance keeps its mutable state: pid files, logs, samples.

    Precedence is env var, then site config, then the code tree.  The env var
    wins because it is the one a service manager can set without editing a file
    that belongs to the operator.  A relative value is resolved against the code
    tree so a config can say ``state_root = "var"`` without knowing the path.
    """
    for cand in (os.environ.get("LAB_STATE_ROOT", ""),
                 str((CONFIG.get("paths", {}) or {}).get("state_root", ""))):
        p = _cfg_path(cand)
        if p is not None:
            return p
    return ROOT


STATE = _state_root()
RUN_DIR = STATE / "run"
LOG_DIR = STATE / "logs"


def _config_roots(key: str, default_rel: str) -> list[Path]:
    """The directories one logical tree may live in, in precedence order.

    A single string works and stays the default.  A *list* is the thing that
    matters after a machine move: the historical runs and reports were produced
    by the old deployment and the new one starts writing beside them, so a
    reader has to see both.  Relative entries resolve against the code tree (so
    ``"reports"`` keeps its old meaning); absolute ones do not, which is how a
    data root outside the checkout is expressed.

    Existence is deliberately not required here: an operator points at a
    directory before creating it, and a missing root should render as "no
    files", not as a crash on import.
    """
    raw = (CONFIG.get("paths", {}) or {}).get(key, default_rel)
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[Path] = []
    for it in items:
        p = _cfg_path(it)
        if p is None or p in out:
            continue
        out.append(p)
    return out or [ROOT / default_rel]


def _abs(rel: str) -> Path:
    """A configured file path as an absolute one, without inventing a location.

    Going through ``_cfg_path`` here matters for the secret file specifically:
    the Dell keeps its key outside the checkout, and on any host where that
    leading-slash path means something else, "no key configured" is the right
    answer while ``D:\\srv\\...`` would be a fabricated one that then reads as
    "credential missing" for a reason that is not true.
    """
    p = _cfg_path(rel)
    if p is not None:
        return p.resolve()
    # Refused because it names another platform.  Return a path under the code
    # tree that deliberately does not exist, rather than ``ROOT / "/srv/..."``
    # -- which is how the library would quietly turn a Linux path into a real
    # Windows one and make "no credential here" look like "credential missing".
    name = Path(str(rel).lstrip("/\\")).name or "unresolved"
    return (ROOT / ".path-not-available-on-this-host" / name).resolve()


def read_secret(var: str, filename: str = "config/secrets.env") -> str:
    """Read one ``KEY=value`` line.  Never logs or returns it to the browser."""
    return _read_secret_abs(var, str(_abs(filename)))


# ===========================================================================
# tiny markdown renderer
#
# Deliberately small: the corpus is our own reports, so it needs headings,
# lists, tables, fenced code, emphasis and links -- and needs to be safe.  Input
# is escaped *first*, then a closed set of inline constructs is re-introduced,
# so nothing in a report can inject markup.  Links are scheme-filtered.
# ===========================================================================

_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(text: str) -> str:
    stash: list[str] = []

    def keep_code(m: re.Match) -> str:
        stash.append("<code>%s</code>" % html.escape(m.group(1), quote=False))
        return "\x00%d\x00" % (len(stash) - 1)

    t = _CODE_SPAN.sub(keep_code, text)
    t = html.escape(t, quote=True)

    def keep_link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        low = url.lower()
        if low.startswith(("javascript:", "data:", "vbscript:")):
            return label
        return ('<a href="%s" rel="noopener noreferrer" target="_blank">%s</a>'
                % (url, label))

    t = _LINK.sub(keep_link, t)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    t = _ITALIC.sub(r"<em>\1</em>", t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], t)
    return t


def md_to_html(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        if line.lstrip().startswith("```"):
            lang = line.strip()[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = ' class="lang-%s"' % html.escape(lang, quote=True) if lang else ""
            out.append("<pre><code%s>%s</code></pre>"
                       % (cls, html.escape("\n".join(buf), quote=False)))
            continue

        if not line.strip():
            i += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2).strip()), lvl))
            i += 1
            continue

        # table: a header row followed by a separator row
        if line.strip().startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            def cells(row: str) -> list[str]:
                row = row.strip()
                if row.startswith("|"):
                    row = row[1:]
                if row.endswith("|"):
                    row = row[:-1]
                return [c.strip() for c in row.split("|")]

            head = cells(line)
            i += 2
            body = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>"
                       + "".join("<th>%s</th>" % _inline(c) for c in head)
                       + "</tr></thead><tbody>")
            for row in body:
                out.append("<tr>" + "".join("<td>%s</td>" % _inline(c) for c in row) + "</tr>")
            out.append("</tbody></table>")
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % _inline(" ".join(buf)))
            continue

        if _UL.match(line) or _OL.match(line):
            ordered = bool(_OL.match(line))
            rx = _OL if ordered else _UL
            items = []
            while i < n and rx.match(lines[i]):
                items.append("<li>%s</li>" % _inline(rx.match(lines[i]).group(1)))  # type: ignore[union-attr]
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag))
            continue

        buf = []
        while i < n and lines[i].strip() and not _HEADING.match(lines[i]) \
                and not lines[i].lstrip().startswith(("```", ">", "|")) \
                and not _UL.match(lines[i]) and not _OL.match(lines[i]) \
                and not _HR.match(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append("<p>%s</p>" % _inline(" ".join(buf)))
    return "\n".join(out)


# ===========================================================================
# benchmark artifacts
# ===========================================================================

BENCH_ROOTS = _config_roots("benchmarks", "data/benchmarks")


def _bench_root_for(run: str) -> Path | None:
    """Which of the benchmark roots holds *run* -- or None if none of them does.

    A run name is looked up rather than concatenated onto one fixed root,
    because with an archive in play the same name may be absent from the live
    root while being perfectly present in the archived one.
    """
    for base in BENCH_ROOTS:
        d = base / run
        if d.is_dir():
            return base
    return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def list_runs() -> list[dict]:
    runs = []
    seen = set()
    for base in BENCH_ROOTS:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir(), reverse=True):
            if not d.is_dir() or d.name in seen:
                continue
            seen.add(d.name)
            files = [p for p in d.rglob("*") if p.is_file()]
            runs.append({
                "run": d.name,
                "root": str(base),
                "files": len(files),
                "bytes": sum(p.stat().st_size for p in files),
                "mtime": max((p.stat().st_mtime for p in files), default=d.stat().st_mtime),
                "has_summary": (d / "summary.md").is_file(),
            })
    return sorted(runs, key=lambda r: r["run"], reverse=True)


def _mean(xs) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 3) if xs else None


def run_detail(run: str) -> dict | None:
    if os.sep in run or "/" in run or "\\" in run or run in ("", ".", ".."):
        return None
    base = _bench_root_for(run)
    if base is None:
        return None
    d = base / run

    doc: dict = {"run": run, "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}

    files = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            files.append({
                "path": str(p.relative_to(d)).replace("\\", "/"),
                "bytes": p.stat().st_size,
                "ext": p.suffix.lstrip(".").lower(),
            })
    doc["files"] = files

    prov = _read_json(base / "provenance.json") or {}
    hashes = {f["path"]: f.get("sha256", "") for f in
              (prov.get("benchmark_run", {}) or {}).get("files", []) if isinstance(f, dict)}
    for f in doc["files"]:
        f["sha256"] = hashes.get(f["path"], "")

    doc["configuration"] = _read_json(d / "configuration.json")
    doc["cold_start"] = _read_json(d / "loadtest_summary.json")
    doc["smoke"] = _read_json(d / "chat_smoke_keyed.json") or _read_json(d / "chat_smoke.json")

    # --- throughput / latency sweep -------------------------------------
    bs = _read_json(d / "bench_summary.json")
    if isinstance(bs, dict) and isinstance(bs.get("per_size"), dict):
        sizes = [int(s) for s in bs["per_size"].keys()]
        order = sorted(sizes)
        per = bs["per_size"]
        doc["bench"] = {
            "max_tokens": bs.get("max_tokens"),
            "repeats": bs.get("repeats"),
            "rows": [{
                "size": s,
                "prompt_tokens": per[str(s)].get("prompt_tokens"),
                "completion_tokens": per[str(s)].get("completion_tokens"),
                "ttft_s": per[str(s)].get("ttft_s"),
                "e2e_s": per[str(s)].get("e2e_s"),
                "prefill_tok_s": per[str(s)].get("engine_prefill_tok_s"),
                "decode_tok_s": per[str(s)].get("engine_decode_tok_s"),
            } for s in order],
        }

    # --- sequential vs concurrent ---------------------------------------
    cs = _read_json(d / "concurrency_short.json")
    if isinstance(cs, dict) and isinstance(cs.get("rows"), list):
        by_size: dict[int, list] = {}
        for r in cs["rows"]:
            by_size.setdefault(int(r["size"]), []).append(r)
        rows = []
        for s in sorted(by_size):
            rs = by_size[s]
            seq = _mean([r.get("seq_tok_s") for r in rs])
            con = _mean([r.get("conc_tok_s") for r in rs])
            rows.append({
                "size": s,
                "rounds": len(rs),
                "seq_tok_s": seq,
                "conc_tok_s": con,
                "ratio": _mean([r.get("ratio_conc_over_seq") for r in rs]),
                "seq_ttft_s": _mean([x for r in rs for x in (r.get("seq_ttft_s") or [])]),
                "conc_ttft_s": _mean([x for r in rs for x in (r.get("conc_ttft_s") or [])]),
            })
        doc["concurrency"] = {"run_id": cs.get("run_id"), "rows": rows}

    # --- quality ---------------------------------------------------------
    qs = _read_json(d / "quality" / "quality_summary.json")
    if isinstance(qs, dict) and isinstance(qs.get("categories"), dict):
        cats = []
        for name, c in qs["categories"].items():
            cats.append({
                "category": name,
                "n": c.get("n"), "passed": c.get("passed"),
                "failed": c.get("failed"), "score": c.get("score"),
                "failed_ids": c.get("failed_ids") or [],
            })
        cats.sort(key=lambda c: (c["score"] if c["score"] is not None else 0))
        total = sum(c["n"] or 0 for c in cats)
        passed = sum(c["passed"] or 0 for c in cats)
        doc["quality"] = {
            "model": qs.get("model"), "generated": qs.get("generated"),
            "grades": qs.get("grades"), "categories": cats,
            "n": total, "passed": passed,
            "score": round(passed / total, 4) if total else None,
        }

    # --- supporting text --------------------------------------------------
    for key, rel in (("hardware_text", "hardware.txt"), ("summary_md", "summary.md")):
        p = d / rel
        if p.is_file():
            doc[key] = _read_text(p)[:20000]
    return doc


# ===========================================================================
# reports
# ===========================================================================

REPORT_ROOTS = _config_roots("reports", "reports")


def list_reports() -> list[dict]:
    seen, out = set(), []
    for base in REPORT_ROOTS:
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".md", ".html", ".htm"):
                continue
            # The published *name* is relative to the report root, not to the
            # code tree.  A root outside the checkout has no path relative to
            # it at all, and computing one is how a listing raises ValueError
            # on exactly the deployment this exists for.
            rel = "reports/" + str(p.relative_to(base)).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            st = p.stat()
            out.append({
                "path": rel,
                "root": str(base),
                "title": _first_heading(p) or p.stem,
                "kind": "report",
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "rendered": p.suffix.lower() == ".md",
            })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _first_heading(p: Path) -> str:
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#"):
                return line.lstrip("#").strip()
            if line and not line.startswith(("<!--", "---")):
                break
    except OSError:
        pass
    return ""


# ===========================================================================
# live probes
#
# Everything here is read-only and cached for a short TTL so that a browser
# refresh loop cannot turn into an accidental load test against the node.
# ===========================================================================

SAMPLE_BUFFER: list[dict] = []
SAMPLE_MAX = 240


class Cache:
    """A TTL cache for an expensive producer, safe under a threaded server.

    The lock is not an optimisation.  `ThreadingHTTPServer` runs every request
    on its own thread, so without it two overlapping requests -- two browser
    tabs, or a page that is still loading while another refreshes -- both see a
    stale entry and both run the producer.  For the node probe that means two
    `ssh` subprocesses racing the same node, which is exactly the request storm
    a "refresh a few times quickly" user produces.  Holding the lock across the
    producer call makes the second caller wait and then take the first caller's
    fresh value: single-flight, the same rule the client store applies.

    A producer that raises is not cached.  The exception propagates and the
    timestamp is left untouched, so the next caller retries rather than being
    served a cached failure for a whole TTL.
    """

    def __init__(self, ttl: int) -> None:
        self.ttl, self.at, self.value = ttl, 0.0, None
        self._lock = threading.Lock()

    def get(self, producer):
        now = time.time()
        if self.value is not None and now - self.at <= self.ttl:
            return self.value
        with self._lock:
            # Re-check under the lock: the caller that held it may have just
            # refreshed the value while we waited.
            now = time.time()
            if self.value is not None and now - self.at <= self.ttl:
                return self.value
            value = producer()
            self.value = value
            self.at = time.time()
            return value


def strata_endpoint() -> dict:
    ep = (CONFIG.get("endpoints", {}) or {}).get("strata-local", {}) or {}
    return {
        "base_url": ep.get("base_url", ""),
        "secret_file": str(_abs(ep.get("secret_file", "config/secrets.env"))),
        "api_key_var": ep.get("api_key_var", "STRATA_API_KEY"),
    }


def probe_strata() -> dict:
    out = StrataClient(**strata_endpoint()).probe()
    # Ring buffer of samples this session actually collected, so any sparkline
    # on the page is a measurement rather than a decoration.
    gpu = out.get("gpu_live")
    if isinstance(gpu, dict):
        machine = ((out.get("status") or {}).get("machine") or {})
        SAMPLE_BUFFER.append({
            "t": time.time(),
            "gpu_util": gpu.get("util_pct"),
            "gpu_mem_mib": gpu.get("used_mib"),
            "gpu_temp": gpu.get("temp_c"),
            "ram_used": (machine.get("ram") or {}).get("used_mib"),
        })
        del SAMPLE_BUFFER[:-SAMPLE_MAX]
    return out


# The one and only remote command.  Fixed string, read-only, no interpolation.
REMOTE_PROBE = (
    'echo "@@@ host"; hostname; '
    'echo "@@@ kernel"; uname -r; '
    'echo "@@@ uptime_s"; cut -d" " -f1 /proc/uptime; '
    'echo "@@@ loadavg"; cut -d" " -f1-3 /proc/loadavg; '
    'echo "@@@ cpu"; grep -c ^processor /proc/cpuinfo; '
    'echo "@@@ meminfo"; grep -E "^(MemTotal|MemAvailable|SwapTotal|SwapFree):" /proc/meminfo; '
    'echo "@@@ disk"; df -PB1 / /srv 2>/dev/null | tail -n +2; '
    'echo "@@@ nvidia_smi"; (nvidia-smi --query-gpu=name,driver_version,temperature.gpu,'
    'utilization.gpu,memory.used,memory.total --format=csv,noheader 2>&1 || true) | head -2; '
    'echo "@@@ strata_units"; (systemctl is-active strata strata-eval 2>&1 || true); '
    'echo "@@@ listeners"; (ss -ltn 2>/dev/null | grep -E ":(8080|8081|9000)\\b" || echo "none"); '
    'echo "@@@ strata_proc"; (pgrep -af "strata" 2>/dev/null | head -2 || echo "none"); '
    'echo "@@@ end"'
)


def _ssh_argv() -> list[str] | None:
    exe = shutil.which("ssh")
    if not exe:
        return None
    key = NODE.get("ssh_key", "{{LAB_SSH_KEY_PATH}}")
    kh = NODE.get("ssh_known_hosts", "{{LAB_KNOWN_HOSTS_PATH}}")
    if not _abs(key).is_file():
        return None
    return [
        exe,
        "-i", str(_abs(key)),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        # Strict verification against the *project* known_hosts, which was
        # seeded from the pinned fingerprint.  No accept-new, ever.
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(_abs(kh)),
        "-o", "ConnectTimeout=%d" % int(NODE.get("ssh_timeout_s", 12)),
        "-o", "LogLevel=ERROR",
        "%s@%s" % (NODE.get("ssh_user", "{{LAB_USER}}"), NODE.get("host", "")),
        REMOTE_PROBE,
    ]


def verify_pinned_fingerprint() -> dict:
    """Re-derive the host key fingerprint from the project known_hosts and
    compare it with the pinned value.  Cheap, local, and it means a silently
    swapped host cannot ride along on a cached known_hosts."""
    pin_file = _abs(NODE.get("ssh_fingerprint", "{{LAB_SSH_FINGERPRINT_PATH}}"))
    kh = _abs(NODE.get("ssh_known_hosts", "{{LAB_KNOWN_HOSTS_PATH}}"))
    want = ""
    try:
        for line in pin_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SHA256:"):
                want = line
                break
    except OSError:
        return {"checked": False, "reason": "pinned fingerprint file missing"}
    if not want:
        return {"checked": False, "reason": "no SHA256: line in pinned fingerprint file"}
    if not kh.is_file():
        return {"checked": False, "reason": "project known_hosts missing"}

    host = NODE.get("host", "")
    # A host offers several key types (rsa, ecdsa, ed25519) and ssh-keyscan
    # writes one line per type.  Taking the *first* line would compare our
    # ed25519 pin against, say, the RSA key and report a bogus mismatch -- a
    # false negative that looks exactly like a real attack.  So: fingerprint
    # every entry for this host and ask whether the pin is among them.
    found: dict[str, str] = {}
    for line in kh.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith(host):
            continue
        try:
            blob = base64.b64decode(parts[2])
        except Exception:  # noqa: BLE001
            continue
        found[parts[1]] = "SHA256:" + base64.b64encode(
            hashlib.sha256(blob).digest()).decode().rstrip("=")
    if not found:
        return {"checked": False, "reason": "no known_hosts entry for %s" % host}
    hits = [t for t, fp in found.items() if fp == want]
    return {"checked": True, "match": bool(hits), "pinned": want,
            "matched_key_type": hits[0] if hits else None, "entries": found}


def probe_ssh() -> dict:
    out: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "fingerprint": verify_pinned_fingerprint()}
    argv = _ssh_argv()
    if not argv:
        out["ok"] = False
        out["error"] = ("ssh client or project key missing -- run "
                        "scripts/bootstrap_remote_access.py once")
        return out
    t0 = time.time()
    # Budget for the whole probe, separate from ConnectTimeout: the variance
    # here is in the session, not in the TCP handshake.  Measured on this
    # workstation, a bare `ssh ... hostname` to the same node took 2s on one
    # attempt and 19s on the next -- a 10x spread on an identical command --
    # and the real probe runs a dozen more commands on top of that.  A 25s
    # budget therefore reported a healthy node as an SSH failure.  A timeout
    # means "slow", and must be worded as slow.
    budget = float(NODE.get("ssh_probe_timeout_s", 60))
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=budget,
                              encoding="utf-8", errors="replace")
        raw = (proc.stdout or "") + (proc.stderr or "")
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        out["elapsed_s"] = round(time.time() - t0, 2)
        if proc.returncode != 0 and "@@@ end" not in raw:
            out["ok"] = False
            out["error"] = (proc.stderr or "").strip()[:400] or "ssh rc=%d" % proc.returncode
            out["raw"] = raw[:1500]
            return out
        out["ok"] = True
        out["blocks"] = _parse_probe(raw)
        out["facts"] = _probe_facts(out["blocks"])
    except subprocess.TimeoutExpired:
        out["ok"] = False
        out["error"] = "ssh probe timed out after %ds" % int(budget)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def _local_addresses() -> set:
    """Every spelling of "this machine" a config file might plausibly use."""
    names = {"localhost", "::1", "localhost", "all-interfaces"}
    try:
        names.add(socket.gethostname())
        names.add(socket.getfqdn())
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(str(info[4][0]))
    except OSError:
        pass
    return names


def node_mode() -> str:
    """Is the node this process runs on, or a machine reached over SSH?

    Both are real deployments and they want opposite things.  On the Dell the
    answer is "local", and there the SSH path is not merely redundant -- it is
    wrong, because it makes the portal depend on a private key that has no
    business existing on that host.  On a laptop with a remote GPU box the SSH
    path is the entire point.  ``[node] mode`` overrides the automatic answer.
    """
    m = str(NODE.get("mode", "") or "").strip().lower()
    if m in ("local", "ssh"):
        return m
    host = str(NODE.get("host", "") or "").strip()
    if not host:
        return "local"
    return "local" if host in _local_addresses() else "ssh"


def _probe_label() -> str:
    """What to call the probe in a message the reader will see.

    "SSH 探针失败" is a lie in local mode -- there is no SSH hop to fail, and an
    operator chasing a broken key that does not exist wastes the one thing an
    alert is supposed to save.
    """
    return "本机探针" if node_mode() == "local" else "SSH 探针"


_MEM_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
             "SwapTotal", "SwapFree")


def _local_meminfo() -> str:
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""
    rows = [ln.strip() for ln in text.splitlines()
            if ln.split(":", 1)[0].strip() in _MEM_KEYS]
    return "\n".join(rows)


# The ports this project has an opinion about: Strata, EvalScope, Grafana,
# Prometheus and the portal itself.
_CARED_PORTS = (3000, 8080, 8081, 8765, 9000, 9090)


def _local_listeners() -> str:
    """A ``ss -ltn`` reading, filtered here rather than through a shell.

    The binary is invoked with a fixed argv and the matching happens in Python,
    so there is no pipeline for anything to be injected into.  When ``ss`` is
    missing the fallback is a real TCP connect, which proves less -- that
    *something* accepts on that loopback port, not that ours does -- so the
    line says which method produced it instead of being dressed up as an ss row.
    """
    out = ""
    try:
        p = subprocess.run(["ss", "-ltn"], capture_output=True, timeout=10,
                           encoding="utf-8", errors="replace")
        out = p.stdout or ""
    except Exception:  # noqa: BLE001
        out = ""
    keep = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4 or cols[0] != "LISTEN":
            continue
        port = cols[3].rsplit(":", 1)[-1]
        if port.isdigit() and int(port) in _CARED_PORTS:
            keep.append(line.strip())
    if keep:
        return "\n".join(keep)
    rows = ["loopback:%d connectable (ss gave nothing; TCP connect fallback)" % pt
            for pt in _CARED_PORTS if tcp_open("localhost", pt)]
    return "\n".join(rows)


def _proc_scan(needle: str, limit: int = 4) -> str:
    """``pgrep -af needle`` done by reading /proc, so no child is spawned.

    Reading /proc also sidesteps a trap that has bitten this project before: a
    ``pgrep`` process carries the needle in its own command line, so any
    implementation that greps a process list it is itself part of has to
    special-case itself or it reports a hit for a process that exists only
    because somebody asked the question.
    """
    hits = []
    me = os.getpid()
    try:
        entries = sorted(Path("/proc").iterdir())
    except OSError:
        return ""
    for entry in entries:
        if not entry.name.isdigit():
            continue
        if int(entry.name) == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
        if argv and any(needle in a for a in argv):
            hits.append("%s %s" % (entry.name, " ".join(argv)[:160]))
        if len(hits) >= limit:
            break
    return "\n".join(hits)


def _local_probe_text() -> str:
    """The same @@@-framed body the SSH probe returns, answered on this host.

    Deliberately the same *shape* and not a parallel "local facts" structure.
    ``_parse_probe`` and ``_probe_facts`` already turn that text into everything
    the pages read, so emitting the same frames means the local path cannot
    drift from the remote one in what the frontend is handed.  Two producers,
    one contract.
    """
    lines: list = []

    def block(tag: str, body) -> None:
        lines.append("@@@ " + tag)
        text = body if isinstance(body, str) else "\n".join(body)
        lines.append(text or "")

    def run(argv):
        try:
            p = subprocess.run(argv, capture_output=True, timeout=15,
                               encoding="utf-8", errors="replace")
            return ((p.stdout or "") + (p.stderr or "")).strip()
        except Exception as exc:  # noqa: BLE001
            return "%s: %s" % (type(exc).__name__, exc)

    block("host", socket.gethostname())
    try:
        block("kernel", os.uname().release)
    except AttributeError:
        block("kernel", "unknown (not POSIX)")
    try:
        block("uptime_s", Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except OSError:
        block("uptime_s", "")
    try:
        block("loadavg", " ".join("%.2f" % x for x in os.getloadavg()))
    except OSError:
        block("loadavg", "")
    block("cpu", str(os.cpu_count() or 0))
    block("meminfo", _local_meminfo())
    # `df -P` rather than shutil.disk_usage: the existing fact parser reads df
    # columns, and /srv is checked as well as / because on this node they are
    # genuinely different filesystems.
    #
    # The header row is dropped here for the same reason the remote probe pipes
    # through `tail -n +2`: the parser reads columns, so a header line becomes a
    # phantom filesystem.  It was observed on the real host as a row whose mount
    # point was the localised header text -- which is also why the row looked
    # plausible enough to survive review on an English-locale machine, where the
    # bogus mount would have read "Mounted on".
    block("disk", "\n".join(run(["df", "-PB1", "/", "/srv"]).split("\n")[1:]))
    block("nvidia_smi", run([
        "nvidia-smi",
        "--query-gpu=name,driver_version,temperature.gpu,utilization.gpu,"
        "memory.used,memory.total", "--format=csv,noheader"]))
    # The remote probe asked systemd.  On this host that is a question whose
    # true answer ("inactive") is still read as a fault, because Strata here is
    # a session process and not a unit at all.  Report the fact instead of
    # copying a misleading command into a context where it means something else.
    block("strata_units", "n/a: Strata is not a systemd unit on this host "
                          "(authority is strata_proc + listeners below)")
    block("listeners", _local_listeners() or "none")
    block("strata_proc", _proc_scan("strata") or "none")
    lines.append("@@@ end")
    return "\n".join(lines) + "\n"


def probe_local() -> dict:
    """Read this host directly.  Same payload contract as ``probe_ssh``."""
    out: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "local",
                 "fingerprint": {"checked": False,
                                 "reason": "no SSH hop in local mode"}}
    t0 = time.time()
    try:
        out["blocks"] = _parse_probe(_local_probe_text())
        out["facts"] = _probe_facts(out["blocks"])
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    out["elapsed_s"] = round(time.time() - t0, 3)
    return out


def probe_node() -> dict:
    """Whichever probe this deployment actually needs.

    Callers must use this rather than ``probe_ssh``/``probe_local`` directly:
    the mode is a property of the deployment, and a caller that picks one is a
    caller that hardcodes which deployment it is in.
    """
    return probe_local() if node_mode() == "local" else probe_ssh()


def _parse_probe(text: str) -> dict:
    blocks: dict[str, list[str]] = {}
    cur = None
    for line in text.split("\n"):
        if line.startswith("@@@ "):
            cur = line[4:].strip()
            blocks.setdefault(cur, [])
        elif cur is not None:
            blocks[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in blocks.items()}


def _int(s: str):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _probe_facts(b: dict) -> dict:
    f: dict = {}
    f["hostname"] = b.get("host", "")
    f["kernel"] = b.get("kernel", "")
    up = _int(b.get("uptime_s", ""))
    f["uptime_s"] = up
    la = (b.get("loadavg", "") or "").split()
    f["loadavg"] = [float(x) for x in la[:3] if _float(x) is not None] or None
    f["cpu_count"] = _int(b.get("cpu", ""))

    mem = {}
    for line in (b.get("meminfo", "") or "").split("\n"):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        mem[k.strip()] = _int(v.strip().split()[0]) if v.strip() else None
    if mem:
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        f["mem"] = {
            "total_mib": round(total / 1024, 1) if total else None,
            "avail_mib": round(avail / 1024, 1) if avail else None,
            "used_pct": round(100.0 * (total - avail) / total, 1) if total and avail else None,
            "swap_total_mib": round((mem.get("SwapTotal") or 0) / 1024, 1),
            "swap_free_mib": round((mem.get("SwapFree") or 0) / 1024, 1),
        }

    disks = []
    seen_mount = set()
    for line in (b.get("disk", "") or "").split("\n"):
        parts = line.split()
        if len(parts) >= 6:
            total, used = _int(parts[1]), _int(parts[2])
            # `df -P / /srv` prints the same filesystem twice when /srv is not
            # its own mount.  Dedupe, or the panel shows a phantom second disk.
            if parts[5] in seen_mount:
                continue
            seen_mount.add(parts[5])
            disks.append({
                "mount": parts[5], "fs": parts[0],
                "total_gib": round(total / 2**30, 1) if total else None,
                "used_gib": round(used / 2**30, 1) if used else None,
                "used_pct": round(100.0 * used / total, 1) if total and used else None,
            })
    f["disks"] = disks

    # nvidia-smi on this host fails its NVML version check; capture that
    # verbatim instead of pretending the GPU is idle.
    nsmi = b.get("nvidia_smi", "") or ""
    if nsmi and "Failed to initialize NVML" in nsmi:
        f["gpu_nvml"] = {"available": False, "reason": nsmi.split("\n")[0][:300]}
    elif nsmi and "," in nsmi.split("\n")[0]:
        cols = [c.strip() for c in nsmi.split("\n")[0].split(",")]
        f["gpu_nvml"] = {"available": True, "raw": cols}
    else:
        f["gpu_nvml"] = {"available": False,
                         "reason": nsmi.split("\n")[0][:300] if nsmi else "no output"}

    units = (b.get("strata_units", "") or "").split("\n")
    f["strata_units"] = [u for u in units if u.strip()][:4]
    # systemd says "inactive" for both unit names here, yet :8080 clearly
    # answers -- so the unit name is not the truth about what is running.
    # Report the listener and the process instead of guessing.
    f["listeners"] = [l.strip() for l in (b.get("listeners", "") or "").split("\n")
                      if l.strip() and l.strip() != "none"]
    f["strata_proc"] = [l.strip() for l in (b.get("strata_proc", "") or "").split("\n")
                        if l.strip() and l.strip() != "none"][:2]
    # The health authority, computed once here so no page has to re-derive it:
    # is the port open, and is there something that looks like Strata running.
    # The systemd string above stays as a diagnostic only -- on the Dell its
    # honest answer is "inactive" and it is still wrong, which is exactly why it
    # must not be what a health claim is built on.
    listening_blob = " ".join(f["listeners"])
    f["strata_runtime"] = {
        "listening": any((":%s" % p) in listening_blob for p in ("8080", "8081")),
        "processes": f["strata_proc"],
        "authority": "listener + process scan (not systemd)",
    }
    return f


def _float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_services() -> list[dict]:
    out = []
    for key, svc in (CONFIG.get("services", {}) or {}).items():
        svc = svc or {}
        port = svc.get("port")
        url = svc.get("url", "")
        uhost = "localhost"
        if url:
            try:
                uhost = urllib.parse.urlsplit(url).hostname or uhost
            except ValueError:
                pass
        out.append({
            "key": key,
            "label": svc.get("label", key),
            "url": url,
            "port": port,
            "enabled": bool(svc.get("enabled", False)),
            "managed": bool(svc.get("managed", False)),
            "up": tcp_open(uhost, int(port)) if port else False,
            "note": svc.get("note", ""),
        })
    return out


def probe_local_ports() -> list[dict]:
    out = []
    for p in (CONFIG.get("ports", {}) or {}).get("must_be_free", []):
        out.append({"port": p, "in_use": tcp_open("localhost", int(p))})
    return out


CACHE_LIVE = Cache(15)
# Longer than the page's 20s live-poll interval on purpose.  At exactly 20s the
# entry expired in the same instant the next poll arrived, so *every* poll
# re-ran the SSH probe and the cache never absorbed anything.  30s makes every
# other poll a cache hit, and the value a reader sees is at most 30s old --
# which is the same order as the 15s console TTL beside it.
CACHE_SSH = Cache(30)


def live_payload() -> dict:
    return {
        "strata": CACHE_LIVE.get(probe_strata),
        "ssh": CACHE_SSH.get(probe_node),
        "services": probe_services(),
        # The tunnel is a service too, but it belongs to *this* machine, not the
        # node, and its three states (configured / listening / ours) do not
        # reduce to the `up` boolean the services list carries.
        "tunnel": tunnel_state(),
        "evalscope": evalscope_state(),
        "samples": SAMPLE_BUFFER[-120:],
    }


# ===========================================================================
# environment check  (§5: verify against *this* machine before promising it)
# ===========================================================================

def _venv_bins() -> tuple:
    """Where the eval service's interpreter and CLI may live.

    Two layouts, one config.  A Windows venv puts executables in
    ``Scripts/*.exe``; a POSIX venv puts them in ``bin/*``.  Both are probed and
    whichever exists wins, so the same source runs on either without a branch at
    every call site.  The directory is taken from the service definition when
    the site config names one -- which is what lets the server point it at
    ``/srv`` instead of inside the checkout.
    """
    svc = ((CONFIG.get("services", {}) or {}).get("evalscope", {}) or {})
    launch = [str(a) for a in (svc.get("launch") or [])]
    root = ROOT / "evaluation" / ".venv"
    if launch:
        cand = Path(launch[0].replace("{port}", "0").replace("{host}", "localhost"))
        exe = cand if cand.is_absolute() else (ROOT / cand)
        root = exe.parent.parent
    for py, cli in ((root / "Scripts" / "python.exe", root / "Scripts" / "evalscope.exe"),
                    (root / "bin" / "python", root / "bin" / "evalscope")):
        if py.is_file() or cli.is_file():
            return py, cli
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "evalscope.exe"
    return root / "bin" / "python", root / "bin" / "evalscope"


def _env_tools(run) -> dict:
    """The external tools it is worth reporting, for *this* operating system.

    A PowerShell row is meaningful on Windows and is a guaranteed false fault
    everywhere else: the probe fails with FileNotFoundError and the environment
    page reports a missing tool that was never part of the design.  Report the
    shell and the socket tool the host actually has instead.
    """
    tools = {
        "node": run(["node", "--version"]),
        "git": run(["git", "--version"]),
        "ssh": run(["ssh", "-V"]) or ("found: " + (shutil.which("ssh") or "no")),
    }
    if os.name == "nt":
        tools["powershell"] = run(["powershell", "-NoProfile", "-Command",
                                   "$PSVersionTable.PSVersion.ToString()"])
    else:
        tools["bash"] = run(["bash", "--version"])
        tools["ss"] = run(["ss", "-V"])
    return tools


def env_report() -> dict:
    def run(args):
        try:
            p = subprocess.run(args, capture_output=True, timeout=20,
                               encoding="utf-8", errors="replace")
            return (p.stdout or p.stderr or "").strip().split("\n")[0][:160]
        except Exception as exc:  # noqa: BLE001
            return "unavailable (%s)" % type(exc).__name__

    venv_py, evalscope = _venv_bins()

    def rel(p: Path) -> str:
        # A site config may put the venv outside the checkout (/srv/...), and
        # relative_to raises ValueError rather than returning something odd --
        # so report the absolute path instead of 500ing the whole page.
        try:
            return str(p.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            return str(p)

    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": {
            "running": sys.version.split()[0],
            "executable": sys.executable,
            "note": "the portal itself is stdlib-only",
        },
        "tools": _env_tools(run),
        "evaluation": {
            "venv_python": rel(venv_py) if venv_py.is_file() else None,
            "evalscope_installed": evalscope.is_file(),
            "evalscope_version": run([str(evalscope), "--version"]) if evalscope.is_file() else None,
        },
        "ports": probe_local_ports(),
    }


# ===========================================================================
# path safety
# ===========================================================================

def _serve_roots(norm: str) -> list[Path] | None:
    """The roots a publishable path may be found under, or None if the path is
    not publishable at all.

    The allowlist is still a prefix allowlist and the denylist is still applied
    first -- this only answers *where* an allowed path lives.  A prefix whose
    data root was moved off the checkout resolves under the configured root
    instead, which is what keeps an archived report readable after a server
    move without widening what may be published.
    """
    if norm in PUBLIC_FILES:
        return [ROOT]
    for prefix in PUBLIC_PREFIXES:
        if norm == prefix or norm.startswith(prefix + "/"):
            key = {"data/benchmarks": "benchmarks"}.get(prefix, prefix)
            if key == "portal/web":
                return [ROOT]
            return _config_roots(key, prefix)
    return None


def _resolve_public(rel: str) -> Path:
    """Resolve ``rel`` and refuse anything not explicitly publishable.

    Two independent gates (denylist + prefix allowlist) because one gate with a
    subtle bug is how a key ends up in a browser.  The per-root ``commonpath``
    check is a third: allowing a root outside the checkout must not also allow
    a ``..`` to walk back out of it.
    """
    rel = urllib.parse.unquote(rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("empty path")
    parts = Path(rel).parts
    if any(p in ("..", "") for p in parts) or Path(rel).is_absolute():
        raise ValueError("path traversal rejected")
    # casefold, not a bare ==.  This list is the second of two independent gates
    # (the prefix allowlist below is the first), and a name comparison that is
    # only exact is a gate whose strength depends on which filesystem it is
    # running on: NTFS would refuse "Secrets.env" and ext4 would not.  A secret
    # must not be publishable on one platform and not the other.
    if parts[-1].casefold() in DENY_NAMES or DENY_RE.search(rel):
        raise ValueError("path is on the never-publish list")

    roots = _serve_roots(rel)
    if roots is None:
        raise ValueError("path is outside the publishable set")

    for root in roots:
        if str(root) == str(ROOT):
            sub = rel
        else:
            prefix = next(p for p in PUBLIC_PREFIXES if rel.startswith(p + "/"))
            sub = rel[len(prefix) + 1:]
        try:
            real = (root / sub).resolve()
            real_root = root.resolve()
            if os.path.commonpath([str(real), str(real_root)]) != str(real_root):
                continue
        except (ValueError, OSError):
            continue
        if real.is_file():
            return real
    raise ValueError("not a file")


def _display_path(p: Path) -> str:
    """The publishable path for ``p``: the inverse of ``_resolve_public``.

    This is not a cosmetic string.  The value it returns is what the API
    publishes as a report's ``path`` and what the front end feeds back in
    ``?p=``, so it has to round-trip: whatever is displayed must resolve again
    to the same file.

    The reason this exists at all: the first implementation wrote
    ``p.relative_to(ROOT)`` inline, which was correct while every publishable
    file lived inside the checkout.  Once reports and benchmarks moved to a
    configured data root, ``relative_to`` raised ValueError for exactly the
    archived material -- and the route's error mapping turned that into a 403.
    The symptom was the shape that wastes the most time: *downloading* a report
    worked and *previewing* it did not, because the download route never
    computed a display path.

    The configured roots are consulted FIRST, and that ordering is load-bearing
    rather than stylistic.  ``relative_to(ROOT)`` succeeds for a root that
    happens to sit inside the checkout -- ``reports = ["var/audit"]``, say --
    and would then publish ``var/audit/x.md``, a value ``_resolve_public``
    refuses because it is not under any allowed prefix.  So the shortcut that
    "always works" is precisely the one that produces a link that does not.
    Asking the roots instead yields ``reports/x.md``, which resolves.
    """
    # Name it by the same prefix the allowlist uses for that root, which is what
    # makes the result resolve back to this very file.
    for prefix in PUBLIC_PREFIXES:
        key = {"data/benchmarks": "benchmarks"}.get(prefix, prefix)
        for root in (_config_roots(key, prefix) or []):
            try:
                return (prefix + "/" + p.relative_to(root).as_posix())
            except ValueError:
                continue
    # Under no configured root, but still inside the checkout: the case of
    # PUBLIC_FILES (README.md, config/llm-lab.example.toml), which are publishable at
    # their own path and belong to no tree.
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        pass
    # Publishable by neither route: say so rather than invent a path.  An
    # absolute path is not usable as a `?p=` value, and a wrong one would be
    # worse than an obviously unusable one.
    return str(p)


def _csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    cols = list(rows[0].keys())

    def cell(v):
        s = "" if v is None else str(v)
        return '"%s"' % s.replace('"', '""') if any(c in s for c in ',"\n') else s

    return "\n".join([",".join(cols)] + [",".join(cell(r.get(c)) for c in cols) for r in rows])


def _zip_dir(d: Path, run: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                z.write(p, arcname="%s/%s" % (run, p.relative_to(d).as_posix()))
    return buf.getvalue()


# ===========================================================================
# the node's native console, and the tunnel that carries it
#
# The node ships its own Chat / Monitor / About UI.  This lab does not
# reimplement any of it: it links to it.  What the lab *does* own is the local
# forwarding port, because a forwarded loopback origin is stable across
# networks whereas the node's LAN address is not.
# ===========================================================================

def tunnel_config() -> dict:
    return ((CONFIG.get("tunnels", {}) or {}).get("strata-native", {}) or {})


def hostport_of(url: str):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not u.hostname or not u.port:
        return None
    return u.hostname, int(u.port)


def _read_pid_file(name: str):
    try:
        text = (RUN_DIR / name).read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _proc_starttime(pid):
    """Field 22 of ``/proc/<pid>/stat``: start time, in clock ticks since boot.

    A pid is not an identity -- the kernel recycles it.  This is the cheapest
    value that separates "our process is still running" from "something else
    now owns that number", so it is what the stop path verifies against.
    Returns None off Linux or once the process is gone.
    """
    try:
        raw = Path("/proc/%d/stat" % int(pid)).read_text(encoding="ascii", errors="replace")
    except (OSError, TypeError, ValueError):
        return None
    # The comm field is parenthesised and may itself contain spaces and parens,
    # so the split has to start after the LAST ')' rather than after a fixed
    # offset.  parts[0] is then field 3 (`state`), making field 22 parts[19].
    rp = raw.rfind(")")
    if rp < 0:
        return None
    parts = raw[rp + 1:].split()
    if len(parts) < 20:
        return None
    try:
        return int(parts[19])
    except ValueError:
        return None


def _write_proc_record(key: str, proc) -> dict:
    """Record *what* was started, beside the bare pid file.

    ``<key>.pid`` stays a plain integer because it predates this and is parsed
    as one elsewhere.  The richer record goes to ``<key>.proc``: pid, its start
    time, the process group and the argv head.  Those four are exactly what a
    stop path needs to refuse a recycled pid instead of killing a stranger.
    """
    rec = {
        "key": key,
        "pid": int(proc.pid),
        "pgid": None,
        "starttime": _proc_starttime(proc.pid),
        "argv0": "",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": socket.gethostname(),
    }
    if os.name != "nt":
        try:
            rec["pgid"] = os.getpgid(proc.pid)
        except OSError:
            pass
    try:
        with open("/proc/%d/cmdline" % int(proc.pid), "rb") as fh:
            head = fh.read(1024).split(b"\x00")
        rec["argv0"] = head[0].decode("utf-8", "replace") if head else ""
        rec["argv1"] = head[1].decode("utf-8", "replace") if len(head) > 1 else ""
    except OSError:
        pass
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / ("%s.proc" % key)).write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return rec


def _pid_alive(pid) -> bool:
    """Is a process with this pid still running?

    stdlib-only.  ``os.kill(pid, 0)`` is not a liveness test on Windows, so ask
    the kernel through OpenProcess.  A live pid says nothing about *identity* --
    pids get recycled -- which is why callers that care must check more.
    """
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def tunnel_state() -> dict:
    """What the forwarding port actually is right now.

    Three independent facts, reported apart because they disagree in practice:
    whether a tunnel is *configured*, whether something is *listening* on its
    local port, and whether the pid we recorded is still alive.  A listener with
    a dead pid file means someone else owns that port -- which is exactly the
    situation that makes "the port answers, therefore our tunnel is up" a lie.
    """
    t = tunnel_config()
    port = int(t.get("local_port", 0) or 0)
    enabled = bool(t.get("enabled", False))
    pid = _read_pid_file("strata-tunnel.pid")
    listening = tcp_open("localhost", port) if port else False
    alive = _pid_alive(pid)
    return {
        "enabled": enabled,
        "label": t.get("label", "Strata native UI"),
        "local_port": port,
        "remote": "%s:%s" % (t.get("remote_host", "localhost"),
                             t.get("remote_port", 8080)),
        "url": ("http://localhost:%d/" % port) if port else "",
        "listening": listening,
        "pid": pid,
        "pid_alive": alive,
        "owned": bool(listening and alive),
        "foreign": bool(listening and not alive),
    }


CACHE_CONSOLE = Cache(15)


def strata_console() -> dict:
    """The node payload the homepage renders, plus where to open its own UI."""
    ep = strata_endpoint()
    payload = CACHE_CONSOLE.get(lambda: StrataClient(**ep).console())

    tun = tunnel_state()
    fallback = (tunnel_config().get("fallback_url") or "").strip() or ep.get("base_url", "")
    if tun["enabled"] and tun["url"]:
        internal, kind = tun["url"].rstrip("/"), "tunnel"
        reachable = bool(tun["owned"] or tun["listening"])
    else:
        internal, kind = fallback.rstrip("/"), "direct"
        hp = hostport_of(internal)
        reachable = tcp_open(*hp) if hp else False

    # ``internal`` is what *this* process dials; the browser needs the other
    # address for the same thing.  They are equal only by coincidence (a
    # loopback-bound portal, where the reader is necessarily local), so the
    # choice is made by entry_url() rather than assumed here.
    entry = entry_url("strata", internal)
    payload["native"] = {
        "kind": kind,
        "base_url": entry["url"],
        "internal_url": entry["internal_url"],
        "via": entry["via"],
        # False when nothing is exposed, even if this process itself can still
        # dial the service: these links are for the reader, not for us, and a
        # green dot next to a link that cannot open is worse than a red one.
        "reachable": bool(entry["url"]) and reachable,
        "panels": native_panels(entry["url"], reachable=bool(entry["url"]) and reachable),
        "fallback_url": fallback,
        "fallback_reachable": (tcp_open(*hp) if (hp := hostport_of(fallback)) else False),
        "tunnel": tun,
        # Say the rule out loud, because it is the thing a reader will doubt.
        "note": ("Links open the node's own UI in a new tab; nothing is embedded, "
                 "and the API key is typed into that UI by you, never injected by "
                 "this console."),
    }
    return payload


# ===========================================================================
# evaluation summary
#
# The benchmark artifacts are the record of what was actually measured.  This
# section answers four questions over them -- what ran last, how many models
# are covered, what is missing, and how it scored -- and it answers the
# "what is missing" one explicitly, because a summary that only lists
# successes quietly reads as full coverage.
# ===========================================================================

EVAL_DIMENSIONS = (
    ("throughput", "吞吐 / 延迟扫描", ("bench_summary.json",)),
    ("concurrency", "串行 vs 并发", ("concurrency_short.json",)),
    ("quality", "能力评分", ("quality/quality_summary.json",)),
    ("cold_start", "冷启动", ("loadtest_summary.json",)),
    ("smoke", "接口冒烟", ("chat_smoke_keyed.json", "chat_smoke.json")),
)


def _dimension_summary(run_dir: Path, key: str) -> str:
    """A one-line headline for a dimension, or '' when it has none to give."""
    try:
        if key == "quality":
            q = _read_json(run_dir / "quality" / "quality_summary.json") or {}
            total = sum((c or {}).get("n") or 0 for c in (q.get("categories") or {}).values())
            passed = sum((c or {}).get("passed") or 0 for c in (q.get("categories") or {}).values())
            return "%d/%d 通过 (%.2f%%)" % (passed, total, 100.0 * passed / total) if total else ""
        if key == "throughput":
            b = _read_json(run_dir / "bench_summary.json") or {}
            per = b.get("per_size") or {}
            if not per:
                return ""
            first = per[sorted(per, key=lambda s: int(s))[0]] or {}
            return "解码 %s tok/s @ %s tok 输入" % (
                first.get("engine_decode_tok_s"), first.get("prompt_tokens"))
        if key == "concurrency":
            c = _read_json(run_dir / "concurrency_short.json") or {}
            rows = c.get("rows") or []
            if not rows:
                return ""
            rs = [r.get("ratio_conc_over_seq") for r in rows
                  if isinstance(r.get("ratio_conc_over_seq"), (int, float))]
            return "并发/串行 %s" % (round(sum(rs) / len(rs), 3) if rs else "—")
        if key == "cold_start":
            l = _read_json(run_dir / "loadtest_summary.json") or {}
            if l.get("first_output_s") is None:
                return ""
            return "首 token %ss（%s，解码 %s tok/s）" % (
                l.get("first_output_s"), l.get("phase"), l.get("engine_decode_tok_s"))
        if key == "smoke":
            s = _read_json(run_dir / "chat_smoke_keyed.json") \
                or _read_json(run_dir / "chat_smoke.json") or {}
            # These artifacts record no HTTP status -- there is no such key in
            # them.  Going by a status field would report every smoke run as
            # "present but unsummarised"; what they *do* record is time to
            # first token and a completion, which is what a smoke test is for.
            if s.get("ttft_first_content_token_s") is None:
                return ""
            return "首内容 token %ss / 端到端 %ss / 出 %s tok" % (
                round(s["ttft_first_content_token_s"], 3),
                round(s.get("e2e_s") or 0, 3),
                (s.get("usage") or {}).get("completion_tokens"))
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _evalscope_reports() -> dict:
    """Ask EvalScope's own service for its report list.

    Its SPA answers HTTP 200 to *every* unknown path -- /api/v1/nonsense
    included -- so a status-code check would read an HTML page as "0 reports"
    and call that a successful query.  The only way to know the API really
    answered is to parse the body and check for the keys it documents.
    """
    st = evalscope_state()
    if not st["up"]:
        return {"ok": False, "reason": "not listening on localhost:%d" % st["port"]}
    url = st["url"].rstrip("/") + "/api/v1/reports"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(body, dict) or "reports" not in body or "total" not in body:
        return {"ok": False, "reason": "response was not the reports API (SPA fallback?)"}
    return {"ok": True, "total": body.get("total"),
            "reports": body.get("reports") or [], "filters": body.get("filters") or {}}


def evaluation_summary() -> dict:
    runs = list_runs()
    per_run, models, gaps = [], set(), []

    for r in runs:
        d = Path(r["root"]) / r["run"]
        dims, headline = {}, None
        for key, label, filenames in EVAL_DIMENSIONS:
            present = any((d / f).is_file() for f in filenames)
            dims[key] = {"label": label, "present": present,
                         "summary": _dimension_summary(d, key) if present else ""}
            if not present:
                gaps.append({"run": r["run"], "dimension": key, "label": label})
        cfg = _read_json(d / "configuration.json") or {}
        model = ((cfg.get("model") or {}).get("name")) or ""
        if model:
            models.add(model)
        q = _read_json(d / "quality" / "quality_summary.json") or {}
        total = sum((c or {}).get("n") or 0 for c in (q.get("categories") or {}).values())
        passed = sum((c or {}).get("passed") or 0 for c in (q.get("categories") or {}).values())
        if total:
            headline = "%d/%d 通过 (%.2f%%)" % (passed, total, 100.0 * passed / total)
        per_run.append({
            "run": r["run"], "at": r["mtime"], "files": r["files"], "bytes": r["bytes"],
            "model": model, "dimensions": dims, "headline": headline,
            "present": sum(1 for v in dims.values() if v["present"]),
            "expected": len(EVAL_DIMENSIONS),
        })

    ev = evalscope_state()
    reports = _evalscope_reports() if ev["up"] else {"ok": False, "reason": "service down"}
    # An empty EvalScope store is a coverage gap in its own right, and it is the
    # kind that a benchmark-folders-only view can never show: the runs were
    # produced outside EvalScope, so EvalScope has nothing to compare them to.
    if reports.get("ok") and not reports.get("total"):
        gaps.append({"run": "", "dimension": "evalscope",
                     "label": "EvalScope 自身结果库为空（现有 run 由外部流程产出，未进入 EvalScope）"})
    elif not reports.get("ok"):
        gaps.append({"run": "", "dimension": "evalscope",
                     "label": "EvalScope 报告 API 未取到：%s" % reports.get("reason", "")})

    latest = per_run[0] if per_run else None
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runs": per_run,
        "latest": latest,
        "model_count": len(models),
        "models": sorted(models),
        "gaps": gaps,
        "coverage": {
            "expected_per_run": len(EVAL_DIMENSIONS),
            "present": sum(r["present"] for r in per_run),
            "expected": len(EVAL_DIMENSIONS) * len(per_run),
        },
        "evalscope": dict(ev, reports=reports),
    }


# ===========================================================================
# on-demand service control
#
# The brief asks for EvalScope to be startable on demand rather than blocking
# the homepage.  That is a real capability, so it is built explicitly and
# narrowly: the *only* argv that can ever be executed is one that already
# exists in this project's own config file, and the request contributes nothing
# but the name of a block to look up.  There is no shell, no parameter that
# reaches a command line, and no way to name a program that is not already
# written down here.
# ===========================================================================

def managed_services() -> dict:
    out = {}
    for key, svc in (CONFIG.get("services", {}) or {}).items():
        svc = svc or {}
        if svc.get("managed") and svc.get("launch"):
            out[key] = svc
    return out


def portal_managed(key: str) -> bool:
    """May the portal's *console* switch this service, as opposed to the host?

    Two different questions, and they only come apart for one kind of service --
    the reverse proxy.  `managed` answers "may this host supervise it at all",
    and for the proxy the answer has to be yes: it is the front door, and the
    cron hook is what brings it back after a reboot.  But the console it fronts
    is the wrong place to switch it off, because the reply to that click travels
    *through the thing being stopped*: the operator stops it, the response
    arrives, the page dies, and the button that would turn it back on is behind
    the service that is now down.  A control whose failure mode is losing the
    console is not a control.

    So the gate is here rather than in ``start_service``/``stop_service``, which
    stay shared.  ``deploy/templates/labctl.py`` imports those two directly and is
    the operator's shell -- it must be able to restart the proxy, and it is not
    the console, so this check does not apply to it.

    Default is ``managed``, so every service written before this existed keeps
    exactly the behaviour it had.
    """
    svc = ((CONFIG.get("services", {}) or {}).get(key, {}) or {})
    return bool(svc.get("portal_managed", svc.get("managed", False)))


def stop_service(key: str) -> tuple[int, dict]:
    """Stop one managed service -- but only if the process is verifiably ours.

    A pid file records a number, and numbers are recycled.  Killing on the
    number alone means that some day the service dies, the pid is handed to
    something unrelated, and the next "stop" kills a stranger.  So identity is
    re-derived from live kernel state and compared with what was written at
    start: the start time (identical only while it is the same process) and the
    process group.  A mismatch refuses rather than guesses.

    Idempotent by construction: every path that reaches an end state also
    records that the user wanted it stopped, so an autostart reading that record
    will not bring it back.
    """
    svc = managed_services().get(key)
    if svc is None:
        return 404, {"ok": False, "error": "not a managed service here: %r" % key,
                     "managed": sorted(managed_services())}

    pid = _read_pid_file("%s.pid" % key)
    rec = _read_proc_record(key)
    if not pid:
        _merge_proc_record(key, disabled_by_user=True)
        return 200, {"ok": True, "stopped": False, "state": "stopped",
                     "note": "no pid file; nothing was running. Marked stopped so an "
                             "autostart will not bring it back"}

    alive = _pid_alive(pid)
    now_start, was_start = _proc_starttime(pid), rec.get("starttime")

    if alive and was_start is not None and now_start is not None \
            and int(now_start) != int(was_start):
        # Same number, different process.  This is the case the check exists
        # for, and the correct action is to touch nothing at all.
        _merge_proc_record(key, disabled_by_user=True)
        return 409, {"ok": False, "state": "stale", "pid": pid,
                     "error": "pid %s is alive but started at %s, not the recorded %s; "
                              "refusing to kill a recycled pid" % (pid, now_start, was_start),
                     "note": "nothing was signalled; the pid file was left as-is"}
    if not alive:
        _merge_proc_record(key, disabled_by_user=True)
        return 200, {"ok": True, "stopped": False, "state": "down", "pid": pid,
                     "note": "recorded pid is not running; marked stopped"}
    if not svc.get("managed"):
        return 409, {"ok": False, "state": "up", "pid": pid,
                     "error": "%r is not a managed service here; the portal does not "
                              "stop processes it did not start" % key}

    pgid = rec.get("pgid")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        elif pgid and int(pgid) == int(pid):
            # Our own process group, which exists because start_service used
            # setsid().  Signalling the group reaches the children the service
            # itself spawned and nothing outside it.
            os.killpg(int(pgid), signal.SIGTERM)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "state": "up", "pid": pid,
                     "error": "%s: %s" % (type(exc).__name__, exc)}

    _merge_proc_record(key, disabled_by_user=True,
                       stopped_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return 200, {"ok": True, "stopped": True, "state": "stopping", "pid": pid,
                 "note": "TERM sent; the flag is set so nothing auto-restarts it"}


def service_state(key: str) -> dict:
    """Live state of one configured service, by name.

    EvalScope and Grafana are two instances of the same question, so this is one
    function rather than one per service: a second copy of this logic is a
    second copy that drifts, and the drift shows up as two pages disagreeing
    about whether the same process is up.

    ``url`` is the *browser-facing* address and may be empty when nothing has
    been exposed yet; ``internal_url`` is the server-side one and is what the
    portal itself dials.  Keeping them as separate fields is the whole point --
    collapsing them is how a loopback address reaches a remote reader.
    """
    svc = ((CONFIG.get("services", {}) or {}).get(key, {}) or {})
    port = int(svc.get("port", 0) or 0)
    pid = _read_pid_file("%s.pid" % key)
    rec = _read_proc_record(key)
    # Where to look for the listener.  Loopback is the default and is right for
    # everything that binds loopback, but it is an assumption, and the one
    # service that breaks it is the reverse proxy -- the only one here that
    # binds a routable address.  Probing localhost for it reports `down` on a
    # working proxy, permanently: the process is up, the port is open, and the
    # status page says otherwise.  Measured on 2026-10-10, first start.
    up = tcp_open(str(svc.get("probe_host", "localhost")), port) if port else False
    alive = _pid_alive(pid)
    # The middle state matters: EvalScope imports for minutes before it binds,
    # so "not listening" splits into "still starting" and "gone".  They need
    # different repairs and a boolean would merge them.  A user who stopped the
    # service on purpose is a third case again -- it is not a fault, and an
    # autostart that silently re-enabled it would be overriding them.
    stopped_by_user = bool(rec.get("disabled_by_user"))
    state = "stopped" if stopped_by_user else ("up" if up else ("starting" if alive else "down"))
    internal = svc.get("url") or ("http://localhost:%d" % port if port else "")
    entry = entry_url(key, internal)

    # Declared dependencies, resolved to live state.  Grafana answers on :3000
    # whether or not anything is feeding it, so a green dot on Grafana alone
    # reports "the web UI responds" while the charts stay empty -- the failure
    # is in the chain, not in the service.  Naming the chain here means the row
    # can say which link is missing instead of leaving the user to guess.
    #
    # Resolved one level deep and without recursion: these graphs are declared
    # by hand in a config file, and a cycle would otherwise hang the request.
    deps = []
    for name in (svc.get("depends_on") or []):
        name = str(name)
        dsvc = ((CONFIG.get("services", {}) or {}).get(name, {}) or {})
        dport = int(dsvc.get("port", 0) or 0)
        deps.append({
            "key": name,
            "label": dsvc.get("label", name),
            "state": "unknown" if not dsvc else
                     ("up" if dport and tcp_open(str(dsvc.get("probe_host", "localhost")),
                                                 dport) else "down"),
        })
    return {
        "key": key, "label": svc.get("label", key),
        "url": entry["url"], "internal_url": entry["internal_url"], "via": entry["via"],
        "port": port, "up": up, "pid": pid, "pid_alive": alive, "state": state,
        "managed": bool(svc.get("managed", False)),
        # Whether the console may toggle it, which is not the same as whether
        # the host supervises it.  Published so the row can render a state
        # without a switch rather than offering a control that will be refused.
        "portal_managed": portal_managed(key),
        "enabled": bool(svc.get("enabled", False)),
        "stopped_by_user": stopped_by_user,
        "depends_on": deps,
        "deps_down": [d["key"] for d in deps if d["state"] != "up"],
        "note": svc.get("note", ""),
    }


def evalscope_state() -> dict:
    return service_state("evalscope")


def start_service(key: str) -> tuple[int, dict]:
    svc = managed_services().get(key)
    if svc is None:
        return 404, {"ok": False,
                     "error": "not a managed service here: %r" % key,
                     "managed": sorted(managed_services())}

    port = int(svc.get("port", 0) or 0)
    if port and tcp_open("localhost", port):
        return 200, {"ok": True, "started": False, "state": "up",
                     "note": "already listening on localhost:%d" % port}
    pid = _read_pid_file("%s.pid" % key)
    if _pid_alive(pid):
        return 200, {"ok": True, "started": False, "state": "starting", "pid": pid,
                     "note": "pid %s is alive but has not bound yet; not starting a second one" % pid}

    argv = [str(a).replace("{port}", str(port)).replace("{host}", "localhost")
            for a in (svc.get("launch") or [])]
    exe = Path(argv[0])
    exe = exe if exe.is_absolute() else (ROOT / argv[0])
    if not exe.is_file():
        return 500, {"ok": False, "state": "down",
                     "error": "launch target is missing: %s" % exe}
    argv[0] = str(exe)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Same reason as the Windows launcher had: this child prints a banner with an
    # emoji, and a non-UTF-8 console turns that print into UnicodeEncodeError the
    # instant it binds -- the service is up but silently dead.  Cheap insurance
    # on Linux and load-bearing on a GBK code page.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Per-service environment.  Grafana's `root_url` has to name the address the
    # *browser* will use, which is a property of the site and not of the program
    # -- so it belongs in config, next to the public_urls that must agree with
    # it, rather than baked into a generated custom.ini that the next person
    # has to find and edit on the server.
    for k, v in (svc.get("env") or {}).items():
        env[str(k)] = str(v).replace("{port}", str(port)).replace("{host}", "localhost")

    popen_kw: dict = {"stdout": None, "stderr": None, "stdin": subprocess.DEVNULL,
                      "close_fds": True}
    if os.name == "nt":
        # DETACHED_PROCESS so it outlives this request; NEW_PROCESS_GROUP so a
        # Ctrl-C aimed at the console does not reach it.
        popen_kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        # setsid().  Windows gets "outlives the console" from a creation flag;
        # POSIX has no such flag, so without this the child stays in the
        # portal's session and process group -- and then a group kill aimed at
        # the portal takes the service down with it, which is precisely what
        # this project must not do.  It also makes pid == pgid == sid, so the
        # recorded group is exact rather than inferred.
        popen_kw["start_new_session"] = True

    try:
        out_f = open(LOG_DIR / ("%s-%s.out.log" % (key, stamp)), "ab")
        err_f = open(LOG_DIR / ("%s-%s.err.log" % (key, stamp)), "ab")
        popen_kw["stdout"], popen_kw["stderr"] = out_f, err_f
        proc = subprocess.Popen(
            argv, cwd=str(ROOT / svc.get("cwd", ".")), env=env, **popen_kw)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "state": "down",
                     "error": "%s: %s" % (type(exc).__name__, exc)}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / ("%s.pid" % key)).write_text(str(proc.pid), encoding="ascii")
    rec = _write_proc_record(key, proc)
    # Starting it is the user saying they want it running, so the "stopped on
    # purpose" flag is cleared here and only here.  An autostart reads that flag
    # and must not set it.
    _merge_proc_record(key, disabled_by_user=False)
    # A declared dependency that is still down does not block the start --
    # refusing would mean the one page that shows you what is broken is the page
    # you cannot open.  It is reported instead, so "started" is never read as
    # "the whole chain works".
    missing = [d["key"] for d in service_state(key).get("depends_on", [])
               if d["state"] != "up"]
    return 200, {"ok": True, "started": True, "pid": proc.pid, "state": "starting",
                 "argv": argv, "pgid": rec.get("pgid"),
                 "deps_down": missing,
                 "note": ("launched; it imports for a while before it binds"
                          + ("" if not missing else
                             " -- but %s is not up, so it will start with an empty data set"
                             % ", ".join(missing)))}


# ===========================================================================
# v2 API -- an additive, enveloped layer over the routes above
#
# Nothing here changes a v1 route.  The reason for an envelope at all is that
# "HTTP 200" is not a health claim on this workstation: an unrelated service on
# :18080 answers 200 to *every* path, so a status-code check reports a healthy
# system while serving somebody else's page.  Every v2 answer therefore carries
# an explicit ok / partial / error verdict, and each block's *shape* is checked
# with ``_looks_like`` before it is trusted.
# ===========================================================================

CACHE_EVAL = Cache(30)


def _v2(data, source: str, ok: bool = True, error=None, partial: bool = False) -> dict:
    """The uniform v2 envelope.

    ``ok=False`` still ships HTTP 200: the envelope is the machine-readable
    verdict about the *data*, and a non-2xx status would make a browser fetch
    throw before the frontend can read which block degraded.  Only a route that
    does not exist is a 404 (see ``do_GET``).
    """
    return {
        "ok": bool(ok),
        "data": data,
        "error": error,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
        "partial": bool(partial),
    }


def _looks_like(payload, required_keys) -> bool:
    """True when *payload* is a dict carrying every key in *required_keys*.

    A status code cannot answer this question.  An unrelated service on this
    workstation's :18080 returns 200 on every path, and the EvalScope SPA below
    does the same -- so "200 with a parseable body" would accept somebody else's
    page as our data (the SPA fallback parses as HTML too).  Requiring the keys
    the API actually documents is the difference between "something answered"
    and "the thing we asked for answered".
    """
    return isinstance(payload, dict) and all(k in payload for k in required_keys)


def _node_public() -> dict:
    """The node block ``Handler._lab`` publishes: identity only, no key material.

    Kept in one place so the v2 endpoints and ``/api/lab`` cannot drift on what
    "the node" is.
    """
    return {
        "label": NODE.get("label", ""),
        "host": NODE.get("host", ""),
        "hostname": NODE.get("hostname", ""),
        "ssh_user": NODE.get("ssh_user", ""),
    }


def _portal_is_local_only() -> bool:
    """True when the only browser that can reach this portal is on this host."""
    return str(PORTAL.get("host", "localhost")) in ("localhost", "localhost", "::1")


def _fronted_by_proxy() -> bool:
    """True when something sits in front of this portal and serves a browser.

    Binding loopback is evidence about *sockets*, not about readers, and the
    two came apart the moment the reverse proxy was deployed: the portal still
    binds localhost -- it must, that is the boundary -- while the browser asking
    for the page is on another machine entirely.

    Without this, ``entry_url`` fell through to its "the reader is necessarily
    on this host" branch and handed a remote browser ``http://localhost:9000``
    for anything without a public URL configured.  On the reader's machine that
    address resolves to the reader, so it fails in a way indistinguishable from
    the service being down.  Stated as configuration because it is a fact about
    the deployment that nothing on this host can observe: a proxy is an outside
    process, and from in here its absence and its presence look identical.
    """
    return bool(PORTAL.get("behind_proxy", False))


def entry_url(key: str, internal: str) -> dict:
    """The address a *browser* should be handed for one component.

    The server talks to components on loopback; a browser talks to them through
    the reverse proxy, on a different origin entirely.  Those are two different
    addresses for one thing, and conflating them is how a page ends up telling a
    remote browser to open ``http://localhost:9000`` -- an address that resolves
    to the *viewer's own* machine and fails in a way indistinguishable from the
    service being down.

    The rule is stated rather than left as a fallback chain, so that "not
    exposed" is a representable answer:
      * a configured public URL wins, always;
      * with none, and a portal that only a local browser can reach, the internal
        address is right, because the reader is necessarily on this host;
      * otherwise the entry is *not exposed* and the caller must render that
        state instead of publishing a loopback address that cannot work for
        whoever is reading.
    """
    pub = str((CONFIG.get("public_urls", {}) or {}).get(key, "") or "").strip()
    if pub:
        return {"url": pub, "internal_url": internal, "via": "reverse-proxy"}
    if _portal_is_local_only() and not _fronted_by_proxy():
        return {"url": internal, "internal_url": internal, "via": "loopback"}
    return {"url": "", "internal_url": internal, "via": "not-exposed"}


def _proc_record_path(key: str) -> Path:
    return RUN_DIR / ("%s.proc" % key)


def _read_proc_record(key: str) -> dict:
    try:
        doc = json.loads(_proc_record_path(key).read_text(encoding="utf-8", errors="replace"))
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _merge_proc_record(key: str, **fields) -> None:
    """Update the record in place, so a field one writer sets is not lost by
    another writer that only knows about its own."""
    rec = _read_proc_record(key)
    rec.update(fields)
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        _proc_record_path(key).write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _endpoint_credential(e: dict):
    """(key_present, key_sha256_12) for one endpoint block -- never the value."""
    var = e.get("api_key_var", "")
    if not var:
        return None, None
    val = read_secret(var, e.get("secret_file", "config/secrets.env"))
    # Same digest shape as StrataClient.key_fingerprint: identifies which key is
    # loaded without revealing it.
    return bool(val), (hashlib.sha256(val.encode()).hexdigest()[:12] if val else None)


def _evaluation_cached() -> dict:
    # evaluation_summary() re-reads every run directory and may call EvalScope's
    # reports API -- far too much work for a short poll.  It gets its own TTL;
    # /api/evaluation keeps its existing uncached behaviour.
    return CACHE_EVAL.get(evaluation_summary)


DISK_WARN_PCT, DISK_CRIT_PCT, MEM_WARN_PCT = 80.0, 90.0, 90.0


def _alerts(strata: dict, ssh: dict, tun: dict, ev: dict, services: list) -> list[dict]:
    """Real problems only, each derived from a live probe -- never seeded.

    An empty list means nothing is wrong, and that is a valid answer.  A
    decorative info line here would make "no alerts" indistinguishable from
    "nobody looked".
    """
    out: list[dict] = []

    def add(level: str, title: str, detail: str, source: str) -> None:
        out.append({"level": level, "title": title, "detail": detail, "source": source})

    ma = strata.get("model_api") or {}
    if ma and not ma.get("ok"):
        add("critical", "模型 API 不可用",
            ma.get("error") or ma.get("health_error") or "model_api.ok = false",
            "strata_console().model_api")
    elif ma.get("ok") and ma.get("loaded") is False:
        add("warning", "模型未加载",
            "/health reports loaded = false for %s" % (strata.get("base_url") or "node"),
            "strata_console().model_api.loaded")

    gpu = strata.get("gpu") or {}
    if gpu and gpu.get("available") is False:
        add("warning", "GPU 遥测不可用", gpu.get("reason") or "gpu.available = false",
            "strata_console().gpu")

    if isinstance(ssh, dict) and ssh.get("ok") is False:
        add("critical", "%s 探针失败" % _probe_label(), ssh.get("error") or "node.ok = false",
            "probe_node()")
    fp = ssh.get("fingerprint") if isinstance(ssh, dict) else None
    if isinstance(fp, dict) and fp.get("checked") and not fp.get("match"):
        add("critical", "主机密钥指纹不匹配",
            "project known_hosts does not contain the pinned host key",
            "verify_pinned_fingerprint()")

    if tun.get("foreign"):
        add("warning", "隧道端口被其他进程占用",
            "localhost:%s is listening but our pid %s is not alive"
            % (tun.get("local_port"), tun.get("pid")), "tunnel_state().foreign")
    elif tun.get("enabled") and not tun.get("listening"):
        add("warning", "隧道未建立",
            "configured local_port %s is not listening" % tun.get("local_port"),
            "tunnel_state()")

    if ev.get("enabled") and ev.get("state") == "down":
        add("warning", "EvalScope 未运行",
            "enabled but neither listening on localhost:%s nor holding a live pid"
            % ev.get("port"), "evalscope_state().state")

    facts = (ssh.get("facts") or {}) if isinstance(ssh, dict) else {}
    mem = facts.get("mem") or {}
    if isinstance(mem.get("used_pct"), (int, float)) and mem["used_pct"] >= MEM_WARN_PCT:
        add("warning", "节点内存高水位", "%.1f%% of RAM in use" % mem["used_pct"],
            "probe_node().facts.mem")
    for d in facts.get("disks") or []:
        pct = d.get("used_pct")
        if not isinstance(pct, (int, float)):
            continue
        if pct >= DISK_CRIT_PCT:
            add("critical", "节点磁盘将满", "%s 已用 %.1f%%" % (d.get("mount"), pct),
                "probe_node().facts.disks")
        elif pct >= DISK_WARN_PCT:
            add("warning", "节点磁盘高水位", "%s 已用 %.1f%%" % (d.get("mount"), pct),
                "probe_node().facts.disks")

    for s in services or []:
        # evalscope is reported above with its three-state detail; the rest only
        # earn an alert when the config says they are supposed to be up and the
        # lab does not own their lifecycle.
        if s.get("key") == "evalscope" or not s.get("enabled"):
            continue
        if not s.get("up") and not s.get("managed"):
            add("warning", "%s 未监听" % (s.get("label") or s.get("key")),
                "enabled service is not listening on port %s" % s.get("port"),
                "probe_services().up")
    return out


def _compact_strata(s: dict) -> dict:
    """The live poll only needs a few fields; the full console payload is for
    the overview request, which is made far less often."""
    ma = s.get("model_api") or {}
    g = s.get("gpu") or {}
    el = s.get("engine_live") or {}
    models = ma.get("model_list") or []
    return {
        "at": s.get("at"),
        "base_url": s.get("base_url"),
        "key_present": s.get("key_present"),
        "model_api": {
            "ok": ma.get("ok"),
            "model": (models[0].get("id") if models else None),
            "loaded": ma.get("loaded"),
            "auth_required": ma.get("auth_required"),
            "error": ma.get("error"),
        },
        "gpu": {
            "available": g.get("available"),
            "name": g.get("name"),
            "util_pct": g.get("util_pct"),
            "temp_c": g.get("temp_c"),
            "used_mib": g.get("used_mib"),
            "total_mib": g.get("total_mib"),
            "source": g.get("source"),
            "reason": g.get("reason"),
        },
        "engine_live": {"state": el.get("state"), "tok_s": el.get("tok_s"),
                        "queued": el.get("queued")},
    }


def _compact_ssh(ssh: dict) -> dict:
    f = ssh.get("facts") or {}
    mem = f.get("mem") or {}
    return {
        "ok": ssh.get("ok"),
        "at": ssh.get("at"),
        "elapsed_s": ssh.get("elapsed_s"),
        "error": ssh.get("error"),
        "hostname": f.get("hostname"),
        "uptime_s": f.get("uptime_s"),
        "loadavg": f.get("loadavg"),
        "cpu_count": f.get("cpu_count"),
        "mem": {"used_pct": mem.get("used_pct"), "total_mib": mem.get("total_mib"),
                "avail_mib": mem.get("avail_mib")},
        "disks": [{"mount": d.get("mount"), "used_pct": d.get("used_pct"),
                   "used_gib": d.get("used_gib"), "total_gib": d.get("total_gib")}
                  for d in (f.get("disks") or [])],
        "fingerprint": ssh.get("fingerprint"),
    }


def v2_overview() -> dict:
    """Everything the homepage renders, in one request."""
    strata = strata_console()
    ssh = CACHE_SSH.get(probe_node)
    tun = tunnel_state()
    ev = evalscope_state()
    services = probe_services()
    evsum = _evaluation_cached()
    latest = evsum.get("latest") or {}

    problems = []
    if not _looks_like(strata, ("model_api", "gpu")):
        problems.append("strata console payload lacks model_api/gpu")
    if not _looks_like(ssh, ("ok", "at")):
        problems.append("ssh probe payload lacks ok/at")
    if not _looks_like(evsum, ("runs", "coverage", "gaps")):
        problems.append("evaluation summary lacks runs/coverage/gaps")
    if not _looks_like(tun, ("enabled", "listening")):
        problems.append("tunnel state lacks enabled/listening")
    if not _looks_like(ev, ("state", "up")):
        problems.append("evalscope state lacks state/up")

    data = {
        "node": _node_public(),
        "strata": strata,
        "benchmark": {
            "run": latest.get("run"),
            "at": latest.get("at"),
            "model": latest.get("model"),
            "headline": latest.get("headline"),
            "present": latest.get("present"),
            "expected": latest.get("expected"),
            "dimensions": latest.get("dimensions") or {},
        } if latest else None,
        "reports": list_reports()[:5],
        "alerts": _alerts(strata, ssh, tun, ev, services),
        "tunnel": tun,
        "evalscope": ev,
    }
    return _v2(data, "portal:v2/overview", ok=not problems,
               error=("; ".join(problems) or None), partial=bool(problems))


INTEGRATION_STATUSES = ("connected", "available", "not_installed",
                        "disabled", "planned", "error")

_STATUS_LABEL = {
    "connected": "已连接",
    "available": "可用",
    "not_installed": "未安装",
    "disabled": "已停用",
    "planned": "规划中",
    "error": "异常",
}


def _feishu_probe() -> dict:
    """Ask the module what it can do, rather than asserting a status.

    ``publish()`` is designed to refuse: it raises ``FeishuWriteRefused`` unless
    acknowledged, and raises ``NotImplementedError`` even then because no
    credential path exists in this project.  Probing that behaviour is what
    makes the reported status a measurement instead of a claim.
    """
    try:
        from integrations.feishu import (FeishuWriteRefused, PublishRequest,
                                         plan_publish, publish)
    except Exception as exc:  # noqa: BLE001
        return {"importable": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        plan = plan_publish(PublishRequest(title="probe", markdown_path="",
                                           provenance={"probe": True}), "")
    except Exception as exc:  # noqa: BLE001
        return {"importable": True, "write_path": "plan_failed",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        publish(plan)
        write_path = "open"
    except FeishuWriteRefused:
        write_path = "refused"
    except NotImplementedError:
        write_path = "unimplemented"
    except Exception:  # noqa: BLE001
        write_path = "error"
    return {"importable": True, "write_path": write_path}


def v2_integrations() -> dict:
    """One row per external system: status from real config plus a real probe."""
    strata = strata_console()
    ev = evalscope_state()
    eps = CONFIG.get("endpoints", {}) or {}
    svcs = CONFIG.get("services", {}) or {}
    items: list[dict] = []

    # --- Strata -----------------------------------------------------------
    se = eps.get("strata-local", {}) or {}
    ma, gpu = strata.get("model_api") or {}, strata.get("gpu") or {}
    shaped = _looks_like(strata, ("model_api", "gpu"))
    models = ma.get("model_list") or []
    if not se.get("enabled", False):
        st = "disabled"
    elif not shaped:
        st = "error"
    elif ma.get("ok"):
        st = "connected"
    else:
        st = "error"
    if not shaped:
        detail = "console payload did not carry model_api/gpu"
    elif st == "connected":
        gpu_note = ("GPU %s %s%%" % (gpu.get("name"), gpu.get("util_pct"))
                    if gpu.get("available") else
                    "GPU telemetry unavailable (%s)" % (gpu.get("reason") or "no source"))
        detail = "%s; %s" % ((models[0].get("id") if models else "endpoint ok"), gpu_note)
    else:
        detail = ma.get("error") or ma.get("health_error") or "model API not answering"
    items.append({
        "key": "strata", "label": se.get("label", "Strata"),
        "kind": se.get("kind", "strata"), "status": st,
        "status_label": _STATUS_LABEL[st], "detail": detail,
        "url": se.get("base_url", ""), "reachable": bool(ma.get("ok")), "managed": False,
        "entry": entry_url("strata", se.get("base_url", "")),
        "evidence": ("config [endpoints.strata-local].enabled = %s; "
                     "strata_console().model_api.ok = %s; gpu.available = %s"
                     % (json.dumps(bool(se.get("enabled", False))),
                        json.dumps(ma.get("ok")), json.dumps(gpu.get("available")))),
    })

    # --- EvalScope --------------------------------------------------------
    sev = svcs.get("evalscope", {}) or {}
    if not sev.get("enabled", False):
        st = "disabled"
    elif ev.get("state") == "up":
        st = "connected"
    else:
        # "starting" / "down": configured and managed, just not bound yet, so
        # it is reachable-in-principle rather than connected or broken.
        st = "available"
    label = {"up": "已连接", "starting": "启动中", "down": "未运行（可启动）"}
    items.append({
        "key": "evalscope", "label": ev.get("label") or sev.get("label", "EvalScope"),
        "kind": "service", "status": st,
        "status_label": label.get(ev.get("state"), _STATUS_LABEL[st]),
        "detail": ("state = %s; port %s; pid %s (alive = %s)"
                   % (ev.get("state"), ev.get("port"), ev.get("pid"),
                      json.dumps(bool(ev.get("pid_alive"))))),
        # `url` is the server-side address in every row, and `entry` is what a
        # browser should open.  Two rows were built the other way round and the
        # inconsistency is invisible until a reader is handed the wrong one --
        # service_state already computed both, so this reuses its answer rather
        # than deriving a second one that could disagree with it.
        "url": ev.get("internal_url", ""), "reachable": bool(ev.get("up")),
        "managed": bool(ev.get("managed")),
        "entry": {"url": ev.get("url", ""), "internal_url": ev.get("internal_url", ""),
                  "via": ev.get("via", "")},
        "evidence": ("config [services.evalscope].enabled = %s, managed = %s; "
                     "evalscope_state().state = %s"
                     % (json.dumps(bool(sev.get("enabled", False))),
                        json.dumps(bool(sev.get("managed", False))),
                        json.dumps(ev.get("state")))),
    })

    # --- Grafana ----------------------------------------------------------
    sg = svcs.get("grafana", {}) or {}
    genabled = bool(sg.get("enabled", False))
    gup = tcp_open("localhost", int(sg.get("port", 0))) if (genabled and sg.get("port")) else False
    st = "planned" if not genabled else ("connected" if gup else "error")
    items.append({
        "key": "grafana", "label": sg.get("label", "Grafana"), "kind": "service",
        "status": st, "status_label": _STATUS_LABEL[st],
        "detail": sg.get("note", "") or ("listening" if gup else "not listening"),
        "url": sg.get("url", ""), "reachable": gup,
        "managed": bool(sg.get("managed", False)),
        "entry": entry_url("grafana", sg.get("url", "")),
        "evidence": ("config [services.grafana].enabled = %s, managed = %s"
                     % (json.dumps(genabled), json.dumps(bool(sg.get("managed", False))))),
    })

    # --- DeepSeek Gateway -------------------------------------------------
    sd = eps.get("deepseek-gateway", {}) or {}
    denabled, dbase = bool(sd.get("enabled", False)), (sd.get("base_url") or "")
    st = "disabled" if not denabled else ("available" if dbase else "error")
    items.append({
        "key": "deepseek-gateway", "label": sd.get("label", "DeepSeek Gateway"),
        "kind": sd.get("kind", "openai-compatible"), "status": st,
        "status_label": _STATUS_LABEL[st],
        "detail": sd.get("note", "") or ("base_url = %r" % dbase),
        "url": dbase, "reachable": False, "managed": False,
        "evidence": ("config [endpoints.deepseek-gateway].enabled = %s, base_url = %s"
                     % (json.dumps(denabled), json.dumps(dbase))),
    })

    # --- Production API ---------------------------------------------------
    # Looking it up rather than assuming: if no block names a production API,
    # the honest answer is that this project does not have one configured.
    prod = next((k for k, v in eps.items()
                 if "production" in k.lower()
                 or "production" in str((v or {}).get("label", "")).lower()), None)
    if prod:
        pe = eps[prod] or {}
        st = "available" if pe.get("base_url") else "disabled"
        items.append({
            "key": "production-api", "label": pe.get("label", prod),
            "kind": pe.get("kind", ""), "status": st,
            "status_label": _STATUS_LABEL[st], "detail": "declared endpoint %s" % prod,
            "url": pe.get("base_url", ""), "reachable": False,
            "managed": False,
            "evidence": "config [endpoints.%s] present" % prod,
        })
    else:
        items.append({
            "key": "production-api", "label": "Production API", "kind": "",
            "status": "not_installed", "status_label": _STATUS_LABEL["not_installed"],
            "detail": "no production API is configured in this project",
            "url": "", "reachable": False, "managed": False,
            "evidence": ("config has no [endpoints.*] block for a production API; "
                         "endpoint keys present: %s" % json.dumps(sorted(eps))),
        })

    # --- Feishu -----------------------------------------------------------
    fprobe = _feishu_probe()
    if not fprobe.get("importable"):
        st, detail = "error", ("integrations/feishu.py did not import: %s"
                               % fprobe.get("error"))
    elif fprobe.get("write_path") == "open":
        st, detail = "connected", "publish() returned -- a write path is live"
    else:
        st = "planned"
        detail = ("publish interface designed; the write path is unavailable by "
                  "design (write_path = %s)" % fprobe.get("write_path"))
    items.append({
        "key": "feishu", "label": "Feishu 知识库发布", "kind": "publish-interface",
        "status": st, "status_label": _STATUS_LABEL[st], "detail": detail,
        "url": "", "reachable": False, "managed": False,
        "evidence": ("integrations/feishu.py publish() -> %s; "
                     "docs/feishu-publish-interface.md lists unmet prerequisites "
                     "(credential, space_id, parent_node_token, redirect_uri allow-list)"
                     % fprobe.get("write_path")),
    })

    for it in items:
        if it["status"] not in INTEGRATION_STATUSES:
            raise ValueError("unknown integration status %r" % it["status"])
    errors = [i["key"] for i in items if i["status"] == "error"]
    return _v2({"items": items}, "portal:v2/integrations", ok=not errors,
               error=("integration probe failed: %s" % ", ".join(errors)) if errors else None,
               partial=bool(errors))


def v2_settings() -> dict:
    """Read-only view of the configuration.  No key value, no key file body."""
    endpoints = []
    for key, e in (CONFIG.get("endpoints", {}) or {}).items():
        e = e or {}
        present, fp = _endpoint_credential(e)
        endpoints.append({
            "key": key, "label": e.get("label", key), "kind": e.get("kind", ""),
            "base_url": e.get("base_url", ""), "enabled": bool(e.get("enabled", False)),
            "key_present": present, "key_sha256_12": fp,
        })

    env = env_report()
    data = {
        "lab": CONFIG.get("lab", {}),
        # NODE carries ssh_key as a *path*; the key file's contents are never read
        # here, and never sent.
        "portal": dict(PORTAL),
        "node": dict(NODE),
        "ports": CONFIG.get("ports", {}),
        "paths": CONFIG.get("paths", {}),
        "services": CONFIG.get("services", {}),
        "tunnels": CONFIG.get("tunnels", {}),
        "endpoints": endpoints,
        "env": env,
    }
    problems = []
    if not _looks_like(env, ("python", "tools", "ports")):
        problems.append("env report lacks python/tools/ports")
    if not data["lab"] or not data["node"]:
        problems.append("config lab/node section missing")
    return _v2(data, "portal:v2/settings", ok=not problems,
               error=("; ".join(problems) or None), partial=bool(problems))


def v2_live() -> dict:
    """The lightweight poll target, so a 20s refresh loop does not re-run the
    full console / evaluation work."""
    strata = strata_console()
    ssh = CACHE_SSH.get(probe_node)
    data = {
        "strata": _compact_strata(strata),
        "ssh": _compact_ssh(ssh),
        "tunnel": tunnel_state(),
        "evalscope": evalscope_state(),
        "samples": SAMPLE_BUFFER[-120:],
        "services": probe_services(),
    }
    problems = []
    if not _looks_like(strata, ("model_api", "gpu")):
        problems.append("strata console payload lacks model_api/gpu")
    if not _looks_like(ssh, ("ok", "at")):
        problems.append("ssh probe payload lacks ok/at")
    return _v2(data, "portal:v2/live", ok=not problems,
               error=("; ".join(problems) or None), partial=bool(problems))


# ===========================================================================
# HTTP
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "LLMLabPortal/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter, and no query strings logged
        sys.stderr.write("[portal] %s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._json({"error": msg, "status": code}, code)

    # -- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):  # noqa: C901 - a router is a router
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")

            if path == "/api/lab":
                return self._json(self._lab())

            if path == "/api/live":
                return self._json(live_payload())

            if path == "/api/strata":
                return self._json(strata_console())

            if path == "/api/evaluation":
                return self._json(evaluation_summary())

            if path == "/api/env":
                return self._json(env_report())

            if path == "/api/benchmarks":
                return self._json({"runs": list_runs()})

            if path.startswith("/api/benchmarks/"):
                run = urllib.parse.unquote(path[len("/api/benchmarks/"):])
                doc = run_detail(run)
                return self._json(doc) if doc else self._err(404, "no such run: %s" % run)

            if path == "/api/reports":
                return self._json({"reports": list_reports()})

            if path == "/report":
                return self._report(q.get("p", [""])[0])

            if path == "/file":
                return self._file(q.get("p", [""])[0], download=False)

            if path == "/download":
                return self._file(q.get("p", [""])[0], download=True)

            if path == "/api/export/csv":
                return self._export_csv(q.get("run", [""])[0], q.get("kind", [""])[0])

            if path == "/api/export/zip":
                return self._export_zip(q.get("run", [""])[0])

            if path == "/api/healthz":
                return self._json({"ok": True, "version": VERSION})

            # v2: enveloped, additive.  A v2 path that does not exist still
            # falls through to the 404 below.
            if path == "/api/v2/overview":
                return self._json(v2_overview())

            if path == "/api/v2/integrations":
                return self._json(v2_integrations())

            if path == "/api/v2/settings":
                return self._json(v2_settings())

            if path == "/api/v2/live":
                return self._json(v2_live())

            if path.startswith("/assets/"):
                return self._static(path[len("/assets/"):])

            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")

            return self._err(404, "no route for %s" % path)
        except ValueError as exc:
            return self._err(403, str(exc))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client went away mid-response.  On Windows that arrives as
            # ConnectionAbortedError (WinError 10053) -- which is NOT a subclass
            # of BrokenPipeError, so a bare `except BrokenPipeError` misses it.
            # The handler then tried to send a 500 down the same dead socket,
            # raised a second time, and escaped to socketserver: one full
            # traceback in stderr for every tab that navigates away.  Nothing is
            # wrong with the server, so say nothing.
            return
        except Exception as exc:  # noqa: BLE001
            return self._err(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self):  # noqa: C901
        parsed = urllib.parse.urlsplit(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/api/service/start":
                # A loopback console is still reachable from any page the
                # browser loads, so this needs a gate a cross-origin page
                # cannot pass.  A custom header does it: a cross-origin fetch
                # that sets one triggers a CORS preflight, this server never
                # answers preflights, and the browser blocks the request before
                # it is ever sent.  Same-origin (our own page) needs no
                # preflight and is unaffected.
                if self.headers.get("X-Requested-With") != "llm-lab-portal":
                    return self._err(403, "missing X-Requested-With; refusing cross-origin POST")
                key = q.get("key", [""])[0]
                # Only for keys that name a managed service.  Applied to every
                # key it collapsed two different answers into one: "no such
                # service here" and "that one exists and the console must not
                # touch it" are not the same fact, and the first carries the
                # list of what *is* managed, which is what the reader needs to
                # correct a typo.  An earlier version gated unconditionally and
                # an acceptance check caught the 404 turning into a 403.
                if key in managed_services() and not portal_managed(key):
                    return self._err(403, "the console does not switch %r: stopping it "
                                          "would take down the page you are reading it on"
                                     % key)
                code, payload = start_service(key)
                return self._json(payload, code)

            if path == "/api/service/stop":
                # Same cross-origin gate as start.  This is a *destructive*
                # action, so it is not enough that it be unreachable from other
                # pages -- the service name is still looked up against the
                # managed set and nothing else is accepted.
                if self.headers.get("X-Requested-With") != "llm-lab-portal":
                    return self._err(403, "missing X-Requested-With; refusing cross-origin POST")
                key = q.get("key", [""])[0]
                if key in managed_services() and not portal_managed(key):
                    return self._err(403, "the console does not switch %r: stopping it "
                                          "would take down the page you are reading it on"
                                     % key)
                code, payload = stop_service(key)
                return self._json(payload, code)

            return self._err(404, "no POST route for %s" % path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as exc:  # noqa: BLE001
            return self._err(500, "%s: %s" % (type(exc).__name__, exc))

    # -- handlers ----------------------------------------------------------
    def _static(self, rel: str):
        rel = rel.lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return self._err(403, "bad asset path")
        p = (WEB / rel).resolve()
        if not str(p).startswith(str(WEB.resolve())) or not p.is_file():
            return self._err(404, "asset not found")
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self._send(200, p.read_bytes(), ctype)

    def _lab(self):
        ep = []
        for key, e in (CONFIG.get("endpoints", {}) or {}).items():
            e = e or {}
            ep.append({
                "key": key,
                "label": e.get("label", key),
                "kind": e.get("kind", ""),
                "base_url": e.get("base_url", ""),
                "deployment": e.get("deployment", ""),
                "enabled": bool(e.get("enabled", False)),
                "note": e.get("note", ""),
                # a truthful credential statement, never the credential
                "key_present": bool(read_secret(e.get("api_key_var", ""),
                                                e.get("secret_file", "config/secrets.env")))
                if e.get("api_key_var") else None,
            })
        return {
            "lab": dict(CONFIG.get("lab", {}) or {}, **{
                # The address this portal is actually bound to.  Published
                # because it is the fact that *licenses* every loopback link in
                # `nav`: a loopback URL is only honest when the reader is on
                # this machine, and the reader is on this machine exactly when
                # the portal is loopback-bound.  Without it published, a reader
                # has to assume, and assuming is how a link that fails on
                # somebody else's machine passes review.
                "portal_host": PORTAL.get("host", "localhost"),
                "portal_port": int(PORTAL.get("port", 8765)),
            }),
            "version": VERSION,
            "node": _node_public(),
            "endpoints": ep,
            "nav": self._nav(),
            "counts": {"reports": len(list_reports()), "runs": len(list_runs())},
            # Config faults found at load time.  Empty is the expected reading;
            # anything in here means the running configuration is not the one
            # written in the file, which is otherwise impossible to notice.
            "config_warnings": list(CONFIG_WARNINGS),
            "now": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _nav(self):
        """The unified entry list: one door per system, no re-implementation.

        Every professional page is opened as a *link* into the system that owns
        it.  Nothing here is embedded -- a Strata panel rendered inside this
        page would be a second copy that drifts from the first, and the brief
        explicitly rules that out.  What the portal adds is the part the three
        systems cannot know about each other: whether each one is reachable
        right now, and from which address.
        """
        tun = tunnel_state()
        ev = evalscope_state()

        # Where the *browser* should be sent for the node's own UI.  The tunnel
        # is one way to make that reachable and a direct address is another;
        # which one is right is a property of the deployment, not of this
        # method, so it is decided once here instead of guessed three times.
        direct = strata_endpoint()["base_url"]
        hp = hostport_of(direct)
        direct_up = bool(hp and tcp_open(hp[0], hp[1]))
        strata_origin = (tun["url"].rstrip("/")
                         if (tun["enabled"] and tun["listening"] and tun["url"])
                         else direct)
        strata_entry = entry_url("strata", strata_origin)
        strata_up = bool(tun["owned"] or tun["listening"] or direct_up)

        # native_panels() composes its URLs from the base it is given, so
        # handing it the *browser-facing* base rebases all of them at once --
        # and handing it "" (nothing exposed) yields url:"" and reachable:False,
        # which is the honest answer rather than a loopback link that would
        # resolve on the reader's own machine.
        panels = native_panels(strata_entry["url"], reachable=strata_up)
        for p in panels:
            p["internal_url"] = strata_entry["internal_url"]
            p["via"] = strata_entry["via"]

        strata_native = {"group": "Strata 原生界面",
                         "note": "节点自带的控制台，本 Portal 不重写其功能",
                         "up": strata_up, "via": strata_entry["via"],
                         "internal_url": strata_entry["internal_url"],
                         "items": [dict(p, external=True) for p in panels]}

        graf = service_state("grafana")
        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "groups": [
                {"group": "本 Portal", "note": "本机聚合视图", "up": True, "items": [
                    {"key": "home", "label": "总览", "note": "模型 / 系统 / 评测",
                     "url": "/", "external": False, "reachable": True},
                    {"key": "bench", "label": "Benchmark", "note": "本机跑出的结果",
                     "url": "#bench", "external": False, "reachable": True},
                    {"key": "reports", "label": "报告", "note": "reports/ 下的文档",
                     "url": "#reports", "external": False, "reachable": True},
                    {"key": "env", "label": "环境自检", "note": "本机依赖与端口",
                     "url": "#env", "external": False, "reachable": True},
                ]},
                strata_native,
                {"group": "EvalScope", "note": "评测与性能压测的专业系统",
                 "up": ev["up"], "state": ev["state"], "via": ev["via"],
                 "internal_url": ev["internal_url"],
                 "items": [
                     {"key": "evalscope", "label": "Dashboard", "note": "EvalScope 原生界面",
                      "url": ev["url"], "internal_url": ev["internal_url"],
                      "via": ev["via"], "external": True, "reachable": ev["up"]},
                 ]},
                {"group": "Grafana",
                 "note": graf["note"] or "历史监控与趋势",
                 "up": graf["up"], "state": graf["state"], "via": graf["via"],
                 "internal_url": graf["internal_url"],
                 "items": [
                     {"key": "grafana", "label": "Grafana", "note": graf["note"] or "监控面板",
                      "url": graf["url"], "internal_url": graf["internal_url"],
                      "via": graf["via"], "external": True, "reachable": graf["up"]},
                 ]},
            ],
            "ports": {
                "portal": int(PORTAL.get("port", 8765)),
                "tunnel": tun["local_port"],
                "evalscope": ev["port"],
            },
        }

    def _report(self, rel: str):
        p = _resolve_public(rel)
        if p.suffix.lower() in (".html", ".htm"):
            return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
        text = _read_text(p)
        title = _first_heading(p) or p.stem
        body = md_to_html(text)
        page = REPORT_SHELL % {
            "title": html.escape(title),
            "path": html.escape(_display_path(p)),
            "body": body,
        }
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _file(self, rel: str, download: bool):
        p = _resolve_public(rel)
        data = p.read_bytes()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        extra = {}
        if download:
            extra["Content-Disposition"] = 'attachment; filename="%s"' % p.name
        return self._send(200, data, ctype, extra)

    def _export_csv(self, run: str, kind: str):
        doc = run_detail(run)
        if not doc:
            return self._err(404, "no such run: %s" % run)
        table = {
            "bench": (doc.get("bench") or {}).get("rows"),
            "concurrency": (doc.get("concurrency") or {}).get("rows"),
            "quality": (doc.get("quality") or {}).get("categories"),
            "files": doc.get("files"),
        }.get(kind)
        if table is None:
            return self._err(404, "run %s has no '%s' table" % (run, kind))
        body = _csv(table).encode("utf-8-sig")  # BOM so Excel reads CJK correctly
        return self._send(200, body, "text/csv; charset=utf-8",
                          {"Content-Disposition":
                           'attachment; filename="%s-%s.csv"' % (run, kind)})

    def _export_zip(self, run: str):
        if "/" in run or "\\" in run or run in ("", ".", ".."):
            return self._err(404, "no such run: %s" % run)
        base = _bench_root_for(run)
        if base is None:
            return self._err(404, "no such run: %s" % run)
        d = base / run
        return self._send(200, _zip_dir(d, run), "application/zip",
                          {"Content-Disposition":
                           'attachment; filename="%s.zip"' % run})


class PortalServer(ThreadingHTTPServer):
    """HTTPServer, minus the reverse-DNS lookup.

    ``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn()`` on the
    bind address to populate ``server_name``.  On Windows that name resolution
    can block for many seconds (and on a machine with an unreachable DNS
    server, effectively forever), during which the process is alive, silently
    not listening, and printing nothing -- which looks exactly like a crash.
    Nothing in this portal reads ``server_name``, so skip the lookup.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Swallow client-disconnect noise raised outside the router.

        An exception that escapes ``do_GET`` -- or one raised while the request
        line is still being read -- lands here, and the stock implementation
        prints a full traceback.  A browser closing a tab is not an incident.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


REPORT_SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s - LLM Lab</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0d0d0d;color:#fff;font:15px/1.7 system-ui,-apple-system,
 "Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 24px 96px}
a{color:#3987e5}
.bar{position:sticky;top:0;background:#0d0d0d;border-bottom:1px solid #2c2c2a;
 padding:12px 24px;display:flex;gap:16px;align-items:center;font-size:13px}
.path{color:#898781;font-family:ui-monospace,Consolas,monospace}
h1,h2,h3{line-height:1.3;margin:1.6em 0 .5em}
h1{font-size:1.7em}h2{font-size:1.3em;border-bottom:1px solid #2c2c2a;padding-bottom:.3em}
h3{font-size:1.08em}
code{background:#1a1a19;border:1px solid #2c2c2a;border-radius:4px;padding:.1em .35em;
 font-size:.9em}
pre{background:#1a1a19;border:1px solid #2c2c2a;border-radius:8px;padding:14px;
 overflow:auto}
pre code{background:none;border:0;padding:0}
table{border-collapse:collapse;width:100%%;margin:1em 0;font-size:.92em}
th,td{border:1px solid #2c2c2a;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#1a1a19;font-weight:600}
tr:nth-child(even) td{background:#121211}
blockquote{border-left:3px solid #3987e5;margin:1em 0;padding:.2em 0 .2em 14px;
 color:#c3c2b7}
hr{border:0;border-top:1px solid #2c2c2a;margin:2em 0}
img{max-width:100%%}
</style></head><body>
<div class="bar"><a href="/">&larr; LLM Lab</a>
<span class="path">%(path)s</span>
<a href="/download?p=%(path)s" style="margin-left:auto">下载原文件</a></div>
<div class="wrap">%(body)s</div></body></html>"""


def main() -> int:
    host = PORTAL.get("host", "localhost")
    port = int(PORTAL.get("port", 8765))

    if host not in ("localhost", "localhost", "::1"):
        print("[portal] REFUSING to bind %s: this console has no authentication and "
              "the brief requires localhost." % host, file=sys.stderr)
        return 2
    if not WEB.is_dir():
        print("[portal] web assets missing at %s" % WEB, file=sys.stderr)
        return 2

    httpd = PortalServer((host, port), Handler)
    print("[portal] LLM Lab %s listening on http://%s:%d" % (VERSION, host, port), flush=True)
    # Printed because the interesting failure here is silent: a server started
    # without LAB_CONFIG reads the workstation config, binds the same loopback
    # port, and answers -- while pointing at paths and a credential that do not
    # exist on this host.  One line at startup turns that into something a log
    # can be read for.
    print("[portal] config: %s" % CONFIG_PATH, flush=True)
    print("[portal] state:  %s" % STATE, flush=True)
    print("[portal] node: %s (%s) mode=%s"
          % (NODE.get("host", "?"), NODE.get("hostname", "?"), node_mode()), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[portal] stopped by request", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
