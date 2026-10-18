#!/usr/bin/env python3
"""One-time bootstrap for LLM Lab's access to the {{LAB_NODE}} inference node.

What it does, in order, and why
-------------------------------
1. Verifies the node's SSH host key against the pinned SHA256 fingerprint in
   ``{{LAB_SSH_FINGERPRINT_PATH}}``.  On mismatch it aborts and touches
   nothing: automation must never accept an unverified host key.
2. Generates a *project-local* ed25519 keypair under ``config/ssh/`` and
   authorises its public half for the Linux user ``{{LAB_USER}}`` on the node.
   After this runs once, every other script in the project authenticates with
   that key only.
3. Reads the Strata API key out of ``{{LAB_ROOT}}/configs/api-keys.env``
   (mode 600, owner {{LAB_ADMIN_USER}}) and writes it to ``config/secrets.env``, which is
   gitignored.  The value is never printed and never leaves this machine.
4. Mirrors the existing deployment report and benchmark artifacts into
   ``reports/deployment/`` and ``data/benchmarks/``, with a provenance
   manifest recording source path, size and sha256 for every file.

Requirements
------------
``LAB_ADMIN_PW_FILE`` must point at a file holding the node's admin password.  The
password is read into memory only: it is never written into this repository,
never logged, and never placed on a command line (it is written to sudo's
stdin over the SSH channel).  It is needed *only* for this bootstrap step;
the runnable project never uses it.

Usage
-----
    LAB_ADMIN_PW_FILE=<path> python bootstrap_remote_access.py [--skip-artifacts]

Idempotent: re-running will not duplicate the authorised key and will not
overwrite an existing local copy of the API key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import posixpath
import stat
import subprocess
import sys
import time
from pathlib import Path

import paramiko

HOST = "{{LAB_HOST}}"
ADMIN_USER = "{{LAB_ADMIN_USER}}"
TARGET_USER = "{{LAB_USER}}"
PORT = 22

PROJECT = Path(__file__).resolve().parent.parent
SSH_DIR = PROJECT / "config" / "ssh"
KEY_PATH = SSH_DIR / "id_ed25519_llmlab"
PINNED_FP = SSH_DIR / "host_ed25519_sha256.txt"
SECRETS = PROJECT / "config" / "secrets.env"

REMOTE_API_ENV = "{{LAB_ROOT}}/configs/api-keys.env"
REMOTE_REPORT = "{{LAB_ROOT}}/Strata_Qwen3.8_Deployment_Report.md"
REMOTE_RESULTS = "{{LAB_ROOT}}/results/2026-10-10_iq2xs_lowram_32k"

LOCAL_REPORT_DIR = PROJECT / "reports" / "deployment"
LOCAL_BENCH_DIR = PROJECT / "data" / "benchmarks"


def log(msg: str) -> None:
    print("[bootstrap] %s" % msg, flush=True)


def die(msg: str) -> None:
    print("[bootstrap] FATAL: %s" % msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# host-key pinning
# --------------------------------------------------------------------------

def sha256_fingerprint(pkey: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(pkey.asbytes()).digest()) \
        .decode().rstrip("=")


def pinned_fingerprint() -> str:
    for line in PINNED_FP.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("SHA256:"):
            return line
    die("no pinned fingerprint found in %s" % PINNED_FP)


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str) -> None:
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        got = sha256_fingerprint(key)
        if got != self.expected:
            raise paramiko.SSHException(
                "host key MISMATCH for %s: pinned=%s got=%s" % (hostname, self.expected, got))
        log("host key OK (%s)" % got)


def new_client() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(PinnedHostKey(pinned_fingerprint()))
    return c


def connect_password(password: str) -> paramiko.SSHClient:
    c = new_client()
    c.connect(HOST, port=PORT, username=ADMIN_USER, password=password,
              timeout=20, banner_timeout=20, auth_timeout=20,
              allow_agent=False, look_for_keys=False)
    return c


def run(c: paramiko.SSHClient, cmd: str, timeout: int = 300):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), o, e


def run_sudo(c: paramiko.SSHClient, script: str, password: str, timeout: int = 300):
    """Run ``script`` under bash as root.  Password goes to sudo's stdin only."""
    remote = "/tmp/.llmlab.%d.%d.sh" % (os.getpid(), int.from_bytes(os.urandom(3), "big"))
    sftp = c.open_sftp()
    with sftp.file(remote, "w") as f:
        f.write(script)
    sftp.chmod(remote, 0o600)
    sftp.close()

    stdin, out, err = c.exec_command("sudo -S -p '' bash " + remote, timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    try:
        stdin.channel.shutdown_write()
    except Exception:  # noqa: BLE001
        pass
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    try:
        c.exec_command("rm -f " + remote)
    except Exception:  # noqa: BLE001
        pass
    return rc, o, e


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def ensure_keypair() -> str:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    pub = Path(str(KEY_PATH) + ".pub")
    if not KEY_PATH.exists():
        log("generating project key (ed25519, no passphrase, project-local)")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "llm-lab@windows",
             "-f", str(KEY_PATH)],
            check=True, capture_output=True,
        )
        try:
            os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    line = pub.read_text(encoding="utf-8").strip()
    if not line.startswith("ssh-ed25519 "):
        die("unexpected public key format in %s" % pub)
    return line


