"""Extended Strata benchmark: context, output length, concurrency, thinking.

Same request path as ``client_basic.py``, with the variables pulled apart:
- context: 512 / 2K / 8K / 16K / 32K prompt tokens;
- decode: short, 512 and 1024 token outputs;
- concurrency: 1 / 2 / 4 / 8 simultaneous requests (wall time and aggregate);
- thinking: on/off, for reference.

The context suite runs first on purpose. A concurrency burst occupies the
batch slots and tightens VRAM; a long prompt immediately afterwards has been
observed to fail prefill with a cuBLAS error and restart the engine, which
would be recorded as a ~60 s TTFT.
"""
import concurrent.futures
import http.client
import json
import os
import secrets
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

KEY = os.environ.get("STRATA_API_KEY")
MODEL = os.environ.get("STRATA_MODEL")
BASE_URL = os.environ.get("STRATA_BASE_URL", "http://127.0.0.1:8080")
ENGINE_PATTERN = os.environ.get("STRATA_ENGINE_PATTERN", "/opt/strata/engine/strata --serve")
PARSED = urlparse(BASE_URL)
HOST = PARSED.hostname or "127.0.0.1"
PORT = PARSED.port or 80

SEED = (
    "共享推理平台需要把模型权重、运行时算力和访问凭据分开管理：权重只读共享，"
    "算力按服务实例分配，凭据按调用者签发。这样做的目的是在多人共用一台"
    "GPU 服务器时，既能避免重复拷贝上百 GB 的模型文件，又能让每个调用者"
    "拥有独立的配额、审计和撤销能力。存储分层则决定了冷启动与稳态吞吐的"
    "上限，需要先用真实负载测量，再决定哪些模型放在固态盘、哪些放在机械盘。"
)

SHORT_Q = "请用一句话解释什么是 TCP 三次握手。"
DETAIL_Q = (
    "请详细解释 TCP 三次握手：每一步客户端和服务器的状态如何变化，"
    "为什么不能是两次，SYN 洪泛攻击利用了什么，以及如何防御。"
)

RESTARTS = 0


def nonce():
    return f"基准编号 {secrets.token_hex(4)}。"


def prompt_for_tokens(target_tokens):
    # Measured ratio is about 1.65 Chinese characters per token; the actual
    # count is taken from usage.prompt_tokens.
    chars = int(target_tokens * 1.65)
    body = (SEED * (chars // len(SEED) + 1))[:chars]
    return f"{nonce()}\n{body}\n\n请用一句话总结上面这段话的核心观点。"


def stream_chat(messages, max_tokens=128, thinking=False):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=1800)
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": thinking},
    })
    t0 = time.monotonic()
    conn.request("POST", "/v1/chat/completions", body=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
    })
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}: {resp.read(400)!r}")
    first = last = None
    chars = 0
    usage = None
    while True:
        line = resp.readline()
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content")
            if text:
                now = time.monotonic()
                if first is None:
                    first = now
                last = now
                chars += len(text)
    conn.close()
    end = time.monotonic()
    ttft = (first - t0) if first else None
    decode = (last - first) if first and last and last > first else None
    tokens = (usage or {}).get("completion_tokens")
    prompt_tokens = (usage or {}).get("prompt_tokens")
    tps = (tokens - 1) / decode if tokens and decode else None
    return {
        "ttft_s": round(ttft, 3) if ttft else None,
        "total_s": round(end - t0, 3),
        "decode_s": round(decode, 3) if decode else None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": tokens,
        "chars": chars,
        "tps": round(tps, 1) if tps else None,
    }


def engine_pid():
    r = subprocess.run(["pgrep", "-f", ENGINE_PATTERN], capture_output=True, text=True)
    pids = [int(x) for x in r.stdout.split() if x.isdigit()]
    return pids[-1] if pids else None


def read_bytes():
    pid = engine_pid()
    if not pid:
        return None
    try:
        for line in open(f"/proc/{pid}/io"):
            if line.startswith("read_bytes:"):
                return int(line.split(":")[1])
    except OSError:
        return None
    return None


