"""Minimal Strata benchmark client: TTFT, decode speed, total latency.

Read-only: it sends chat completions and prints aggregates. Credentials come
from ``STRATA_API_KEY``; the endpoint and model from ``STRATA_BASE_URL`` and
``STRATA_MODEL``. It never writes prompts or completions to disk.
"""
import http.client
import json
import os
import secrets
import statistics
import sys
import time
from urllib.parse import urlparse

KEY = os.environ.get("STRATA_API_KEY")
MODEL = os.environ.get("STRATA_MODEL")
BASE_URL = os.environ.get("STRATA_BASE_URL", "http://127.0.0.1:8080")
PARSED = urlparse(BASE_URL)
HOST = PARSED.hostname or "127.0.0.1"
PORT = PARSED.port or 80

LONG_SEED = (
    "共享推理平台需要把模型权重、运行时算力和访问凭据分开管理：权重只读共享，"
    "算力按服务实例分配，凭据按调用者签发。这样做的目的是在多人共用一台"
    "GPU 服务器时，既能避免重复拷贝上百 GB 的模型文件，又能让每个调用者"
    "拥有独立的配额、审计和撤销能力。存储分层则决定了冷启动与稳态吞吐的"
    "上限，需要先用真实负载测量，再决定哪些模型放在固态盘、哪些放在机械盘。"
)


def prompts(long_chars=4000):
    long_text = LONG_SEED * (long_chars // len(LONG_SEED) + 1)
    return {
        "short": [{"role": "user", "content": "请用一句话解释什么是 TCP 三次握手。"}],
        "long": [{"role": "user", "content": long_text[:long_chars] + "\n\n请用一句话总结上面这段话的核心观点。"}],
    }


def stream_chat(messages, max_tokens=128):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=900)
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        # Thinking would put a long reasoning_content block before the answer
        # and make disk comparisons about "how long it thought" instead of
        # "how fast the first token arrived".
        "chat_template_kwargs": {"enable_thinking": False},
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


def fresh(messages):
    """Vary the prefix so a prompt cache cannot turn TTFT into a cache hit."""
    out = json.loads(json.dumps(messages, ensure_ascii=False))
    out[0]["content"] = f"基准编号 {secrets.token_hex(4)}。\n" + out[0]["content"]
    return out


def measure(name, messages, runs=3):
    stream_chat(fresh(messages), max_tokens=8)  # warm-up, not measured
    rows = [stream_chat(fresh(messages), max_tokens=128) for _ in range(runs)]
    med = lambda k: statistics.median([r[k] for r in rows if r[k] is not None]) if any(r[k] is not None for r in rows) else None  # noqa: E731
    return {
        "case": name,
        "runs": rows,
        "median": {"ttft_s": med("ttft_s"), "tps": med("tps"), "total_s": med("total_s"),
                   "prompt_tokens": med("prompt_tokens")},
    }


VARIED = [
    "请用一句话解释什么是 TCP 三次握手。",
    "用 Python 写一个快速排序函数，并说明时间复杂度。",
    "用一句话说明向量数据库和关系数据库的区别。",
    "把“今天天气很好，适合出去走走”翻译成英文。",
]


def measure_varied(runs=1):
    """Different questions so expert routing spreads over more experts."""
    stream_chat(fresh([{"role": "user", "content": VARIED[0]}]), max_tokens=8)
    rows = []
    for question in VARIED:
        for _ in range(runs):
            rows.append(stream_chat(fresh([{"role": "user", "content": question}]), max_tokens=128))
    med = lambda k: statistics.median([r[k] for r in rows if r[k] is not None]) if any(r[k] is not None for r in rows) else None  # noqa: E731
    return {
        "case": "varied",
        "runs": rows,
        "median": {"ttft_s": med("ttft_s"), "tps": med("tps"), "total_s": med("total_s"),
                   "prompt_tokens": med("prompt_tokens")},
    }


def main():
    if not KEY:
        raise SystemExit("STRATA_API_KEY is not set")
    if not MODEL:
        raise SystemExit("STRATA_MODEL is not set")
    p = prompts()
    out = {
        "short": measure("short", p["short"]),
        "long": measure("long", p["long"]),
        "varied": measure_varied(),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