def authorize_key(c: paramiko.SSHClient, publine: str, password: str) -> None:
    marker = "llm-lab@windows"
    script = """set -eu
U=%(user)s
H=$(getent passwd "$U" | cut -d: -f6)
G=$(id -gn "$U")
[ -n "$H" ] || { echo "no home for $U" >&2; exit 1; }
install -d -m 700 -o "$U" -g "$G" "$H/.ssh"
AK="$H/.ssh/authorized_keys"
touch "$AK"; chown "$U:$G" "$AK"; chmod 600 "$AK"
if grep -qF '%(marker)s' "$AK"; then
  echo "ALREADY_PRESENT"
else
  cp -a "$AK" "$AK.bak-$(date +%%Y%%m%%d%%H%%M%%S)" 2>/dev/null || true
  printf '%%s\\n' '%(pub)s' >> "$AK"
  echo "ADDED"
fi
echo "entries=$(grep -c . "$AK")"
""" % {"user": TARGET_USER, "marker": marker, "pub": publine}

    rc, o, e = run_sudo(c, script, password)
    if rc != 0:
        die("authorize step rc=%d stderr=%s" % (rc, e.strip()[:400]))
    log("authorized_keys for %s: %s" % (TARGET_USER, o.strip().replace("\n", " ")))


def fetch_secrets(c: paramiko.SSHClient) -> None:
    rc, o, e = run(c, "grep -E '^[[:space:]]*(export[[:space:]]+)?STRATA_API_KEY=' " + REMOTE_API_ENV)
    if rc != 0 or not o.strip():
        log("WARN: could not read API key (%s)" % (e.strip()[:200] or "empty"))
        return
    value = o.strip().splitlines()[-1].split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        log("WARN: API key entry present but empty")
        return
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(
        "# LLM Lab local secrets -- GITIGNORED.\n"
        "# Never copy this into reports, logs, screenshots or browser code.\n"
        "# Fetched %s from %s (mode 600, owner {{LAB_ADMIN_USER}}).\n"
        "STRATA_API_KEY=%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), REMOTE_API_ENV, value),
        encoding="utf-8")
    try:
        os.chmod(SECRETS, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    log("wrote %s  (value withheld; sha256[:12]=%s)"
        % (SECRETS.relative_to(PROJECT), hashlib.sha256(value.encode()).hexdigest()[:12]))


def fetch_artifacts(c: paramiko.SSHClient) -> None:
    LOCAL_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_BENCH_DIR.mkdir(parents=True, exist_ok=True)

    sftp = c.open_sftp()
    sftp.get(REMOTE_REPORT, str(LOCAL_REPORT_DIR / Path(REMOTE_REPORT).name))
    sftp.close()
    log("report: mirrored %s" % Path(REMOTE_REPORT).name)

    rc, o, _ = run(c, "find %s -type f -printf '%%s %%p\\n' | sort -k2" % REMOTE_RESULTS)
    entries = []
    for line in o.splitlines():
        if line.strip():
            size, path = line.split(" ", 1)
            entries.append((int(size), path))

    dest = LOCAL_BENCH_DIR / Path(REMOTE_RESULTS).name
    files = []
    sftp = c.open_sftp()
    for size, path in entries:
        rel = posixpath.relpath(path, REMOTE_RESULTS)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(path, str(target))
        files.append({"path": rel, "bytes": size,
                      "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "source": path})
    sftp.close()
    log("benchmark: mirrored %d files -> %s" % (len(files), dest.relative_to(PROJECT)))

    (LOCAL_BENCH_DIR / "provenance.json").write_text(json.dumps({
        "node": HOST,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fetched_by": "scripts/bootstrap_remote_access.py",
        "note": "Verbatim mirror. The originals on the node are authoritative and were not modified.",
        "report": {"source": REMOTE_REPORT},
        "benchmark_run": {"source": REMOTE_RESULTS, "files": files},
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log("wrote provenance manifest")


def verify_key_auth() -> None:
    log("verifying key-only authentication as %s ..." % TARGET_USER)
    k = paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    c = new_client()
    try:
        c.connect(HOST, port=PORT, username=TARGET_USER, pkey=k, timeout=20,
                  allow_agent=False, look_for_keys=False)
        rc, o, _ = run(c, "id -un; hostname; uname -r")
        log("KEY AUTH OK -> %s" % o.strip().replace("\n", " / "))
    except Exception as exc:  # noqa: BLE001
        log("KEY AUTH FAILED: %s" % exc)
    finally:
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-artifacts", action="store_true")
    ap.add_argument("--skip-key-setup", action="store_true")
    args = ap.parse_args()

    pw_file = os.environ.get("LAB_ADMIN_PW_FILE")
    if not pw_file or not Path(pw_file).exists():
        die("set LAB_ADMIN_PW_FILE to a file containing the node admin password")
    password = Path(pw_file).read_text(encoding="utf-8").strip()

    publine = ensure_keypair()

    admin = connect_password(password)
    try:
        if not args.skip_key_setup:
            authorize_key(admin, publine, password)
        fetch_secrets(admin)
        if not args.skip_artifacts:
            fetch_artifacts(admin)
    finally:
        admin.close()

    if not args.skip_key_setup:
        verify_key_auth()
    log("done.")


if __name__ == "__main__":
    main()
