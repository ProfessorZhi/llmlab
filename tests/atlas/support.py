"""本 Lane 测试包的共用脚手架：路径引导、临时目录、受信文档与语料构造。

**这里出现的每一个 ``key_id`` / ``principal`` / ``project`` 都是虚构的。**
本文件不读取、也不指向任何真实 ``usage.jsonl`` 或真实 Key Directory —— 那两样东西
属于 Dell 现场（见 Issue #77 的 HOST_PENDING）。
"""

from __future__ import annotations

import json
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.atlas.dto import Provenance  # noqa: E402

#: 演示用受信文档：两把在用 key（同一 principal 名下，其中一把无 workload）
#: 加一把已吊销 key —— 覆盖「轮换后仍保留已吊销条目」这一现实形状。
DEMO_REVISION = "2026-10-17T00:00:00Z#1"

DEMO_BINDINGS = [
    {"key_id": "key-alpha", "principal": "u-1001", "project": "proj-a", "workload": "resume-parse"},
    {"key_id": "key-orphan", "principal": "u-1001", "project": "proj-a"},
    {"key_id": "key-retired", "principal": "u-1001", "project": "proj-a", "revoked": True},
    {"key_id": "key-gamma", "principal": "u-2001", "project": "proj-b", "workload": "eval-suite"},
]


def directory_document(**overrides) -> dict:
    """构造一份 ``atlas-key-directory/1`` 文档；``overrides`` 覆盖顶层字段。"""
    document = {
        "schema_version": "atlas-key-directory/1",
        "revision": DEMO_REVISION,
        "bindings": [dict(binding) for binding in DEMO_BINDINGS],
    }
    document.update(overrides)
    return document


def dumps(document: object) -> str:
    return json.dumps(document, sort_keys=True)


#: 一份最小但真实的 Gateway 元数据语料（两行，跨两个 key / 两个模型）。
DEMO_CORPUS_LINES = [
    '{"ts":"2026-10-14T00:10:00Z","key_id":"key-alpha","request_id":"req-1",'
    '"model":"m-a","status":200,"outcome":"success","stream":true,'
    '"usage":{"input_tokens":10,"output_tokens":20},"queue_ms":3,"ttft_ms":40,"total_ms":900}',
    '{"ts":"2026-10-14T01:20:00Z","key_id":"key-gamma","request_id":"req-2",'
    '"model":"m-b","status":500,"outcome":"failed","stream":false,'
    '"usage":null,"total_ms":120}',
]


def corpus_text(lines: list[str] | None = None) -> str:
    """把若干行拼成 JSONL 原文（**末尾保留换行**，与真实导出一致）。"""
    return "\n".join((DEMO_CORPUS_LINES if lines is None else lines)) + "\n"


def write_file(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def directory_from_document():
    """把演示文档喂给**被测**的加载器，得到测试要用的 ``KeyDirectory``。

    刻意走加载器而不是手搓 ``KeyDirectory([...])``：这样逐条视图的用例验证的
    是一条真实链路（文档 -> 受信目录 -> 授权判定），而不是一个手写的替身。
    """
    from backend.atlas.corpus import parse_key_directory_document

    return parse_key_directory_document(dumps(directory_document())).directory


#: 合成来源的显式声明。``Provenance`` 没有默认值，正是为了不让调用方默默把
#: 合成数据说成生产统计。
SYNTHETIC_PROVENANCE = Provenance(source="gateway-export", synthetic=True)


__all__ = [
    "DEMO_BINDINGS",
    "DEMO_CORPUS_LINES",
    "DEMO_REVISION",
    "SYNTHETIC_PROVENANCE",
    "corpus_text",
    "directory_document",
    "directory_from_document",
    "dumps",
    "write_file",
]
