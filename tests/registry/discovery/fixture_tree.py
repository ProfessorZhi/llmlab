"""仿 kaidong 家目录的合成 fixture。

为什么用 ``truncate`` 造稀疏文件：验收要的是「100 MB 阈值前后的判定」，不是
真的读写 1 GB；稀疏文件在磁盘上几乎不占空间，``stat`` 却会如实报出逻辑体积，
测试因此既快又能代表真实部署。

目录布局刻意把「真模型」「噪声」「6 棵源码树」「2 个 build」「HF cache」
放在同一棵树里，让 ``scan_tree`` 在一个 root 上同时面对所有规则。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["EXPECTED_BUILD_COUNT", "EXPECTED_MODEL_COUNT", "build_kaidong_like_tree"]

MB = 1024 * 1024

#: S1 验收项：仿 kaidong 目录必须恰好得到 4 个模型 / 2 个 build。
EXPECTED_MODEL_COUNT = 4
EXPECTED_BUILD_COUNT = 2

#: HF cache 里的单个权重分片大小；测试用它验证软链被跟随、报出真实体积。
CACHE_SHARD_BYTES = 120 * MB


def _sparse(path: Path, size: int) -> Path:
    """创建一个逻辑大小为 ``size`` 的稀疏文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def _text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    _text(path, content)
    path.chmod(0o755)
    return path


def _hf_config(path: Path) -> Path:
    return _text(path, json.dumps({"model_type": "fixture", "architectures": ["FixtureForCausalLM"]}))


def build_kaidong_like_tree(base: Path) -> Path:
    """在 ``base`` 下生成 fixture 树，返回 ``base``。

    调用方负责提供临时目录；本函数不读环境变量、不碰 HOME。
    """

    base.mkdir(parents=True, exist_ok=True)

    # --- 4 个真模型 -------------------------------------------------------
    # 1) 单个大 GGUF
    _sparse(base / "models" / "one" / "big-one.gguf", 300 * MB)
    # 2) 另一个大 GGUF + 视觉投影辅助文件（mmproj 本身不是模型）
    _sparse(base / "models" / "two" / "big-two.gguf", 260 * MB)
    _sparse(base / "models" / "two" / "mmproj-big-two.gguf", 24 * MB)
    # 3) 两分片 HF 目录：60 MB + 60 MB = 120 MB，刚好过阈值
    _hf_config(base / "hf" / "two-shard" / "config.json")
    _sparse(base / "hf" / "two-shard" / "model-00001-of-00002.safetensors", 60 * MB)
    _sparse(base / "hf" / "two-shard" / "model-00002-of-00002.safetensors", 60 * MB)
    # 4) HF cache 的单分片快照：config 与权重都是指向 blobs/ 的软链，
    #    只有跟随软链才能看到 120 MB。
    cache_root = base / "hf-cache" / "hub" / "models--org--name"
    config_blob = _text(cache_root / "blobs" / "cfgsha", '{"model_type": "fixture"}')
    weight_blob = _sparse(cache_root / "blobs" / "modelsha", CACHE_SHARD_BYTES)
    _text(cache_root / "refs" / "main", "rev1\n")
    snapshot = cache_root / "snapshots" / "rev1"
    snapshot.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(config_blob, snapshot), snapshot / "config.json")
    os.symlink(os.path.relpath(weight_blob, snapshot), snapshot / "model.safetensors")

    # --- 噪声 -------------------------------------------------------------
    _sparse(base / "noise" / "vocab-a" / "ggml-vocab-a.gguf", 1 * MB)
    _sparse(base / "noise" / "vocab-b" / "ggml-vocab-b.gguf", 2 * MB)
    _sparse(base / "noise" / "small-gguf" / "tiny-10mb.gguf", 10 * MB)
    # 有 config.json 但权重只有 5 MB 的 HF 目录
    _hf_config(base / "noise" / "hf-small" / "config.json")
    _sparse(base / "noise" / "hf-small" / "model.safetensors", 5 * MB)

    # --- 6 棵源码树：里面的 200 MB model.gguf 必须全部被排除 ---------------
    # (1) third_party/*/models；旁边就是真正要发现的 llama-server。
    _sparse(base / "third_party" / "llama.cpp" / "models" / "model.gguf", 200 * MB)
    _executable(base / "third_party" / "llama.cpp" / "build" / "bin" / "llama-server")
    # (2)~(6) 其余五棵，各把模型藏在不同名字的排除目录里。
    _sparse(base / "src" / "git-tree" / ".git" / "model.gguf", 200 * MB)
    _sparse(base / "src" / "node-tree" / "node_modules" / "model.gguf", 200 * MB)
    _sparse(base / "src" / "venv-tree" / ".venv" / "model.gguf", 200 * MB)
    _sparse(base / "src" / "cmake-tree" / "CMakeFiles" / "model.gguf", 200 * MB)
    _sparse(base / "src" / "pycache-tree" / "__pycache__" / "model.gguf", 200 * MB)

    # --- 2 个 build + 2 个诱饵 -------------------------------------------
    _executable(base / "venv" / "bin" / "vllm", "#!/bin/sh\necho vllm\n")
    # 诱饵 1：同名但没有执行位。
    _text(base / "dist" / "llama-server", "#!/bin/sh\nexit 0\n")
    base.joinpath("dist", "llama-server").chmod(0o644)
    # 诱饵 2：site-packages 里的可执行 vllm；整个目录被排除，不该被发现。
    _executable(base / "vendor" / "site-packages" / "vllm", "#!/bin/sh\necho not-a-build\n")

    return base
