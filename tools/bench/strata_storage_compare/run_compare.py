#!/usr/bin/env python3
"""Run the same Strata engine/model from two storage roots and compare.

This is an operational lab harness, not a CI test. It stops the engine service,
evicts page cache, starts one manual instance per storage root, runs a fixed
client suite, records I/O and VRAM, and restores the service in ``finally``.

Example::

    STRATA_API_KEY=... python3 tools/bench/strata_storage_compare/run_compare.py \
        --config /opt/strata/strata-iq2_xs.json \
        --source-root /srv/strata-data \
        --targets nvme=/srv/strata-data hdd=/data/scratch/strata-bench \
        --model qwen3.8-flash-next-iq2_xs \
        --systemd-unit strata.service \
        --suite extended \
        --out /tmp/strata-compare

Use ``--dry-run`` to generate the two configs and print the plan without
stopping anything.
"""
import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PYTHON = "/opt/strata/.venv/bin/python"
DEFAULT_SERVER = "/opt/strata/serve/server.py"
DEFAULT_ENGINE_PATTERN = "/opt/strata/engine/strata --serve"


def sh(args):
    return subprocess.run(args, check=False, text=True, capture_output=True)


def read_key(path, env_key):
    if env_key:
        return env_key
    if path:
        raw = Path(path).read_text(encoding="utf-8").strip()
        value = raw.split("=", 1)[1] if "=" in raw else raw
        # The engine accepts comma-separated keys; the whole string is not one.
        return value.split(",")[0].strip()
    raise SystemExit("set STRATA_API_KEY or pass --key-file")


def rewrite(node, source_root, target_root):
    if isinstance(node, str):
        return node.replace(source_root, target_root)
    if isinstance(node, list):
        return [rewrite(x, source_root, target_root) for x in node]
    if isinstance(node, dict):
        return {k: rewrite(v, source_root, target_root) for k, v in node.items()}
    return node


def evict(root):
    """Drop a tree from page cache without root or dropping the whole cache."""
    sh(["sync"])
    dropped = files = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        fd = os.open(e.path, os.O_RDONLY)
                        try:
                            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                        finally:
                            os.close(fd)
                        files += 1
                        dropped += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return {"files": files, "bytes": dropped}


def port_open(host, port):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def http_status(host, port, path, key):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {key}"})
        return conn.getresponse().status
    except OSError:
        return None
    finally:
        conn.close()