def _retry(messages, max_tokens, attempts=2):
    """Retry an empty response once; also flag an engine restart.

    A long stall is often not slowness but a prefill crash followed by the
    supervisor starting a new instance. Recording that separately keeps it out
    of the latency comparison.
    """
    global RESTARTS
    last = None
    for _ in range(attempts):
        before = engine_pid()
        last = stream_chat(messages, max_tokens=max_tokens)
        after = engine_pid()
        if before and after and before != after:
            last["engine_restarted"] = True
            RESTARTS += 1
        if last["prompt_tokens"] is not None and last["ttft_s"] is not None:
            return last
    return last


def context_suite():
    rows = []
    # Walk a long prompt first so expert paging is paid before the first
    # measured length; otherwise that length absorbs tens of seconds alone.
    stream_chat([{"role": "user", "content": prompt_for_tokens(2048)}], max_tokens=8)
    for target in (512, 2048, 8192, 16384, 32768):
        text = prompt_for_tokens(target)
        r = _retry([{"role": "user", "content": text}], max_tokens=32)
        r["target_tokens"] = target
        rows.append(r)
        print(f"  context {target}: prompt={r['prompt_tokens']} ttft={r['ttft_s']}s", file=sys.stderr, flush=True)
    return rows


def decode_suite():
    rows = []
    for n, question in ((128, SHORT_Q), (512, DETAIL_Q), (1024, DETAIL_Q)):
        r = _retry([{"role": "user", "content": f"{nonce()}\n{question}"}], max_tokens=n)
        r["requested_tokens"] = n
        rows.append(r)
        print(f"  decode {n}: tokens={r['completion_tokens']} tps={r['tps']}", file=sys.stderr, flush=True)
    return rows


def concurrency_suite():
    rows = []
    for n in (1, 2, 4, 8):
        barrier = threading.Barrier(n)

        def one(_):
            messages = [{"role": "user", "content": f"{nonce()}\n{SHORT_Q}"}]
            barrier.wait()
            return stream_chat(messages, max_tokens=128)

        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(one, range(n)))
        wall = time.monotonic() - t0
        total_tokens = sum((r["completion_tokens"] or 0) for r in results)
        row = {
            "n": n,
            "wall_s": round(wall, 3),
            "aggregate_tps": round(total_tokens / wall, 1) if wall else None,
            "ttft_median_s": round(statistics.median([r["ttft_s"] for r in results if r["ttft_s"]]), 3),
            "tps_median": round(statistics.median([r["tps"] for r in results if r["tps"]]), 1),
            "requests": results,
        }
        rows.append(row)
        print(f"  concurrency {n}: wall={row['wall_s']}s aggregate={row['aggregate_tps']} tps", file=sys.stderr, flush=True)
    return rows


def thinking_suite():
    out = {}
    for name, flag in (("off", False), ("on", True)):
        r = stream_chat([{"role": "user", "content": f"{nonce()}\n{SHORT_Q}"}],
                        max_tokens=256, thinking=flag)
        out[name] = r
        print(f"  thinking {name}: tokens={r['completion_tokens']} tps={r['tps']}", file=sys.stderr, flush=True)
    return out


def main():
    if not KEY:
        raise SystemExit("STRATA_API_KEY is not set")
    if not MODEL:
        raise SystemExit("STRATA_MODEL is not set")
    started_read = read_bytes()
    # Warm-up: keep the first request's cold cost out of every group.
    stream_chat([{"role": "user", "content": f"{nonce()}\n{SHORT_Q}"}], max_tokens=8)
    after_warm = read_bytes()

    out = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out["warmup_read_bytes"] = (after_warm - started_read) if started_read and after_warm else None

    marks = {}
    out["context"] = context_suite(); marks["after_context"] = read_bytes()
    out["decode"] = decode_suite(); marks["after_decode"] = read_bytes()
    out["concurrency"] = concurrency_suite(); marks["after_concurrency"] = read_bytes()
    out["thinking"] = thinking_suite(); marks["after_thinking"] = read_bytes()
    out["engine_restarts"] = RESTARTS

    base = after_warm
    for key, value in marks.items():
        out[f"read_bytes_{key}"] = (value - base) if base and value else None
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