def wait_ready(host, port, key, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if http_status(host, port, "/v1/models", key) == 200:
            return round(time.monotonic() - start, 1)
        time.sleep(2)
    raise TimeoutError(f"engine not ready within {timeout}s")


def stop_unit(unit, scope):
    if not unit:
        return
    sh(["systemctl", scope, "stop", unit])


def start_unit(unit, scope):
    if not unit:
        return
    sh(["systemctl", scope, "start", unit])


def stop_engine_processes(pattern):
    for _ in range(60):
        if not sh(["pgrep", "-f", pattern]).stdout.strip():
            return
        time.sleep(1)
    sh(["pkill", "-TERM", "-f", pattern])
    time.sleep(3)


def engine_pid(pattern):
    r = sh(["pgrep", "-f", pattern])
    pids = [int(x) for x in r.stdout.split() if x.isdigit()]
    return pids[-1] if pids else None


def proc_io(pid):
    out = {}
    try:
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            k, v = line.split(":")
            out[k.strip()] = int(v)
    except (OSError, ValueError):
        return None
    return out


def proc_rss_kb(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def meminfo():
    want = {"MemAvailable": "mem_available_kb", "SwapFree": "swap_free_kb", "Cached": "cached_kb"}
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            if key in want:
                out[want[key]] = int(rest.split()[0])
    except (OSError, ValueError):
        return None
    return out


def gpu_mib():
    r = sh(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return [int(x) for x in r.stdout.split()]
    except ValueError:
        return None


def wait_vram_free(threshold_mib, timeout=180):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        used = gpu_mib()
        if not used or max(used) < threshold_mib:
            return True
        time.sleep(2)
    raise RuntimeError(f"VRAM not released: {gpu_mib()}")


def start_engine(python, server, config_path, bind_host, port, log_path, cwd):
    handle = open(log_path, "wb")
    proc = subprocess.Popen(
        [python, "-u", server, "--engine", "strata", "--config", str(config_path),
         "--host", bind_host, "--port", str(port)],
        cwd=cwd, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    return proc, handle


def stop_engine(proc, handle, pattern, host, port):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    handle.close()
    sh(["pkill", "-TERM", "-f", pattern])
    for _ in range(60):
        if not port_open(host, port):
            return
        time.sleep(1)


def run_client(client, env, timeout):
    r = subprocess.run([sys.executable, str(client)], env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"client failed: {r.stderr[-1200:]}")
    if r.stderr:
        sys.stderr.write(r.stderr)
    return json.loads(r.stdout)


def run_case(name, config_path, config, tree, args, key, out_dir):
    print(f"[{name}] evicting {tree}", flush=True)
    evicted = evict(tree)
    print(f"[{name}] evicted {evicted['files']} files / {evicted['bytes'] / 2**30:.1f} GiB", flush=True)
    wait_vram_free(args.vram_free_mib)
    before_gpu = gpu_mib()
    log_path = out_dir / f"engine-{name}.log"
    proc, handle = start_engine(args.python, args.server, config_path, args.bind_host,
                                args.port, log_path, config.get("cwd") or ".")
    try:
        load_s = wait_ready(args.client_host, args.port, key, args.readiness_timeout)
        pid = engine_pid(args.engine_pattern)
        print(f"[{name}] ready in {load_s}s, pid={pid}", flush=True)
        startup_io = proc_io(pid)
        startup_rss = proc_rss_kb(pid)
        ready_gpu = gpu_mib()
        env = {
            **os.environ,
            "STRATA_API_KEY": key,
            "STRATA_MODEL": args.model,
            "STRATA_BASE_URL": f"http://{args.client_host}:{args.port}",
            "STRATA_ENGINE_PATTERN": args.engine_pattern,
        }
        client = Path(__file__).with_name(f"client_{args.suite}.py")
        bench = run_client(client, env, args.client_timeout)
        end_io = proc_io(pid)
        delta = None
        if startup_io and end_io:
            delta = {k: end_io.get(k, 0) - startup_io.get(k, 0) for k in ("read_bytes", "rchar")}
        return {
            "case": name,
            "tree": str(tree),
            "evicted": evicted,
            "load_seconds": load_s,
            "startup_io": startup_io,
            "benchmark_io_delta": delta,
            "startup_rss_kb": startup_rss,
            "gpu_mib_before": before_gpu,
            "gpu_mib_ready": ready_gpu,
            "memory_ready": meminfo(),
            "bench": bench,
        }
    finally:
        stop_engine(proc, handle, args.engine_pattern, args.bind_host, args.port)


def restore(args, key):
    stop_engine_processes(args.engine_pattern)
    start_unit(args.systemd_unit, args.systemctl_scope)
    if not args.systemd_unit:
        return {"ok": True, "skipped": "no --systemd-unit"}
    try:
        load_s = wait_ready(args.client_host, args.port, key, args.readiness_timeout)
        print(f"[restore] service ready in {load_s}s", flush=True)
        return {"ok": True, "load_seconds": load_s}
    except Exception as exc:  # noqa: BLE001
        print(f"[restore] FAILED: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}


def parse_targets(values):
    out = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"--targets expects NAME=DIR, got {item!r}")
        name, path = item.split("=", 1)
        out[name] = Path(path)
    if len(out) < 2:
        raise SystemExit("--targets needs at least two roots to compare")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="engine config JSON that points at --source-root")
    ap.add_argument("--source-root", required=True, help="data root inside --config to rewrite")
    ap.add_argument("--targets", nargs="+", required=True, metavar="NAME=DIR")
    ap.add_argument("--model", required=True)
    ap.add_argument("--suite", choices=("basic", "extended"), default="extended")
    ap.add_argument("--out", default="bench-out")
    ap.add_argument("--key-file", help="file containing STRATA_API_KEY=... (comma-separated keys allowed)")
    ap.add_argument("--systemd-unit", help="unit to stop before and restore after, e.g. strata.service")
    ap.add_argument("--systemctl-scope", choices=("--user", "--system"), default="--user")
    ap.add_argument("--python", default=DEFAULT_PYTHON)
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--engine-pattern", default=DEFAULT_ENGINE_PATTERN)
    ap.add_argument("--bind-host", default="0.0.0.0")
    ap.add_argument("--client-host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--readiness-timeout", type=int, default=2400)
    ap.add_argument("--client-timeout", type=int, default=7200)
    ap.add_argument("--vram-free-mib", type=int, default=2000)
    ap.add_argument("--only", help="comma-separated target names to run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = parse_targets(args.targets)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        unknown = wanted - set(targets)
        if unknown:
            raise SystemExit(f"--only names unknown targets: {sorted(unknown)}")
        targets = {k: v for k, v in targets.items() if k in wanted}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    source_root = str(Path(args.source_root))
    configs = {}
    for name, root in targets.items():
        cfg = rewrite(base, source_root, str(root))
        cfg["log"] = str(out_dir / f"engine-{name}.log")
        path = out_dir / f"config-{name}.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        configs[name] = (path, cfg)

    if args.dry_run:
        print(json.dumps({n: str(p) for n, (p, _) in configs.items()}, ensure_ascii=False, indent=2))
        print("dry run: nothing stopped or started", flush=True)
        return

    key = read_key(args.key_file, os.environ.get("STRATA_API_KEY"))
    if port_open(args.client_host, args.port) and not args.systemd_unit:
        raise SystemExit(
            f"port {args.port} is already in use; stop the engine first or pass --systemd-unit"
        )

    results = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "suite": args.suite, "cases": []}
    print(f"[main] stopping {args.systemd_unit or 'engine'}", flush=True)
    stop_unit(args.systemd_unit, args.systemctl_scope)
    stop_engine_processes(args.engine_pattern)
    try:
        for name, (config_path, cfg) in configs.items():
            # Evict the whole target root, not just the pack subdirectory.
            case = run_case(name, config_path, cfg, targets[name], args, key, out_dir)
            results["cases"].append(case)
            (out_dir / f"result-{args.suite}.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{name}] done", flush=True)
    finally:
        print("[main] restoring service", flush=True)
        results["restore"] = restore(args, key)
        results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (out_dir / f"result-{args.suite}.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
